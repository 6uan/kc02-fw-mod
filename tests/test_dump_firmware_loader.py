from __future__ import annotations

import importlib.util
import io
import json
import plistlib
from pathlib import Path
import struct
import subprocess
import tempfile
import sys

import usb.core
import usb.backend.libusb1
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/dump_firmware_loader.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("usb_loader_dump", SCRIPT)
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
loader.usb = usb


class LoaderCommandTests(unittest.TestCase):
    def test_flash_read_matches_factory_signal_drive_layout(self):
        self.assertEqual(loader.spi_cdb(0x03, 0x123400).hex(),
                         "cb740000000007040312340000000000")
        self.assertEqual(loader.spi_cdb(0x9F).hex(),
                         "cb740000000003019f00000000000000")

    def test_program_erase_status_write_and_write_enable_are_rejected(self):
        for opcode in (0x02, 0x06, 0x01, 0x20, 0xD8, 0xC7, 0x60):
            with self.subTest(opcode=opcode), self.assertRaises(ValueError):
                loader.spi_cdb(opcode)

    def test_flash_read_bounds_and_alignment(self):
        for offset in (-512, 1, 0x1000000):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                loader.spi_cdb(0x03, offset)
        with self.assertRaises(ValueError):
            loader.spi_cdb(0x9F, 512)

    def test_sram_upload_cannot_target_flash_or_rom(self):
        for address, length in ((0x100000, 512), (3840, 512), (512, 513), (1, 512)):
            with self.subTest(address=address), self.assertRaises(ValueError):
                loader.memory_cdb(address, length, write=True)
        self.assertEqual(loader.memory_cdb(0, 512, write=True).hex(),
                         "cb0800100000000000ffffffff000000")
        self.assertEqual(len(loader.memory_cdb(3584, 256, write=True)), 16)

    def test_invalid_capacity_does_not_fall_back_to_four_mib(self):
        self.assertEqual(loader.capacity_from_jedec(bytes.fromhex("85601685")), 0x400000)
        self.assertEqual(loader.capacity_from_jedec(bytes.fromhex("ef401700")), 0x800000)
        for raw in ("ffffff00", "00001600", "8560ff00", "85601900", "856016", "85aa1600", "ee601600"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                loader.capacity_from_jedec(bytes.fromhex(raw))

    def test_different_driver_is_rejected_before_usb(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unreviewed.elf"
            path.write_bytes(bytes(23609))
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                loader.read_driver(path)

    def test_other_firmware_header_sums_do_not_reject_an_original(self):
        data = bytearray(0x400000)
        data[4:8] = b"BLDR"
        data[0x1FE:0x200] = b"\x55\xAA"
        data[0x304:0x308] = b"SFAT"
        self.assertNotEqual(sum(data[:512]) % 256, 0)
        self.assertTrue(loader.validate_flash(data)["bldr_magic"])
        with self.assertRaises(ValueError):
            loader.validate_flash(bytes(0x400000))


class LoaderReceiveTests(unittest.TestCase):
    class Device:
        def __init__(self, replies):
            self.replies = list(replies)
            self.writes = []

        def write(self, endpoint, data, timeout):
            self.writes.append(bytes(data))
            return len(data)

        def read(self, endpoint, length, timeout):
            item = self.replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    def test_early_status_is_not_consumed_as_partial_payload(self):
        dev = self.Device([struct.pack("<4sIIB", b"USBS", 1, 512, 1), b"never read"])
        report = {}
        t = loader.Transport(dev, report)
        with self.assertRaisesRegex(RuntimeError, "status instead"):
            t.transaction(loader.memory_cdb(0, 512), 512)
        self.assertEqual(len(dev.replies), 1)
        with self.assertRaisesRegex(RuntimeError, "already failed"):
            t.transaction(loader.memory_cdb(0, 512), 512)
        self.assertEqual(len(dev.writes), 1)
        self.assertIn("early_csw_hex", report["transactions"][0])

    def test_short_data_stops_before_status_and_is_not_replayed(self):
        dev = self.Device([bytes(8), b"never read"])
        t = loader.Transport(dev, {})
        with self.assertRaisesRegex(RuntimeError, "Short data"):
            t.transaction(loader.spi_cdb(0x03), 512)
        self.assertEqual(len(dev.replies), 1)
        self.assertEqual(len(dev.writes), 1)

    def test_nonzero_residue_or_wrong_tag_cannot_count_as_a_read(self):
        for tag, residue, status in ((2, 0, 0), (1, 1, 0), (1, 0, 1)):
            dev = self.Device([bytes(512), struct.pack("<4sIIB", b"USBS", tag, residue, status)])
            t = loader.Transport(dev, {})
            with self.subTest(tag=tag, residue=residue, status=status), self.assertRaisesRegex(RuntimeError, "Invalid CSW"):
                t.transaction(loader.spi_cdb(0x03), 512)

    def test_success_preserves_data_and_validates_both_phases(self):
        data = bytes(range(256)) * 2
        dev = self.Device([data, struct.pack("<4sIIB", b"USBS", 1, 0, 0)])
        report = {}
        t = loader.Transport(dev, report)
        self.assertEqual(t.transaction(loader.spi_cdb(0x03), 512), data)
        self.assertEqual(report["transactions"][0]["stage"], "completed")
        self.assertEqual(len(dev.writes[0]), 31)


class CompleteBackupTests(unittest.TestCase):
    class Device:
        manufacturer, product, bcdDevice = "BUILDWIN", "BLDR v1.00", 256

        def __init__(self, mismatch=False):
            self.ram = bytearray(3840)
            self.flash = bytearray((i * 17 + 3) % 256 for i in range(1 << 20))
            self.flash[4:8] = b"BLDR"
            self.flash[0x1FE:0x200] = b"\x55\xAA"
            self.flash[0x304:0x308] = b"SFAT"
            self.mismatch, self.flash_commands = mismatch, 0
            self.inquiry = bytes(8) + b"BuildWinVideo050Loader  1.00"
            self.jedec = bytes.fromhex("85601485")
            self.init_commands = 0
            self.corrupt_sram = False
            self.status_phase = False

        def get_active_configuration(self):
            class Interface(list):
                bInterfaceNumber, bAlternateSetting = 0, 0
                bInterfaceClass, bInterfaceSubClass, bInterfaceProtocol = 8, 6, 80
            return [Interface([SimpleNamespace(bEndpointAddress=e, bmAttributes=2) for e in (1, 129)])]

        def is_kernel_driver_active(self, interface):
            return False

        def write(self, endpoint, data, timeout):
            if len(data) == 31 and data[:4] == b"USBC":
                self.tag, self.length = struct.unpack_from("<II", data, 4)
                self.direction, self.cdb = data[12], data[15:]
                if self.cdb[0] == 0xCB and struct.unpack_from("<I", self.cdb, 1)[0] == loader.SPI_INIT:
                    self.init_commands += 1
                self.status_phase = not self.length or self.direction == 0
            else:
                self.assert_memory()
                address = struct.unpack_from("<I", self.cdb, 5)[0]
                self.ram[address:address + len(data)] = data
                if self.corrupt_sram:
                    self.ram[address] ^= 1
            return len(data)

        def assert_memory(self):
            if struct.unpack_from("<I", self.cdb, 1)[0] != loader.MEMORY_API:
                raise AssertionError("Unexpected SRAM-write function")

        def read(self, endpoint, length, timeout):
            if self.status_phase:
                self.status_phase = False
                return struct.pack("<4sIIB", b"USBS", self.tag, 0, 0)
            self.status_phase = True
            if self.cdb[0] == 0x12:
                return self.inquiry
            function = struct.unpack_from("<I", self.cdb, 1)[0]
            if function == loader.MEMORY_API:
                address = struct.unpack_from("<I", self.cdb, 5)[0]
                return self.ram[address:address + length]
            if function != loader.SIGNAL_DRIVE:
                raise AssertionError("Unexpected function")
            if self.cdb[8] == 0x9F:
                return self.jedec
            if self.cdb[8] != 0x03:
                raise AssertionError("Unexpected SPI opcode")
            offset = int.from_bytes(self.cdb[9:12], "big")
            data = bytearray(self.flash[offset:offset + length])
            self.flash_commands += 1
            if self.mismatch and self.flash_commands > (1 << 20) // 512 and offset == 0x1000:
                data[0] ^= 1
            return data

    def exercise(self, mismatch=False, error=None, configure=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        dev, report = self.Device(mismatch), {}
        if configure:
            configure(dev)
        args = SimpleNamespace(driver=Path("unused.elf"), output_dir=root, enter_loader=False)
        with patch.object(loader, "read_driver", return_value=(bytes([0xA5]) * 3840, {})), \
             patch.object(loader, "load_backend", return_value=object()), \
             patch.object(loader.usb.core, "find", return_value=[dev]), \
             patch.object(loader.usb.util, "claim_interface"), \
             patch.object(loader.usb.util, "release_interface"), \
             patch.object(loader.usb.util, "dispose_resources"), \
             patch("sys.stdout", new=io.StringIO()):
            if mismatch or error:
                with self.assertRaisesRegex((RuntimeError, ValueError), error or "do not match"):
                    loader.run(args, report)
            else:
                loader.run(args, report)
        return dev, report

    def test_unknown_loader_identity_stops_before_sram_access(self):
        dev, report = self.exercise(error="identity has no reviewed", configure=lambda d:
                                    setattr(d, "inquiry", bytes(8) + b"BuildWinVideo060Loader  1.00"))
        self.assertEqual(dev.ram, bytes(3840))
        self.assertEqual(dev.init_commands, 0)
        self.assertEqual(dev.flash_commands, 0)

    def test_driver_readback_mismatch_stops_before_execution(self):
        dev, report = self.exercise(error="SRAM verification failed", configure=lambda d:
                                    setattr(d, "corrupt_sram", True))
        self.assertNotIn("driver_sram_verified", report)
        self.assertEqual(dev.init_commands, 0)
        self.assertEqual(dev.flash_commands, 0)

    def test_unknown_nor_type_cannot_start_flash_reads(self):
        dev, report = self.exercise(error="no reviewed NOR profile", configure=lambda d:
                                    setattr(d, "jedec", bytes.fromhex("85aa1685")))
        self.assertTrue(report["driver_sram_verified"])
        self.assertEqual(dev.init_commands, 1)
        self.assertEqual(dev.flash_commands, 0)

    def test_two_complete_independent_reads_are_required_for_verified_backup(self):
        dev, report = self.exercise(False)
        self.assertTrue(report["two_full_reads_match"])
        self.assertEqual(dev.flash_commands, 2 * len(dev.flash) // 512)
        for read in report["reads"]:
            self.assertTrue(read["verified"])
            self.assertEqual(Path(read["path"]).read_bytes(), dev.flash)

    def test_a_changed_second_read_leaves_both_originals_unverified(self):
        dev, report = self.exercise(True)
        self.assertNotIn("two_full_reads_match", report)
        self.assertEqual(len(report["reads"]), 2)
        self.assertTrue(all(not r["verified"] for r in report["reads"]))
        self.assertTrue(all(r["bytes"] == len(dev.flash) for r in report["reads"]))


class LoaderModeTests(unittest.TestCase):
    class AppDevice:
        def get_active_configuration(self):
            class Interface(list):
                bInterfaceNumber, bAlternateSetting = 4, 0
                bInterfaceClass, bInterfaceSubClass, bInterfaceProtocol = 8, 6, 80
            return [Interface([SimpleNamespace(bEndpointAddress=e, bmAttributes=2) for e in (1, 129)])]

        def is_kernel_driver_active(self, interface):
            return False

    def test_loader_request_matches_factory_ufupdate_without_function_pointer(self):
        packet = loader.cbw(0x12345678, 0xDA)
        self.assertEqual(len(packet), 31)
        self.assertEqual(struct.unpack("<4sIIBBB", packet[:15]),
                         (b"USBC", 0x12345678, 0, 0x80, 0, 16))
        self.assertEqual(packet[15:], bytes([0xDA]) + bytes(15))

    def test_read_request_is_standard_36_byte_inquiry(self):
        packet = loader.cbw(7, 0x12)
        self.assertEqual(struct.unpack_from("<I", packet, 8)[0], 36)
        self.assertEqual(packet[14:21], bytes([6, 0x12, 0, 0, 0, 36, 0]))

    def test_other_vendor_opcodes_are_rejected_before_io(self):
        for opcode in (0xCB, 0xCD, 0x02, 0x20, 0xC7, 0xDA00):
            with self.subTest(opcode=opcode), self.assertRaises(ValueError):
                loader.cbw(1, opcode)

    def test_transport_failure_is_not_replayed(self):
        class Device:
            writes = 0

            def write(self, endpoint, packet, timeout):
                self.writes += 1
                return len(packet)

            def read(self, endpoint, size, timeout):
                raise OSError("device left bus")

        dev, report = Device(), {}
        with self.assertRaises(OSError):
            loader.command(dev, 5, 0xDA, report)
        self.assertEqual(dev.writes, 1)
        self.assertEqual(len(report["commands"]), 1)
        self.assertEqual(report["commands"][0]["stage"], "bulk IN CSW")

    def test_failed_mode_switch_stops_before_loader_discovery(self):
        report = {}
        inquiry = bytes(8) + b"Buildwin" + b"Media-Player    " + b"1.00"
        with patch.object(loader.usb.core, "find", side_effect=[[], [self.AppDevice()]]) as find, \
             patch.object(loader, "unmount_camera_card", return_value=("unmounted", lambda: None)), \
             patch.object(loader.usb.util, "claim_interface"), \
             patch.object(loader.usb.util, "release_interface"), \
             patch.object(loader.usb.util, "dispose_resources"), \
             patch.object(loader, "command", side_effect=[inquiry, loader.usb.core.USBError("disconnected")]):
            with self.assertRaises(loader.usb.core.USBError):
                loader.enter_loader(object(), report)
        self.assertTrue(report["mode_switch_attempted"])
        self.assertNotIn("mode_switch_csw_valid", report)
        self.assertEqual(find.call_count, 2)

    def test_mode_switch_hands_off_first_loader_without_reopening_strings(self):
        report = {}
        inquiry = bytes(8) + b"Buildwin" + b"Media-Player    " + b"1.00"
        device = SimpleNamespace(bus=3, address=1, bcdDevice=256)
        with patch.object(loader.usb.core, "find", side_effect=[[], [self.AppDevice()], [device]]) as find, \
             patch.object(loader, "unmount_camera_card", return_value=("unmounted", lambda: None)), \
             patch.object(loader.usb.util, "claim_interface"), \
             patch.object(loader.usb.util, "release_interface"), \
             patch.object(loader.usb.util, "dispose_resources"), \
             patch.object(loader, "command", side_effect=[inquiry, b""]):
            self.assertIs(loader.enter_loader(object(), report), device)
        self.assertTrue(report["mode_switch_csw_valid"])
        self.assertTrue(report["loader_observed"])
        self.assertEqual(find.call_count, 3)

    def test_existing_loader_is_not_mistaken_for_the_switched_camera(self):
        with patch.object(loader.usb.core, "find", return_value=[object()]), \
             patch.object(loader, "unmount_camera_card") as unmount:
            with self.assertRaisesRegex(RuntimeError, "already present"):
                loader.enter_loader(object(), {})
        unmount.assert_not_called()


class HostBoundaryTests(unittest.TestCase):
    def test_sudo_outputs_return_to_invoking_user(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            (output / "read1.bin").write_bytes(b"synthetic")
            with patch.dict(loader.os.environ, {"SUDO_UID": "501", "SUDO_GID": "20"}), \
                 patch.object(loader.os, "chown") as chown:
                loader.return_output_ownership(output)
            self.assertEqual(chown.call_count, 2)
            chown.assert_any_call(output / "read1.bin", 501, 20, follow_symlinks=False)
            chown.assert_any_call(output, 501, 20, follow_symlinks=False)

    def test_help_works_without_site_packages_or_usb_access(self):
        result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--help"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--enter-loader", result.stdout)
        self.assertIn("--driver", result.stdout)

    def test_output_on_camera_volume_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            volume = Path(temp) / "camera"
            volume.mkdir()
            reply = SimpleNamespace(stdout=plistlib.dumps({"MountPoint": str(volume)}))
            with patch.object(loader, "registry_cameras", return_value=[{"disks": ["disk27s1"]}]), \
                 patch.object(loader.subprocess, "run", return_value=reply):
                with self.assertRaisesRegex(ValueError, "not the camera card"):
                    loader.validate_output_dir(volume / "backup")
                loader.validate_output_dir(Path(temp) / "host-backup")

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(loader, "registry_cameras") as inventory:
            with self.assertRaisesRegex(ValueError, "already exists"):
                loader.validate_output_dir(Path(temp))
        inventory.assert_not_called()

    def test_bad_driver_stops_before_output_or_usb(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "backup"
            with patch.object(loader.sys, "platform", "darwin"), \
                 patch.object(loader.os, "geteuid", return_value=0), \
                 patch.object(loader, "read_driver", side_effect=ValueError("bad hash")), \
                 patch.object(loader, "load_backend") as backend, \
                 patch("sys.stderr", new=io.StringIO()):
                code = loader.main(["--from-loader", "--driver", "bad.elf", "--output-dir", str(output)])
            self.assertEqual(code, 1)
            self.assertFalse(output.exists())
            backend.assert_not_called()

    def test_interruption_preserves_an_unverified_report(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "backup"
            with patch.object(loader.sys, "platform", "darwin"), \
                 patch.object(loader.os, "geteuid", return_value=0), \
                 patch.object(loader, "read_driver"), \
                 patch.object(loader, "validate_output_dir"), \
                 patch.object(loader, "run", side_effect=KeyboardInterrupt), \
                 patch("sys.stdout", new=io.StringIO()):
                code = loader.main(["--from-loader", "--driver", "unused.elf", "--output-dir", str(output)])
            self.assertEqual(code, 130)
            report = json.loads((output / "report.json").read_text())
            self.assertFalse(report["two_full_reads_match"])
            self.assertIn("Interrupted", report["error"])


if __name__ == "__main__":
    unittest.main()
