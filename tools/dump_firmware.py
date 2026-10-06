#!/usr/bin/env python3
"""Dump a HiMont KC02's complete 4 MiB SPI flash over USB (Python 3.9+).

Quick start:
    python3 -m pip install pyusb libusb-package
    python3 tools/dump_firmware.py

Windows: substitute `py -3` for `python3`; see README.md for USB permissions/drivers.
This is a self-contained script; only PyUSB and a libusb backend are needed.

Design:
    Use stock vendor command 0xCD over USB mass-storage Bulk-Only Transport
    (interface 4), with the function pointer pinned to the memory-transfer helper.
    Check known routine signatures; cache-clean and verify fixed RAM uploads.
    Test RAM execution with a return-only canary before loading a 48-byte adapter
    that reshapes callback(buffer, length, offset) into SPI_read(offset, buffer, length).
    Flash -> reusable 2 KiB RAM buffer -> USB -> host; validate each command's
    status, check the image headers, and compare two complete 4 MiB reads before
    saving. Guard the scratch bounds to avoid documented patch regions.

No flash writes, UI patches, firmware updates or automatic USB resets are
performed. RAM changes disappear on power-off. A stalled camera may need a
power-cycle. Only the researched KC02 firmware layout is supported; matching
USB IDs alone do not establish firmware compatibility.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
from pathlib import Path
import platform
import struct
import sys
import tempfile
import time

SUPPORTED_IDS = ((0x0219, 0x3280), (0x1908, 0x3283))
WEBCAM_ID = (0x1908, 0x3282)
FLASH_SIZE = 0x400000
INTERFACE = 4
EP_IN, EP_OUT = 0x81, 0x01
MEMORY_TRANSFER_FN = 0x0204C624
CACHE_CLEAN_FN = 0x0202B058
CALLBACK_SKIP = 0xFFFFFFFF
SHIM_ADDR, CANARY_ADDR, BUFFER_ADDR = 0x020D8000, 0x020D8080, 0x020D8100

# SPI DMA rounds up to 16 bytes; keep every read aligned.
SPI_CHUNK = 2048

# The staging buffer ends at 0x020D8900. Beyond STAGING_LIMIT are documented
# unlock/hoist/child-lock patches; bigger buffers need a separate owned region.
STAGING_LIMIT = 0x020D9000
assert SPI_CHUNK % 16 == 0 and FLASH_SIZE % SPI_CHUNK == 0
assert BUFFER_ADDR + SPI_CHUNK <= STAGING_LIMIT

# Shim to shuffle the argument order of the 0xCD USB Vendor command so we
# can call into SPI_read(offset, buffer, length).
# OpenRISC-compatible, little-endian, no delay slots
# Entry: r3=buffer, r4=length, r5=flash offset. Saves r2/r9 before claiming stack
# space, calls only SPI_read (never write-enable/program/erase), then returns.
#
# 020D8000  l.sw   -8(r1), r2          # save caller's r2
# 020D8004  l.sw   -4(r1), r9          # save return address
# 020D8008  l.addi r1, r1, -8          # claim the saved-register stack frame
# 020D800C  l.ori  r2, r3, 0           # preserve buffer while shuffling arguments
# 020D8010  l.ori  r3, r5, 0           # SPI argument 1: flash offset
# 020D8014  l.ori  r5, r4, 0           # SPI argument 3: length
# 020D8018  l.ori  r4, r2, 0           # SPI argument 2: buffer
# 020D801C  l.jal  0x0203D38C          # SPI_read(offset, buffer, length)
# 020D8020  l.addi r1, r1, 8           # release frame; restore entry SP
# 020D8024  l.lwz  r2, -8(r1)         # restore caller's r2
# 020D8028  l.lwz  r9, -4(r1)         # restore return address
# 020D802C  l.jr   r9                 # return to stock USB memory-transfer helper
SHIM_BYTES = bytes.fromhex(
    "f817e1d7fc4fe1d7f8ff219c000043a8000065a80000a4a8000082a8"
    "dc94fd070800219cf8ff4184fcff218500480044"
)

# l.jr r9: return immediately.
CANARY_BYTES = bytes.fromhex("00480044")
CANARY_PATTERN = bytes((0xA5 ^ (i * 0x3D)) & 0xFF for i in range(SPI_CHUNK))
RAM_BLOBS = {
    SHIM_ADDR: SHIM_BYTES,
    CANARY_ADDR: CANARY_BYTES,
    BUFFER_ADDR: CANARY_PATTERN,
}

# Stock routine prologues, verified against the reference dump. CPU code bytes
# map to file offset (address - 0x02000000 + 0x2600), NOT linear flash addresses.
# Check before loading/executing RAM code. These checks are compatibility guards,
# not a proof that an arbitrary firmware sharing the prologues is safe.
ROUTINE_SIGNATURES = (
    (
        0x0204C624,
        bytes.fromhex("0d026018fc4fe1d7d8a363a8f80fe1d7"),
        "USB memory transfer",
    ),
    (0x0203D38C, bytes.fromhex("fc4fe1d7ec17e1d7f077e1d7f497e1d7"), "SPI flash reader"),
    (0x0202B058, bytes.fromhex("fc17e1d7fbff4018f80fe1d700c042a8"), "RAM cache clean"),
)


class DumpError(Exception):
    """An actionable failure suitable for display without a traceback."""


def load_usb():
    """Load dependencies lazily so --help works without them."""
    try:
        import usb.core
        import usb.util
        import usb.backend.libusb1
    except ImportError as exc:
        python = "py -3" if platform.system() == "Windows" else "python3"
        raise DumpError(
            f"PyUSB is missing. Install it with `{python} -m pip install pyusb "
            "libusb-package` using the same Python that runs this script."
        ) from exc

    backend = None
    try:
        import libusb_package
    except ImportError:
        pass
    else:
        backend = usb.backend.libusb1.get_backend(
            find_library=libusb_package.find_library
        )
    if backend is None:
        backend = usb.backend.libusb1.get_backend()
    if backend is None:
        raise DumpError(
            "No libusb 1.0 backend could be loaded. Install/reinstall libusb-package "
            "for this Python, or install the system libusb runtime. Python and "
            "libusb must have matching architectures. See README.md for OS-specific setup."
        )
    return usb, backend


def find_cameras(usb, backend):
    devices = list(
        usb.core.find(
            find_all=True,
            backend=backend,
            custom_match=lambda dev: (
                (dev.idVendor, dev.idProduct) in SUPPORTED_IDS + (WEBCAM_ID,)
            ),
        )
    )
    return [d for d in devices if (d.idVendor, d.idProduct) in SUPPORTED_IDS], [
        d for d in devices if (d.idVendor, d.idProduct) == WEBCAM_ID
    ]


def device_label(dev):
    # Do not request USB strings: listing should work without device permissions.
    return (
        f"{dev.idVendor:04x}:{dev.idProduct:04x} "
        f"(bus {getattr(dev, 'bus', None)}, address {getattr(dev, 'address', None)})"
    )


def select_camera(cameras, webcams, index):
    if not cameras:
        if webcams:
            raise DumpError(
                "KC02 detected in webcam-only mode (1908:3282), which has no "
                "flash-dump interface. Reconnect in USB mass-storage/USB-SD mode."
            )
        raise DumpError(
            "No KC02 camera found (expected 0219:3280 or 1908:3283). Power it on, "
            "check the USB data cable, try a direct USB port, and leave it on the "
            "USB/SD screen. See README.md for permissions/driver instructions."
        )
    if index is None:
        if len(cameras) != 1:
            raise DumpError(
                f"Found {len(cameras)} cameras. Run --list, then select one with "
                "--device NUMBER, or disconnect the others."
            )
        return cameras[0]
    if index > len(cameras):
        raise DumpError(
            f"Camera #{index} not found; --list currently shows {len(cameras)} camera(s)."
        )
    return cameras[index - 1]


def build_cbw(tag, buffer, length, direction, callback=CALLBACK_SKIP, offset=0):
    """The sole command builder: fixed memory routine, tightly bounded RAM writes.

    No caller can supply a function pointer. Only the three fixed RAM blobs can
    be written; callbacks can only skip, clean those writes, or execute our
    canary/SPI-read adapter. No arbitrary memory writes or calls are exposed.
    """
    # Enforce this at the transaction boundary, even with Python -O or if a
    # caller changes SPI_CHUNK. Never overwrite the documented patch regions.
    if buffer == BUFFER_ADDR and buffer + length > STAGING_LIMIT:
        raise DumpError(
            "Safety check: staging buffer would overlap documented patch regions at 0x020D9000."
        )
    if direction == 0x00:
        if buffer not in RAM_BLOBS or length != len(RAM_BLOBS[buffer]):
            raise DumpError(
                "Safety check: write is not one of the fixed RAM scratch blobs."
            )
        if callback != CACHE_CLEAN_FN or offset != 0:
            raise DumpError("Safety check: invalid RAM-write callback.")
    elif direction == 0x80:
        if callback == CALLBACK_SKIP:
            allowed = {(addr, len(blob)) for addr, blob in RAM_BLOBS.items()}
            allowed.update((addr, len(blob)) for addr, blob, _ in ROUTINE_SIGNATURES)
            if (buffer, length) not in allowed or offset != 0:
                raise DumpError(
                    "Safety check: memory read outside known firmware/scratch sites."
                )
        elif callback in (CANARY_ADDR, SHIM_ADDR):
            if buffer != BUFFER_ADDR or length != SPI_CHUNK:
                raise DumpError("Safety check: invalid SPI/canary buffer or length.")
            if callback == CANARY_ADDR and offset != 0:
                raise DumpError("Safety check: invalid canary argument.")
            if callback == SHIM_ADDR and not (
                0 <= offset <= FLASH_SIZE - SPI_CHUNK and offset % SPI_CHUNK == 0
            ):
                raise DumpError(
                    "Safety check: SPI offset outside the 4 MiB flash or unaligned."
                )
        else:
            raise DumpError("Safety check: callback is not permitted.")
    else:
        raise DumpError("Safety check: invalid USB transfer direction.")
    cdb = (
        bytes([0xCD])
        + struct.pack("<III", MEMORY_TRANSFER_FN, buffer, callback)
        + offset.to_bytes(3, "little")
    )
    return struct.pack("<4sIIBBB", b"USBC", tag, length, direction, 0, 16) + cdb


class CameraTransport:
    """Self-contained USB Bulk-Only Transport for the stock KC02 vendor command.

    Keep the active configuration: changing it can reset unrelated webcam/audio
    interfaces. Detach and reclaim interface 4 only, using public PyUSB APIs.
    """

    def __init__(self, usb, dev, timeout_ms, verbose=False, trace=False):
        self.usb, self.dev, self.timeout_ms = usb, dev, timeout_ms
        self.verbose, self.trace = verbose or trace, trace
        self.claimed = self.detached = False
        self.tag = 0

    def log(self, message):
        if self.verbose:
            print(f"🔧 {message}", file=sys.stderr, flush=True)

    def __enter__(self):
        try:
            self.log("Reading active USB configuration; leaving its value unchanged.")
            config = self.dev.get_active_configuration()
            # PyUSB config[(index, alt_index)] uses descriptor positions, not
            # bInterfaceNumber. The stock camera has ONE interface numbered 4.
            interface = next(
                (
                    item
                    for item in config
                    if item.bInterfaceNumber == INTERFACE
                    and item.bAlternateSetting == 0
                ),
                None,
            )
            if interface is None:
                raise DumpError(
                    "Camera has no mass-storage interface 4. Reconnect in USB/SD mode."
                )
            endpoints = {ep.bEndpointAddress: ep for ep in interface}
            if (
                interface.bInterfaceClass,
                interface.bInterfaceSubClass,
                interface.bInterfaceProtocol,
            ) != (8, 6, 0x50) or any(
                address not in endpoints
                or self.usb.util.endpoint_type(endpoints[address].bmAttributes)
                != self.usb.util.ENDPOINT_TYPE_BULK
                for address in (EP_IN, EP_OUT)
            ):
                raise DumpError(
                    "Unexpected interface 4 layout (requires mass-storage BOT and bulk "
                    "endpoints 0x81/0x01). This camera/mode is not supported."
                )
            try:
                active = self.dev.is_kernel_driver_active(INTERFACE)
            except NotImplementedError:
                active = False  # Windows/macOS may not implement this query.
            if active:
                self.log(f"Detaching kernel driver from interface {INTERFACE}.")
                self.dev.detach_kernel_driver(INTERFACE)
                self.detached = True
            self.log(
                f"Claiming interface {INTERFACE}; bulk OUT=0x01, IN=0x81; timeout={self.timeout_ms} ms."
            )
            self.usb.util.claim_interface(self.dev, INTERFACE)
            self.claimed = True
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        self.log(f"USB cleanup: claimed={self.claimed}, detached={self.detached}.")
        operations = []
        if self.claimed:
            operations.append(
                lambda: self.usb.util.release_interface(self.dev, INTERFACE)
            )
        if self.detached:
            operations.append(lambda: self.dev.attach_kernel_driver(INTERFACE))
        operations.append(lambda: self.usb.util.dispose_resources(self.dev))
        for operation in operations:
            try:
                operation()
            except Exception as exc:
                print(
                    f"⚠️ Warning: USB cleanup failed ({exc}); unplug/reconnect the camera.",
                    file=sys.stderr,
                )
        self.claimed = self.detached = False

    def _write(self, data):
        if self.verbose:
            self.log(f"TX ep=0x{EP_OUT:02X} {len(data)} bytes: {data.hex(' ')}")
        written = self.dev.write(EP_OUT, data, timeout=self.timeout_ms)
        self.log(f"TX accepted {written}/{len(data)} bytes.")
        if written != len(data):
            raise DumpError(
                f"Short USB write: {written}/{len(data)} bytes. Power-cycle before retrying."
            )

    def _read_exact(self, length):
        data = bytearray()
        while len(data) < length:
            self.log(f"RX request ep=0x{EP_IN:02X} {length - len(data)} bytes.")
            chunk = self.dev.read(EP_IN, length - len(data), timeout=self.timeout_ms)
            if self.verbose:
                payload = f": {bytes(chunk).hex(' ')}" if self.trace else ""
                self.log(f"RX ep=0x{EP_IN:02X} {len(chunk)} bytes{payload}")
            if not chunk:
                raise DumpError(
                    f"Short USB read: {len(data)}/{length} bytes. Power-cycle before retrying."
                )
            data.extend(chunk)
        if len(data) != length:
            raise DumpError(
                "USB returned more data than requested; power-cycle before retrying."
            )
        return bytes(data)

    def _command(self, buffer, length, callback=CALLBACK_SKIP, offset=0, payload=None):
        if not self.claimed:
            raise DumpError("USB interface has not been claimed.")
        direction = 0x80 if payload is None else 0x00
        if payload is not None and payload != RAM_BLOBS.get(buffer):
            raise DumpError(
                "Safety check: RAM payload differs from the fixed scratch blob."
            )
        self.tag = (self.tag + 1) & 0xFFFFFFFF
        cbw = build_cbw(self.tag, buffer, length, direction, callback, offset)
        self.log(
            f"CBW tag={self.tag} opcode=0xCD fn=0x{MEMORY_TRANSFER_FN:08X} "
            f"direction={'IN/read' if payload is None else 'OUT/RAM write'} "
            f"buffer=0x{buffer:08X} length={length} callback=0x{callback:08X} arg=0x{offset:06X}"
        )
        if self.verbose:
            execution = {
                CALLBACK_SKIP: "callback skipped; stock memory transfer only",
                CACHE_CLEAN_FN: f"cache_clean(0x{buffer:08X}, {length}) after RAM write",
                CANARY_ADDR: "RAM canary: l.jr r9 (return without changing the buffer)",
                SHIM_ADDR: f"SPI-read adapter -> SPI_read@0x0203D38C(0x{offset:06X}, 0x{buffer:08X}, {length})",
            }
            self.log(f"Requesting camera execution: {execution[callback]}.")
        self._write(cbw)
        if payload is None:
            data = self._read_exact(length)
        else:
            self._write(payload)
            data = b""
        csw = self._read_exact(13)
        signature, tag, residue, status = struct.unpack("<4sIIB", csw)
        self.log(
            f"CSW signature={signature!r} tag={tag} residue={residue} status={status}"
        )
        if signature != b"USBS" or tag != self.tag or residue != 0 or status != 0:
            raise DumpError(
                f"Invalid USB command status (signature={signature!r}, tag={tag}, "
                f"expected={self.tag}, residue={residue}, status={status}). "
                "The transport is out of sync or the command failed; power-cycle before retrying."
            )
        return data

    def read_memory(self, address, length):
        return self._command(address, length)

    def _load_scratch(self, address):
        blob = RAM_BLOBS[address]
        self._command(address, len(blob), CACHE_CLEAN_FN, payload=blob)
        if self.read_memory(address, len(blob)) != blob:
            raise DumpError(
                f"RAM scratch verification failed at 0x{address:08X}. "
                "Refusing to execute RAM code; power-cycle before retrying."
            )
        self.log(
            f"RAM scratch at 0x{address:08X}: {len(blob)} bytes read back identically."
        )

    def canary(self):
        """Probe RAM execution through the stock USB read callback.

        Upload ``l.jr r9`` at CANARY_ADDR and CANARY_PATTERN into the staging
        buffer; _load_scratch cache-cleans and verifies each upload by read-back.
        Then request a buffer read with CANARY_ADDR as its callback. The stock
        USB helper calls the canary first: it returns through the link register
        r9 without changing the buffer, which the helper then sends to the host.

        Return the received bytes; read_dump requires an exact CANARY_PATTERN
        match before loading the SPI adapter. This checks the chosen RAM entry
        point's execution/return path, not general RAM ownership or every cache
        condition. No flash writes occur. A hang or timeout may need a power-cycle.
        """
        self._load_scratch(CANARY_ADDR)
        self._load_scratch(BUFFER_ADDR)
        return self._command(BUFFER_ADDR, SPI_CHUNK, CANARY_ADDR)

    def load_shim(self):
        self._load_scratch(SHIM_ADDR)

    def flash_read(self, progress):
        data = bytearray()
        for offset in range(0, FLASH_SIZE, SPI_CHUNK):
            data.extend(self._command(BUFFER_ADDR, SPI_CHUNK, SHIM_ADDR, offset))
            progress(len(data), FLASH_SIZE)
        return bytes(data)


def check_firmware(dev):
    for address, expected, name in ROUTINE_SIGNATURES:
        if dev.verbose:
            dev.log(
                f"Checking {name} at 0x{address:08X}; expected {expected.hex(' ')}."
            )
        actual = dev.read_memory(address, len(expected))
        if actual != expected:
            raise DumpError(
                f"Unsupported firmware: {name} at 0x{address:08X} differs from "
                "the researched KC02 layout. Refusing to load RAM code. "
                "There is no safety override; use a SPI programmer for this camera."
            )


def validate_image(data):
    if len(data) != FLASH_SIZE:
        raise DumpError(
            f"Incomplete dump: received {len(data):,} of {FLASH_SIZE:,} bytes."
        )
    checks = (
        (data[4:8] == b"BLDR", "BLDR boot magic at offset 0x04 is missing"),
        (data[0x1FE:0x200] == b"\x55\xaa", "boot signature at offset 0x1FE is invalid"),
        (sum(data[:512]) % 256 == 0, "512-byte boot-header checksum is invalid"),
        (data[0x304:0x308] == b"SFAT", "SFAT marker at offset 0x304 is missing"),
    )
    for valid, reason in checks:
        if not valid:
            raise DumpError(
                f"Invalid firmware dump: {reason}. No output saved; power-cycle and retry."
            )


class Status:
    """Replace a task's line in terminals; emit only its result in regular logs."""

    def __init__(self, label, quiet=False, verbose=False, success=None, stream=None):
        self.label, self.quiet, self.verbose = label, quiet, verbose
        self.stream = stream if stream is not None else sys.stderr
        self.inline = self.stream.isatty() and not verbose
        self.success, self.text, self.width = success, label, 0

    def update(self, text, finished=False):
        self.text = text
        if self.quiet or (not self.inline and not self.verbose and not finished):
            return
        padding = " " * max(0, self.width - len(text)) if self.inline else ""
        print(
            ("\r" if self.inline else "") + text + padding,
            end="\n" if finished or not self.inline else "",
            file=self.stream,
            flush=True,
        )
        self.width = len(text)

    def __enter__(self):
        self.update(self.label + "...")
        return self

    def __exit__(self, exc_type, exc, tb):
        text = (
            (self.success or "✅ " + self.text.split(" ", 1)[1])
            if exc_type is None
            else "❌ " + self.label.split(" ", 1)[1] + " failed."
        )
        self.update(text, finished=True)


class Progress(Status):
    """A fixed 20-cell bar; success is shown only after the caller's checks pass."""

    def __init__(self, label, quiet=False, verbose=False):
        super().__init__(label, quiet, verbose)
        self.start = self.last = time.monotonic()
        self.percent = -10

    def __call__(self, done, total):
        if self.quiet or total <= 0:
            return
        now = time.monotonic()
        fraction = min(max(done / total, 0), 1)
        percent = int(100 * fraction)
        if (
            done < total
            and self.percent >= 0
            and (now - self.last < 0.1 if self.inline else percent < self.percent + 10)
        ):
            return
        elapsed = max(now - self.start, 0.001)
        rate = done / elapsed
        seconds = elapsed if done >= total else (total - done) / max(rate, 1)
        minutes, seconds = divmod(int(seconds), 60)
        timing = f"{'in' if done >= total else 'ETA'} {minutes:02d}:{seconds:02d}"
        filled = int(20 * fraction)
        bar = "▰" * filled + "▱" * (20 - filled)
        self.update(
            f"{self.label:<13} {bar} {percent:3d}%  {rate / 1024:.0f} KiB/s  {timing}"
        )
        self.last, self.percent = now, percent


def read_dump(dev, single_read=False, quiet=False, verbose=False):
    with Status(
        "🔍 Checking compatibility", quiet, verbose, "✅ Compatibility checked."
    ):
        check_firmware(dev)
        if verbose:
            print(
                "🧪 Checking RAM Execution (temporary RAM writes only; flash is not modified)...",
                file=sys.stderr,
                flush=True,
            )
        if dev.canary() != CANARY_PATTERN:
            raise DumpError(
                "RAM execution check failed. Refusing to run the SPI shim; power-cycle the camera."
            )
        if verbose:
            print(
                f"🧩 Loading SPI-read adapter at 0x{SHIM_ADDR:08X} ({len(SHIM_BYTES)} bytes)...",
                file=sys.stderr,
                flush=True,
            )
        dev.load_shim()  # Verifies the written bytes and cleans D-cache.
    with Progress("📥 Dumping", quiet, verbose) as progress:
        data = dev.flash_read(progress=progress)
        validate_image(data)
    if not single_read:
        with Progress("📥 Validating", quiet, verbose) as progress:
            verification = dev.flash_read(progress=progress)
            validate_image(verification)
            if data != verification:
                offset = next(
                    i for i, (a, b) in enumerate(zip(data, verification)) if a != b
                )
                raise DumpError(
                    f"The two flash reads differ (first mismatch at 0x{offset:06X}). "
                    "No output saved. Leave the camera idle, power-cycle and retry. "
                    "Camera counters can change; "
                    "--single-read skips comparison but gives less assurance."
                )
    else:
        print(
            "⚠️ Warning: second-read comparison skipped (--single-read).",
            file=sys.stderr,
        )
    return data


def prepare_output(path, force):
    if path.name.lower() == "destbin.bin":
        raise DumpError(
            "Do not name a backup DestBin.bin: the camera treats it as a firmware update."
        )
    if path.is_symlink():
        raise DumpError(f"Output is a symbolic link: {path}. Choose a regular file.")
    if path.exists() and (not force or not path.is_file()):
        raise DumpError(
            f"Output already exists: {path}. Choose another path or use --force for a regular file."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reserve a temporary file before USB operations: catches unwritable output
    # directories early, without truncating an existing backup.
    return tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    )


def save_dump(staged, path, data, force):
    validate_image(data)  # Never publish a partial or unverified image.
    if staged.write(data) != len(data):
        raise OSError(errno.EIO, "short write to temporary dump")
    staged.flush()
    os.fsync(staged.fileno())
    staged.close()  # Windows cannot rename an open temporary file.
    if force:
        os.replace(staged.name, path)
        return
    try:
        # Atomic no-clobber publication on filesystems supporting hard links.
        os.link(staged.name, path)
    except FileExistsError:
        raise DumpError(
            f"Output appeared during dumping: {path}. It was not overwritten."
        ) from None
    except OSError as exc:
        if exc.errno not in (
            errno.EPERM,
            errno.EOPNOTSUPP,
            errno.ENOSYS,
            errno.EXDEV,
            errno.EINVAL,
        ) and getattr(exc, "winerror", None) not in (1, 50, 1314):
            raise
        # FAT/exFAT cannot hard-link. Delete only a file we created, including
        # on short writes, disk errors or interruption; never delete a backup.
        created = False
        try:
            with path.open("xb", buffering=0) as output:
                created = True
                if output.write(data) != len(data):
                    raise OSError(errno.EIO, "short write to output dump")
                os.fsync(output.fileno())
        except BaseException:
            if created:
                try:
                    path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    print(
                        f"⚠️ Could not remove incomplete output {path}: {cleanup_error}",
                        file=sys.stderr,
                    )
            raise


def explain_error(exc):
    """Inspect USB causes, not just OS errno (libusb codes are portable)."""
    if isinstance(exc, DumpError):
        return str(exc)
    cause = exc
    while cause.__cause__ is not None:
        cause = cause.__cause__
    code = getattr(cause, "backend_error_code", None)
    number = getattr(cause, "errno", None)
    text = str(exc)
    lower = str(cause).lower()
    usb_failure = (
        code is not None
        or type(cause).__module__.startswith("usb.")
        or isinstance(cause, NotImplementedError)
    )
    if usb_failure:
        if (
            code == -3
            or number in (errno.EACCES, errno.EPERM)
            or "access" in lower
            or "permission" in lower
        ):
            return f"USB permission denied. See README.md for {platform.system()} permissions/driver instructions. Details: {text}"
        if code == -6 or number == errno.EBUSY or "busy" in lower:
            return f"Camera interface 4 is busy. Eject/unmount SD volumes, close camera apps and reconnect. See README.md for driver ownership guidance. Details: {text}"
        if code == -4 or number == errno.ENODEV:
            return f"Camera disconnected or powered off during USB access. Check power and cable, then reconnect and retry. Details: {text}"
        if code == -7 or number == errno.ETIMEDOUT or "timed out" in lower:
            return f"Camera did not respond before the USB timeout. Power-cycle, use USB/SD mode and a direct USB data cable, then retry. If needed increase --timeout. No automatic retry/reset is performed. Details: {text}"
        if code == -5:
            return f"Camera interface/configuration is unavailable. On Windows this can mean the KC02 mass-storage interface lacks a WinUSB driver. Reconnect in USB/SD mode; see README.md. Details: {text}"
        if code == -12 or isinstance(cause, NotImplementedError):
            return f"USB operation/driver is not supported on this host. On Windows, install WinUSB for the KC02 mass-storage interface; on macOS a kernel driver may own it. See README.md. Details: {text}"
        if code == -9:
            return f"Camera stalled the USB endpoint. It may be in the wrong mode or have incompatible firmware. Power-cycle before retrying; do not resume this transfer. Details: {text}"
        return f"USB communication failed. Power-cycle the camera before retrying; see README.md for host instructions. Details: {text}"
    if isinstance(exc, OSError):
        if exc.errno in (errno.EACCES, errno.EPERM):
            return f"Cannot write the output location: permission denied ({exc}). Choose a writable path with -o."
        if exc.errno == errno.ENOSPC:
            return "Not enough disk space to save the dump. Free at least 4 MiB (8 MiB on filesystems without hard links) and retry."
        return f"Output/file operation failed: {exc}. Check the destination path, permissions and free space."
    return f"Dump failed: {exc}. Power-cycle before retrying; no flash-write commands were sent."


def positive_int(text):
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def build_parser():
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            super().error(f"❌ {message}")

    parser = Parser(
        description="📷 Back up the complete 4 MiB HiMont KC02 firmware over USB, without writing flash.",
        epilog="🔌 Connect/power on and leave the camera idle on the USB/SD screen. The SD card can stay inserted. See README.md for USB permissions/drivers.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list connected KC02s without claiming interfaces",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("firmware/original.bin"),
        help="backup path (default: firmware/original.bin, relative to current directory)",
    )
    parser.add_argument(
        "--device",
        type=positive_int,
        metavar="NUMBER",
        help="camera number from --list (1-based; numbering can change on reconnect)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file only after a successful dump",
    )
    parser.add_argument(
        "--single-read",
        action="store_true",
        help="skip the second full read/compare (faster, less assurance)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=5000,
        metavar="MS",
        help="USB timeout per transfer in milliseconds (default: 5000)",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="hide progress/status (errors, warnings and final result remain visible)",
    )
    output.add_argument(
        "-v",
        "--verbose",
        dest="verbosity",
        action="store_const",
        const=1,
        default=0,
        help="log USB commands, sent bytes, received lengths/status and camera-side execution",
    )
    output.add_argument(
        "--trace",
        dest="verbosity",
        action="store_const",
        const=2,
        help="include verbose diagnostics plus raw RX payloads",
    )
    return parser


def main(argv=None):
    # Keep Unicode status output readable in Windows pipes as well as consoles.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    verbose = args.verbosity >= 1
    trace = args.verbosity >= 2
    staged = None
    saved = False
    try:
        if not args.list:
            staged = prepare_output(args.output, args.force)
        usb, backend = load_usb()
        cameras, webcams = find_cameras(usb, backend)
        if args.list:
            for i, dev in enumerate(cameras, 1):
                print(f"📷 {i}: {device_label(dev)} [✅ supports dumping]")
            for dev in webcams:
                print(f"📷 -: {device_label(dev)} [❌ webcam-only; cannot dump]")
            if not cameras and not webcams:
                select_camera(cameras, webcams, None)  # helpful not-found error
            return 0
        camera = select_camera(cameras, webcams, args.device)
        if not args.quiet:
            print(
                f"📷 Camera: {device_label(camera)}\n🔌 Keep it connected until reading finishes.",
                file=sys.stderr,
                flush=True,
            )
        with CameraTransport(usb, camera, args.timeout, verbose, trace) as dev:
            data = read_dump(dev, args.single_read, args.quiet, verbose)
        summary = f"✅ Saved {len(data):,} bytes to {args.output}"
        with Status(
            f"💾 Saving to {args.output}",
            args.quiet,
            verbose,
            summary,
            stream=sys.stdout,
        ):
            save_dump(staged, args.output, data, args.force)
            saved = True
        if args.quiet:
            print(summary)
        if verbose:
            print(f"🔐 SHA-256: {hashlib.sha256(data).hexdigest()}", flush=True)
        return 0
    except KeyboardInterrupt:
        status = (
            f"The verified dump is at {args.output}."
            if saved
            else "No dump completed; check the output path."
        )
        print(
            f"\n❌ Error: interrupted. {status} Power-cycle the camera before retrying.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(f"\n❌ Error: {explain_error(exc)}", file=sys.stderr)
        return 1
    finally:
        if staged is not None:
            try:
                staged.close()
            except OSError as exc:
                print(
                    f"⚠️ Warning: could not close temporary file {staged.name}: {exc}",
                    file=sys.stderr,
                )
            try:
                Path(staged.name).unlink(missing_ok=True)
            except OSError as exc:
                print(
                    f"⚠️ Warning: could not remove temporary file {staged.name}: {exc}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    sys.exit(main())
