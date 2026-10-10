"""Xbox face-button swap follows the kernel driver, not the product name.

The trees stand in for /sys/class/input. joydev and evdev are the links closest
to the js node; the hardware driver is further up, which is what a real pad
looks like.
"""
import struct
import tempfile
import unittest
from pathlib import Path

import server


def _tree(root, driver, vendor=None, vendor_file="id/vendor"):
    """js0 -> joydev, then evdev, then `driver`. Optional vendor id file."""
    js = root / "js0"
    hid = js / "device" / "device"
    hid.mkdir(parents=True)
    (js / "driver").symlink_to("/drivers/joydev")
    (js / "device" / "driver").symlink_to("/drivers/evdev")
    (hid / "driver").symlink_to(f"/drivers/{driver}")
    if vendor is not None:
        path = js / "device" / vendor_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{vendor:04x}\n", encoding="ascii")
    return root


def _blank():
    return {"b": 0, "x": 0, "lt": 0, "rt": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}


def _press(buttons, index, swap):
    state = _blank()
    data = struct.pack("<IhBB", 0, 1, 1, index)
    server._js_apply_event(state, [], buttons, swap, data)
    return state["b"]


class JsDriverSwapTest(unittest.TestCase):
    def test_sysfs_driver_and_face_buttons(self):
        # Compass gamepad map: index 2 is BTN_X (0x133), index 3 is BTN_Y (0x134).
        buttons = [0x130, 0x131, 0x133, 0x134]
        cases = (
            ("xpad", None, True),
            ("xpadneo", None, True),
            ("playstation", None, False),
            ("nintendo", None, False),
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for driver, vendor, swap in cases:
                root = _tree(base / driver, driver, vendor)
                found = server._js_driver("/dev/input/js0", sysfs_root=root)
                self.assertEqual(found, driver)
                self.assertEqual(server._js_swaps_xy(found, vendor), swap)
                # Swapped: physical X -> west 0x4000, physical Y -> north 0x8000.
                # Compass: BTN_X is north 0x8000, BTN_Y is west 0x4000.
                x_bit = 0x4000 if swap else 0x8000
                y_bit = 0x8000 if swap else 0x4000
                self.assertEqual(_press(buttons, 2, swap), x_bit, driver)
                self.assertEqual(_press(buttons, 3, swap), y_bit, driver)

    def test_microsoft_bluetooth_vendor(self):
        buttons = [0x130, 0x131, 0x133, 0x134]
        # hid-microsoft's sysfs name is "microsoft"; the module name is accepted too.
        cases = (
            ("hid-generic", 0x045E, True),
            ("microsoft", 0x045E, True),
            ("hid-microsoft", 0x045E, True),
            ("hid-generic", 0x054C, False),   # Sony, even on the generic HID driver
            ("microsoft", 0x045E, True, "idVendor"),
            ("playstation", 0x045E, False),   # driver wins; a Sony driver is not Xbox
            ("nintendo", 0x057E, False),
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for item in cases:
                driver, vendor, swap = item[0], item[1], item[2]
                vendor_file = item[3] if len(item) > 3 else "id/vendor"
                root = _tree(base / f"{driver}-{vendor:04x}-{vendor_file.replace('/', '-')}",
                             driver, vendor, vendor_file)
                found, seen = server._js_identity("/dev/input/js0", sysfs_root=root)
                self.assertEqual(found, driver)
                self.assertEqual(seen, vendor)
                self.assertEqual(server._js_swaps_xy(found, seen), swap)
                x_bit = 0x4000 if swap else 0x8000
                self.assertEqual(_press(buttons, 2, server._js_swaps_xy(found, seen)), x_bit)

    def test_generic_joystick_stays_in_button_order(self):
        # BTN_TRIGGER..TOP are A B X Y with no face-button swap.
        buttons = [0x120, 0x121, 0x122, 0x123]
        with tempfile.TemporaryDirectory() as tmp:
            root = _tree(Path(tmp), "hid-generic", 0x0079)
            found, vendor = server._js_identity("/dev/input/js0", sysfs_root=root)
            swap = server._js_swaps_xy(found, vendor)
            self.assertFalse(swap)
            self.assertEqual(_press(buttons, 0, swap), 0x1000)
            self.assertEqual(_press(buttons, 2, swap), 0x4000)
            self.assertEqual(_press(buttons, 3, swap), 0x8000)

    def test_product_name_does_not_decide(self):
        self.assertFalse(server._js_swaps_xy("8BitDo Ultimate"))
        self.assertFalse(server._js_swaps_xy("Logitech F310"))
        self.assertFalse(server._js_swaps_xy(""))
        self.assertTrue(server._js_swaps_xy("xpadneo"))
        self.assertTrue(server._js_swaps_xy("xpad"))


if __name__ == "__main__":
    unittest.main()
