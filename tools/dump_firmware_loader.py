#!/usr/bin/env python3
"""Back up matched Video050Loader cameras through the factory USB loader.

Initial host support is macOS. No application-layout addresses are used.
The exact external ThunderSE.elf is required and SHA-256 checked before USB.
Only a fixed SRAM upload, SPI initialization, JEDEC ID and SPI reads are used.
Two fresh full-chip reads must match. A failed command stops without replay.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import time

import dump_firmware as application_dumper

usb = None  # Loaded lazily; --help and source-only inspection need no PyUSB.
APP_ID = (0x0219, 0x3280)
CANDIDATE_IDS = {APP_ID, (0x1908, 0x3283), (0x1908, 0x3282), (0x1908, 0x3319)}


SOURCE = "https://github.com/Cool2Feel/Dwl_ISP/tree/a76bb27ed569984b0f2f801ef431c0aea2aae45d"
DRIVER_SHA256 = "85e9a4c512352b8e451af9e430384b8ceac120cb65ca2e03319b30284637434e"
MEMORY_API = 0x00100008  # SHN_ABS RBC_mem_rwex_CPU/DMA in exact driver ELF.
SPI_INIT = 0x24
SIGNAL_DRIVE = 0x74
NO_CALLBACK = 0xFFFFFFFF

# Manufacturer/type/density entries from the pinned FlashLib.ini, restricted
# to parts explicitly using 0x03 READ and 0x05 status, with 3-byte addresses.
NOR_CAPACITIES = {
    bytes.fromhex("0b4015"): 0x200000,
    bytes.fromhex("5e4015"): 0x200000,
    bytes.fromhex("e04015"): 0x200000,
    bytes.fromhex("c22015"): 0x200000,
    bytes.fromhex("5e6014"): 0x100000,
    bytes.fromhex("856014"): 0x100000,
    bytes.fromhex("c22013"): 0x80000,
    bytes.fromhex("5e4016"): 0x400000,
    bytes.fromhex("ef4017"): 0x800000,
    bytes.fromhex("f83217"): 0x800000,
    bytes.fromhex("ef4015"): 0x200000,
    bytes.fromhex("ef4016"): 0x400000,
    bytes.fromhex("5e3214"): 0x100000,
    bytes.fromhex("856016"): 0x400000,
    bytes.fromhex("c84015"): 0x200000,
    bytes.fromhex("c84016"): 0x400000,
    bytes.fromhex("0b4016"): 0x400000,
    bytes.fromhex("c22016"): 0x400000,
    bytes.fromhex("852016"): 0x400000,
    bytes.fromhex("ef4018"): 0x1000000,
}


def load_backend():
    global usb
    usb, backend = application_dumper.load_usb()
    return backend


def descendants(value):
    if isinstance(value, dict):
        yield value
        for child in value.get("IORegistryEntryChildren", []):
            yield from descendants(child)
    elif isinstance(value, list):
        for child in value:
            yield from descendants(child)


def camera_records(registry):
    records = []
    seen = set()
    for node in descendants(registry):
        identity = (node.get("idVendor"), node.get("idProduct"))
        if identity not in CANDIDATE_IDS or "bcdDevice" not in node:
            continue
        key = (*identity, node.get("locationID"))
        if key in seen:
            continue
        seen.add(key)
        interfaces = set()
        disks = set()
        for child in descendants(node):
            if "bInterfaceNumber" in child:
                interfaces.add(tuple(child.get(k) for k in (
                    "bInterfaceNumber", "bInterfaceClass",
                    "bInterfaceSubClass", "bInterfaceProtocol")))
            bsd = child.get("BSD Name", "")
            if bsd.startswith("disk"):
                disks.add(bsd)
        records.append({
            "usb_id": f"{identity[0]:04x}:{identity[1]:04x}",
            "product": node.get("USB Product Name", node.get("IORegistryEntryName")),
            "vendor": node.get("USB Vendor Name"),
            "device_revision": node.get("bcdDevice"),
            "interfaces": [dict(zip(("number", "class", "subclass", "protocol"), i))
                           for i in sorted(interfaces, key=repr)],
            "disks": sorted(disks),
            "firmware_compatibility": "unconfirmed; USB identity is insufficient",
        })
    return records


def registry_cameras():
    result = subprocess.run(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOUSBHostDevice", "-l"],
                            check=True, capture_output=True, timeout=10)
    return camera_records(plistlib.loads(result.stdout))


def unmount_camera_card():
    cameras = registry_cameras()
    if len(cameras) != 1:
        raise RuntimeError("Cannot uniquely map the camera to its card")
    disks = cameras[0]["disks"]
    whole = [d for d in disks if d.startswith("disk") and d[4:].isdigit()]
    if len(whole) != 1:
        raise RuntimeError("Expected exactly one camera card disk")
    for disk in disks:
        result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", disk],
                                check=True, capture_output=True, timeout=10)
        info = plistlib.loads(result.stdout)
        if info.get("Internal"):
            raise RuntimeError("Refusing to unmount an internal disk")
        mount = info.get("MountPoint")
        if mount:
            if any(p.name.lower() == "destbin.bin" for p in Path(mount).iterdir()):
                raise RuntimeError("Card contains an update filename; stop before device re-enumeration")
    result = subprocess.run(["/usr/sbin/diskutil", "unmountDisk", whole[0]],
                            check=True, capture_output=True, text=True, timeout=15)

    def remount():
        # Capture/release can change the disk number. Resolve it again from the camera.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            candidates = registry_cameras()
            if len(candidates) == 1:
                fresh = [d for d in candidates[0]["disks"] if d.startswith("disk") and d[4:].isdigit()]
                if len(fresh) == 1:
                    r = subprocess.run(["/usr/sbin/diskutil", "mountDisk", fresh[0]],
                                       capture_output=True, text=True, timeout=15)
                    if r.returncode:
                        raise RuntimeError("Camera card remount failed: " + r.stderr.strip())
                    return r.stdout.strip()
            time.sleep(0.25)
        raise RuntimeError("Camera card did not reappear; reconnect the camera")

    return result.stdout.strip(), remount


def cbw(tag, opcode):
    """Only INQUIRY and the zero-data loader switch can be constructed."""
    if opcode == 0x12:
        cdb, length, cdb_length = bytes([0x12, 0, 0, 0, 36, 0]), 36, 6
    elif opcode == 0xDA:
        cdb, length, cdb_length = bytes([0xDA]), 0, 16
    else:
        raise ValueError("Command not permitted by this mode-only probe")
    return struct.pack("<4sIIBBB", b"USBC", tag, length, 0x80, 0, cdb_length) + cdb.ljust(16, b"\0")


def command(dev, tag, opcode, report):
    packet = cbw(tag, opcode)
    length = struct.unpack_from("<I", packet, 8)[0]
    entry = {"opcode": f"0x{opcode:02x}", "cbw_hex": packet.hex(), "stage": "bulk OUT CBW"}
    report.setdefault("commands", []).append(entry)
    try:
        sent = dev.write(0x01, packet, timeout=5000)
        entry["cbw_bytes_sent"] = sent
        if sent != 31:
            raise RuntimeError("Short CBW write")
        data = b""
        if length:
            entry["stage"] = "bulk IN data"
            data = bytes(dev.read(0x81, length, timeout=5000))
            entry["data_hex"] = data.hex()
            if len(data) != length:
                raise RuntimeError("Short data response")
        entry["stage"] = "bulk IN CSW"
        raw = bytes(dev.read(0x81, 13, timeout=5000))
        entry["csw_hex"] = raw.hex()
        if len(raw) != 13:
            raise RuntimeError("Short CSW")
        sig, got_tag, residue, status = struct.unpack("<4sIIB", raw)
        if (sig, got_tag, residue, status) != (b"USBS", tag, 0, 0):
            raise RuntimeError(f"Invalid CSW: signature={sig!r}, tag={got_tag}, residue={residue}, status={status}")
        entry["stage"] = "completed"
        return data
    except Exception as exc:
        entry["error"] = str(exc)
        raise


def enter_loader(backend, report):
    if list(usb.core.find(find_all=True, backend=backend, idVendor=0x1908, idProduct=0x3319)):
        raise RuntimeError("A bootloader is already present; use --from-loader or disconnect it")
    devices = list(usb.core.find(find_all=True, backend=backend,
                                idVendor=APP_ID[0], idProduct=APP_ID[1]))
    if len(devices) != 1:
        raise RuntimeError(f"Expected exactly one 0219:3280 camera, found {len(devices)}")
    dev = devices[0]
    config = dev.get_active_configuration()
    interfaces = [i for i in config if i.bInterfaceNumber == 4 and i.bAlternateSetting == 0]
    if len(interfaces) != 1:
        raise RuntimeError("Expected only one interface 4 alternate 0")
    interface = interfaces[0]
    if (interface.bInterfaceClass, interface.bInterfaceSubClass, interface.bInterfaceProtocol) != (8, 6, 80):
        raise RuntimeError("Camera is not in USB mass-storage BOT mode")
    endpoints = {e.bEndpointAddress: e for e in interface}
    if set(endpoints) != {0x01, 0x81} or any(usb.util.endpoint_type(e.bmAttributes) != 2 for e in endpoints.values()):
        raise RuntimeError("Unexpected bulk endpoints")
    claimed = detached = switched = False
    remount = None
    try:
        report["unmount"], remount = unmount_camera_card()
        if dev.is_kernel_driver_active(4):
            dev.detach_kernel_driver(4)
            detached = True
            report["driver_detached"] = True
        usb.util.claim_interface(dev, 4)
        claimed = True
        report["interface_claimed"] = 4
        data = command(dev, 0x48434C41, 0x12, report)
        identity = {"vendor": data[8:16].decode("ascii", "replace").strip(),
                    "product": data[16:32].decode("ascii", "replace").strip(),
                    "revision": data[32:36].decode("ascii", "replace").strip()}
        report["inquiry"] = identity
        if (identity["vendor"].lower(), identity["product"]) != ("buildwin", "Media-Player"):
            raise RuntimeError("Unexpected SCSI identity; refusing mode switch")
        # Set before transmission: after an error the device may already be
        # changing mode. Never replay the request or reattach the stale handle.
        switched = True
        report["mode_switch_attempted"] = True
        command(dev, 0x48434C42, 0xDA, report)
        report["mode_switch_csw_valid"] = True
        report["mode_switch_sent_once"] = True
    finally:
        cleanup = []
        if claimed:
            cleanup.append(("release_interface", lambda: usb.util.release_interface(dev, 4)))
        if detached and not switched:
            cleanup.append(("restore_driver", lambda: dev.attach_kernel_driver(4)))
        cleanup.append(("dispose_handle", lambda: usb.util.dispose_resources(dev)))
        if remount and not switched:
            cleanup.append(("remount_card", remount))
        for name, action in cleanup:
            try:
                action()
                report[name] = "completed"
            except Exception as exc:
                report.setdefault("cleanup_errors", []).append({"step": name, "error": str(exc)})
        if switched:
            report["recovery"] = "Loader mode is temporary. Power-cycle/reconnect to restore normal application/card mode. No automatic reset or retry."
    if switched:
        observations = []
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            loaders = list(usb.core.find(find_all=True, backend=backend,
                                         idVendor=0x1908, idProduct=0x3319))
            if len(loaders) > 1:
                raise RuntimeError("Multiple bootloaders present; cannot select uniquely")
            if loaders:
                # Retain this exact device object for the caller. Repeated
                # product-string queries open/discard handles and are avoided.
                loader = loaders[0]
                record = {"usb_id": "1908:3319", "bus": loader.bus,
                          "address": loader.address, "revision": loader.bcdDevice}
                observations.append({"recorded_utc": datetime.now(timezone.utc).isoformat(),
                                     "devices": [record]})
                report["reenumeration"] = observations
                report["after"] = [record]
                report["loader_observed"] = True
                return loader
            time.sleep(0.25)
        report["reenumeration"] = observations
        report["after"] = []
        report["loader_observed"] = False
        raise RuntimeError("Bootloader did not appear within 15 seconds; no retry")


def read_driver(path):
    """Check exact provenance, ELF structure, load segment, and required symbols."""
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != DRIVER_SHA256:
        raise ValueError("Factory driver SHA-256 differs from reviewed pin")
    h = struct.unpack_from("<16sHHIIIIIHHHHHH", data)
    if data[:7] != b"\x7fELF\x01\x01\x01" or h[2] != 92:
        raise ValueError("Expected little-endian ELF32 OpenRISC driver")
    ph = [struct.unpack_from("<IIIIIIII", data, h[5] + i * h[9]) for i in range(h[10])]
    loads = [p for p in ph if p[0] == 1]
    if len(loads) != 1 or loads[0][1:6] != (8192, 0, 0, 3840, 3840):
        raise ValueError("Unexpected SRAM driver load segment")
    sh = [struct.unpack_from("<IIIIIIIIII", data, h[6] + i * h[11]) for i in range(h[12])]
    symbols = {}
    for section in sh:
        if section[1] != 2:
            continue
        strings = sh[section[6]]
        names = data[strings[4]:strings[4] + strings[5]]
        for offset in range(section[4], section[4] + section[5], section[9]):
            name, value, _size, _info, _other, _index = struct.unpack_from("<IIIBBH", data, offset)
            end = names.find(b"\0", name)
            symbols[names[name:end].decode("ascii")] = value
    required = {"RBC_mem_rwex_CPU": MEMORY_API, "RBC_mem_rwex_DMA": MEMORY_API,
                "l1_func_spi_init": SPI_INIT, "l1_func_signal_drive": SIGNAL_DRIVE}
    if any(symbols.get(name) != value for name, value in required.items()):
        raise ValueError("Required factory loader symbols differ")
    return data[8192:8192 + 3840], required


def memory_cdb(address, length, write=False):
    # Only the exact driver-owned SRAM upload/readback chunks are allowed.
    if not (0 <= address < 3840 and address % 512 == 0 and
            length == min(512, 3840 - address)):
        raise ValueError("Memory operation outside reviewed driver SRAM")
    return bytes([0xCB]) + struct.pack("<III", MEMORY_API, address, NO_CALLBACK) + bytes(3)


def spi_cdb(opcode, offset=0):
    if opcode == 0x9F:
        if offset != 0:
            raise ValueError("JEDEC ID takes no address")
        ctrl, si_length = 3, 1
    elif opcode == 0x03 and 0 <= offset < 0x1000000 and offset % 512 == 0:
        ctrl, si_length = 7, 4
    else:
        raise ValueError("Only JEDEC-ID and aligned flash-read commands are permitted")
    return (bytes([0xCB]) + struct.pack("<I", SIGNAL_DRIVE) +
            bytes([0, ctrl, si_length, opcode]) + offset.to_bytes(3, "big") + bytes(4))


def capacity_from_jedec(data):
    if len(data) != 4 or data[:3] not in NOR_CAPACITIES:
        raise ValueError("JEDEC manufacturer/type/density has no reviewed NOR profile; capacity is unconfirmed")
    return NOR_CAPACITIES[data[:3]]


def validate_flash(data):
    checks = {"bldr_magic": data[4:8] == b"BLDR",
              "boot_signature": data[0x1FE:0x200] == b"\x55\xAA",
              "sfat_magic": data[0x304:0x308] == b"SFAT"}
    if not all(checks.values()):
        raise ValueError("Flash data does not have the expected Buildwin headers: " + repr(checks))
    # The upstream v1 zero-sum header check is not universal across firmware.
    return {**checks, "header_sum_mod_256": sum(data[:512]) % 256}


class Transport:
    def __init__(self, dev, report):
        self.dev, self.report = dev, report
        self.tag = 0
        self.failed = False

    def transaction(self, cdb, length=0, payload=None, note=""):
        if self.failed:
            raise RuntimeError("Transport already failed; refusing further commands")
        if not 1 <= len(cdb) <= 16 or length < 0 or (payload is not None and len(payload) != length):
            raise ValueError("Invalid command framing")
        self.tag += 1
        direction = 0x80 if payload is None else 0
        cbw = struct.pack("<4sIIBBB", b"USBC", self.tag, length, direction, 0, len(cdb)) + cdb.ljust(16, b"\0")
        entry = {"tag": self.tag, "note": note, "cbw_hex": cbw.hex(), "stage": "CBW OUT"}
        # Keep detailed setup and one failed command, without 16k success entries.
        retain = self.tag <= 32 or note.startswith("flash offset 0x000000")
        try:
            sent = self.dev.write(0x01, cbw, timeout=5000)
            entry["cbw_bytes_sent"] = sent
            if sent != 31:
                raise RuntimeError("Short CBW write")
            data = b""
            if payload is not None and length:
                entry["stage"] = "payload OUT"
                if self.dev.write(0x01, payload, timeout=5000) != length:
                    raise RuntimeError("Short SRAM payload write")
            elif length:
                entry["stage"] = "data IN"
                data = bytes(self.dev.read(0x81, length, timeout=5000))
                entry["data_bytes"] = len(data)
                if len(data) == 13 and data[:4] == b"USBS":
                    entry["early_csw_hex"] = data.hex()
                    raise RuntimeError("Device returned status instead of requested data")
                if len(data) != length:
                    raise RuntimeError(f"Short data phase: expected {length}, received {len(data)}")
            entry["stage"] = "CSW IN"
            raw = bytes(self.dev.read(0x81, 13, timeout=5000))
            entry["csw_hex"] = raw.hex()
            if len(raw) != 13:
                raise RuntimeError("Short CSW")
            sig, tag, residue, status = struct.unpack("<4sIIB", raw)
            if (sig, tag, residue, status) != (b"USBS", self.tag, 0, 0):
                raise RuntimeError(f"Invalid CSW: signature={sig!r}, tag={tag}, residue={residue}, status={status}")
            entry["stage"] = "completed"
            return data
        except Exception as exc:
            self.failed = True
            retain = True
            entry["error"] = str(exc)
            raise
        finally:
            if retain:
                self.report.setdefault("transactions", []).append(entry)
            self.report["transaction_count"] = self.tag


def run(args, report):
    driver, symbols = read_driver(args.driver)
    report["factory_driver"] = {"path": str(args.driver), "sha256": DRIVER_SHA256,
                                "sram_bytes": len(driver), "symbols": symbols}
    backend = load_backend()
    if backend is None:
        raise RuntimeError("libusb backend unavailable")
    loader_dev = None
    if getattr(args, "enter_loader", False):
        transition = {"source": SOURCE, "scope": "INQUIRY plus one 0xDA mode switch; no flash/card writes"}
        report["mode_transition"] = transition
        loader_dev = enter_loader(backend, transition)
        if not transition.get("loader_observed"):
            raise RuntimeError("Factory bootloader was not observed after mode switch")
    devices = [loader_dev] if loader_dev is not None else list(
        usb.core.find(find_all=True, backend=backend, idVendor=0x1908, idProduct=0x3319))
    if len(devices) != 1:
        raise RuntimeError(f"Expected exactly one 1908:3319 bootloader, found {len(devices)}")
    dev = devices[0]
    claimed = False
    try:
        report["usb_identity"] = {"vid_pid": "1908:3319", "manufacturer": dev.manufacturer,
                                  "product": dev.product, "revision": dev.bcdDevice}
        if (dev.manufacturer, dev.product) != ("BUILDWIN", "BLDR v1.00"):
            raise RuntimeError("Unexpected USB loader identity")
        config = dev.get_active_configuration()
        interfaces = list(config)
        if len(interfaces) != 1:
            raise RuntimeError("Expected exactly one loader interface")
        interface = interfaces[0]
        if (interface.bInterfaceNumber, interface.bAlternateSetting, interface.bInterfaceClass,
            interface.bInterfaceSubClass, interface.bInterfaceProtocol) != (0, 0, 8, 6, 80):
            raise RuntimeError("Unexpected loader interface layout")
        endpoints = {e.bEndpointAddress: e for e in interface}
        if set(endpoints) != {0x01, 0x81} or any(usb.util.endpoint_type(e.bmAttributes) != 2 for e in endpoints.values()):
            raise RuntimeError("Unexpected loader bulk endpoints")
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
            report["driver_detached"] = True
        usb.util.claim_interface(dev, 0)
        claimed = True
        transport = Transport(dev, report)
        inquiry = transport.transaction(bytes([0x12, 0, 0, 0, 36, 0]), 36, note="loader SCSI INQUIRY")
        report["inquiry_hex"] = inquiry.hex()
        vendor, product, rev = (inquiry[8:16].decode("ascii").strip(),
                                inquiry[16:32].decode("ascii").strip(),
                                inquiry[32:36].decode("ascii").strip())
        report["scsi_identity"] = {"vendor": vendor, "product": product, "revision": rev}
        if inquiry[8:36] != b"BuildWinVideo050Loader  1.00":
            raise RuntimeError("Loader SCSI identity has no reviewed driver profile; stopping")
        # Exact live SCSI identity selects this factory SRAM layout. Data reads
        # of ROM are not a compatibility guard: the family can map its ROM
        # prefix to zeros. Preserve SRAM first, then verify every uploaded byte
        # through the published memory API before executing the driver.
        original_sram = bytearray()
        for offset in range(0, len(driver), 512):
            length = min(512, len(driver) - offset)
            original_sram.extend(transport.transaction(memory_cdb(offset, length), length,
                                                       note=f"preserve loader driver SRAM 0x{offset:04x}"))
        snapshot = args.output_dir / "sram-before.bin"
        with snapshot.open("xb") as handle:
            handle.write(original_sram)
        report["sram_before_upload"] = {"path": str(snapshot), "bytes": len(original_sram),
                                        "sha256": hashlib.sha256(original_sram).hexdigest()}
        for offset in range(0, len(driver), 512):
            chunk = driver[offset:offset + 512]
            transport.transaction(memory_cdb(offset, len(chunk), write=True), len(chunk), chunk,
                                  note=f"SRAM driver upload 0x{offset:04x}")
        for offset in range(0, len(driver), 512):
            chunk = driver[offset:offset + 512]
            actual = transport.transaction(memory_cdb(offset, len(chunk)), len(chunk),
                                           note=f"SRAM driver readback 0x{offset:04x}")
            if actual != chunk:
                raise RuntimeError("Driver SRAM verification failed; refusing execution")
        report["driver_sram_verified"] = True
        init = bytes([0xCB]) + struct.pack("<III", SPI_INIT, 0, 0) + bytes(3)
        transport.transaction(init, 0, payload=b"", note="initialize factory SPI pins/controller only")
        time.sleep(0.01)
        jedec = transport.transaction(spi_cdb(0x9F), 4, note="SPI JEDEC-ID read")
        capacity = capacity_from_jedec(jedec)
        report["flash"] = {"jedec_hex": jedec.hex(), "capacity_bytes": capacity,
                           "capacity_method": "Exact manufacturer/type/density profile in pinned FlashLib.ini; no capacity fallback"}
        print(f"Flash ID {jedec.hex(' ')}, capacity {capacity:,} bytes", flush=True)
        reads = []
        for number in (1, 2):
            path = args.output_dir / f"read{number}.bin"
            record = {"path": str(path), "bytes": 0, "verified": False}
            report.setdefault("reads", []).append(record)
            digest = hashlib.sha256()
            data = bytearray()
            with path.open("xb") as handle:
                for offset in range(0, capacity, 512):
                    chunk = transport.transaction(spi_cdb(0x03, offset), 512,
                                                  note=f"flash offset 0x{offset:06x}, read {number}")
                    handle.write(chunk)
                    digest.update(chunk)
                    data.extend(chunk)
                    record["bytes"] += len(chunk)
                    if record["bytes"] % 0x40000 == 0:
                        print(f"Read {number}: {record['bytes']:,}/{capacity:,} bytes", flush=True)
                handle.flush()
                os.fsync(handle.fileno())
            record["sha256"] = digest.hexdigest()
            record["headers"] = validate_flash(data)
            reads.append(bytes(data))
        if reads[0] != reads[1] or len(reads[0]) != capacity:
            raise RuntimeError("Full flash reads do not match; files are unverified")
        for record in report["reads"]:
            record["verified"] = True
        report["two_full_reads_match"] = True
        report["result"] = "Two independent full-chip SPI reads over USB match byte for byte."
        print(report["result"], flush=True)
    finally:
        if claimed:
            try:
                usb.util.release_interface(dev, 0)
            except Exception as exc:
                report.setdefault("cleanup_errors", []).append(str(exc))
        try:
            usb.util.dispose_resources(dev)
        except Exception as exc:
            report.setdefault("cleanup_errors", []).append(str(exc))
        report["recovery"] = "Power-cycle/reconnect to return from temporary loader mode to normal camera/card mode. No automatic reset or retries."


def validate_output_dir(path):
    """Refuse an existing output directory or one on the camera card."""
    if path.exists() or path.is_symlink():
        raise ValueError("Output directory already exists; choose a new directory")
    target = path.resolve()
    for camera in registry_cameras():
        for disk in camera["disks"]:
            result = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", disk],
                                    check=True, capture_output=True, timeout=10)
            info = plistlib.loads(result.stdout)
            mount = info.get("MountPoint")
            if mount:
                volume = Path(mount).resolve()
                if target == volume or volume in target.parents:
                    raise ValueError("Save backups to the host, not the camera card")


def return_output_ownership(path):
    """Keep the private output readable by the user who invoked sudo."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None or gid is None or not uid.isdecimal() or not gid.isdecimal():
        return
    for name in ("read1.bin", "read2.bin", "sram-before.bin", "report.json"):
        file = path / name
        if file.exists():
            os.chown(file, int(uid), int(gid), follow_symlinks=False)
    os.chown(path, int(uid), int(gid), follow_symlinks=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enter-loader", action="store_true",
                      help="unmount this camera card and send one 0xDA mode switch")
    mode.add_argument("--from-loader", action="store_true",
                      help="use a camera already in the exact supported loader mode")
    parser.add_argument("--driver", type=Path, required=True,
                        help="path to the pinned external ThunderSE.elf; see docs/USB_LOADER_BACKUP.md")
    parser.add_argument("--output-dir", type=Path,
                        help="new host directory for both reads, SRAM snapshot and report")
    parser.add_argument("--firmware-version", help="optional version-screen text to record locally")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("This initial implementation supports macOS only; see the protocol notes")
    if os.geteuid() != 0:
        parser.error("Run from Terminal with sudo to capture the Mac storage driver")
    if args.output_dir is None:
        args.output_dir = Path("firmware/loader-backups") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"recorded_utc": datetime.now(timezone.utc).isoformat(),
              "firmware_version": args.firmware_version, "source": SOURCE,
              "scope": "Temporary SRAM driver and two full SPI reads; no flash/card write commands",
              "two_full_reads_match": False}
    # Complete local validation before creating output or touching the device.
    try:
        read_driver(args.driver)
        validate_output_dir(args.output_dir)
        args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    except Exception as exc:
        print("Stopped before USB: " + str(exc), file=sys.stderr)
        return 1
    code = 0
    try:
        run(args, report)
    except KeyboardInterrupt:
        report["error"] = "Interrupted; incomplete reads are not verified backups"
        code = 130
    except Exception as exc:
        report.update(error=str(exc), error_type=type(exc).__name__)
        print("Stopped: " + str(exc), file=sys.stderr)
        code = 1
    finally:
        path = args.output_dir / "report.json"
        try:
            with path.open("x") as handle:
                json.dump(report, handle, indent=2)
                handle.write("\n")
            print("Report: " + str(path), flush=True)
            recovery = report.get("recovery") or report.get("mode_transition", {}).get("recovery")
            if recovery:
                print(recovery, flush=True)
        except OSError as exc:
            print("Could not save report: " + str(exc), file=sys.stderr)
            code = 1
        try:
            return_output_ownership(args.output_dir)
        except OSError as exc:
            print("Could not return backup ownership to the sudo user: " + str(exc), file=sys.stderr)
            code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
