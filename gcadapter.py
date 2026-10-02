"""Reads the official Nintendo GameCube adapter (Wii U / Switch, USB 057E:0337) directly through libusb.

The official SDL3.dll we ship can't open this adapter on Windows (its GameCube driver needs libusb support the build doesn't have), so this
reads it ourselves. It is only a fallback: the controller loop tries SDL first, so adapters that show up as a normal gamepad (a Mayflash in
PC mode, say) never get here.

Windows needs a WinUSB driver on the adapter for libusb to open it (Zadig, "WUP-028" -> WinUSB; Dolphin's installer does the same). While
another program (Dolphin) has the adapter open this can't read it, and it quietly retries.

The adapter streams one 37-byte packet every ~8 ms: 0x21, then four 9-byte ports (status, buttons 1, buttons 2, stick X, stick Y,
C-stick X, C-stick Y, L trigger, R trigger). Needs lib/libusb-1.0.dll (libusb, LGPL-2.1+; see lib/libusb-NOTICE.txt).
"""
import ctypes
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
VID, PID = 0x057E, 0x0337
EP_OUT, EP_IN = 0x02, 0x81
RETRY = 2.0                                     # seconds between attempts to open the adapter while it isn't available

# adapter buttons -> the XInput-style bitmask the overlay uses (same positions SDL gives the GameCube pad:
# A south, B west, X east, Y north, Z = right shoulder)
BTN1 = {0x01: 0x1000, 0x02: 0x4000, 0x04: 0x2000, 0x08: 0x8000,        # A B X Y
        0x10: 0x4, 0x20: 0x8, 0x40: 0x2, 0x80: 0x1}                      # d-pad left right down up
BTN2 = {0x01: 0x10, 0x02: 0x200}                                          # Start, Z
# The analog triggers rest around 20-30 and top out below 255: stretch that range to 0..255 (tuned on a real adapter, see TRIG_*).
TRIG_MIN, TRIG_MAX = 28, 225                      # (a full pull read 255 on a real adapter)


def _stick(v):
    # 0..255 centred on 128, but a GameCube stick only travels about +-95 of that (measured: +-25000 at *256), so stretch it to the full
    # +-32767 or the sticks in the artwork would never reach the edge. The adapter's Y already points up.
    return max(-32767, min(32767, (v - 128) * 340))


def _trig(v):
    return max(0, min(255, (v - TRIG_MIN) * 255 // (TRIG_MAX - TRIG_MIN)))


class GcAdapterBackend:
    name = "GC adapter (libusb)"

    def __init__(self, index=None):
        dll = BASE / "lib" / "libusb-1.0.dll"
        if not dll.exists():
            raise OSError(f"{dll} not found")
        self.lib = lib = ctypes.CDLL(str(dll))
        lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
        lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
        lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.libusb_close.argtypes = [ctypes.c_void_p]
        lib.libusb_exit.argtypes = [ctypes.c_void_p]
        lib.libusb_interrupt_transfer.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, ctypes.c_int,
                                                  ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
        self.ctx = ctypes.c_void_p()
        if lib.libusb_init(ctypes.byref(self.ctx)) != 0:
            raise OSError("libusb_init failed")
        self.index = index                       # which connected controller to show when several are plugged in (default: the first)
        self.handle = None
        self.next_try = 0.0
        self.last = None                         # the last good state, shown again when a read times out
        self.buf = (ctypes.c_ubyte * 37)()
        self.port = None

    # -- the adapter
    def _open(self):
        now = time.monotonic()
        if now < self.next_try:
            return False
        self.next_try = now + RETRY
        h = self.lib.libusb_open_device_with_vid_pid(self.ctx, VID, PID)
        if not h:
            return False
        if self.lib.libusb_claim_interface(h, 0) != 0:           # another program has it, or no WinUSB driver
            self.lib.libusb_close(h)
            return False
        init = (ctypes.c_ubyte * 1)(0x13)                        # "start sending controller data"
        n = ctypes.c_int(0)
        self.lib.libusb_interrupt_transfer(h, EP_OUT, init, 1, ctypes.byref(n), 100)
        self.handle = h
        print(f"[pad] {self.name}: adapter opened")
        return True

    def _close(self):
        if self.handle:
            self.lib.libusb_release_interface(self.handle, 0)
            self.lib.libusb_close(self.handle)
        self.handle = None
        self.port = None
        self.last = None

    def poll(self):
        if not self.handle and not self._open():
            return None
        n = ctypes.c_int(0)
        rc = self.lib.libusb_interrupt_transfer(self.handle, EP_IN, self.buf, 37, ctypes.byref(n), 25)
        if rc == -7:                                             # timeout: nothing new
            return self.last
        if rc != 0 or n.value < 37 or self.buf[0] != 0x21:
            if rc != 0:                                          # unplugged, or something else grabbed it
                self._close()
            return None
        data = bytes(self.buf)
        ports = [data[1 + 9 * i:10 + 9 * i] for i in range(4)]
        live = [i for i, p in enumerate(ports) if p[0] >> 4 in (1, 2)]    # 0x10 wired pad, 0x20 WaveBird
        if not live:
            self.port = None
            self.last = None
            return None                                          # the adapter is there but no controller is plugged into it
        if self.port not in live:
            self.port = live[min(self.index or 0, len(live) - 1)]
            print(f"[pad] {self.name}: controller on port {self.port + 1}")
        s = ports[self.port]
        b = 0
        for bit, mask in BTN1.items():
            if s[1] & bit:
                b |= mask
        for bit, mask in BTN2.items():
            if s[2] & bit:
                b |= mask
        self.last = {"c": 1, "b": b, "x": 0, "r": s[1] | (s[2] << 8), "t": [], "ty": 11, "nm": "Nintendo GameCube Controller",
                     "lt": _trig(s[7]), "rt": _trig(s[8]),
                     "lx": _stick(s[3]), "ly": _stick(s[4]), "rx": _stick(s[5]), "ry": _stick(s[6])}
        return self.last
