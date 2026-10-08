"""Reads the Nintendo Switch 2 Pro Controller over USB (057E:2069) by itself, because the SDL3.dll we ship doesn't support it.

How the controller works (the startup sequence and the report layout follow SDL's own driver, SDL_hidapi_switch2.c, and the community notes
at github.com/ndeadly/switch2_controller_research):
  * Interface 1 of the USB device is a vendor-specific one with two bulk endpoints. Commands go out there, and the controller will not send
    any input until it has been sent a short start-up sequence. That needs libusb and a WinUSB driver on that interface (Windows binds one
    by itself for this controller; it shows up as "Switch 2 Pro Controller", class USBDevice).
  * Interface 0 is an ordinary HID gamepad. Once started, it sends 64-byte input reports (ID 0x05) that carry buttons and 12-bit sticks.
    Windows' own HID calls read these, so other programs can read them too.
The sticks are calibrated from the controller's flash memory, the same way SDL does it.

Needs lib/libusb-1.0.dll on Windows, or the system libusb plus hidraw on Linux (libusb, LGPL-2.1+; see lib/libusb-NOTICE.txt). Over USB only: Bluetooth is not supported.
"""
import ctypes
import os
import select
import sys
import time
from ctypes import wintypes as wt
from pathlib import Path

from gcadapter import claim, load_libusb

BASE = Path(__file__).resolve().parent
VID, PID = 0x057E, 0x2069
RETRY = 2.0                                               # seconds between attempts while the controller isn't there / won't start

# ---------------------------------------------------------------- the startup sequence (from SDL; each is sent on the bulk OUT endpoint)
INIT = [
    bytes([0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),                                   # unknown purpose
    bytes([0x0c, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00]),         # set feature output bit mask
    bytes([0x11, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),                                   # unknown purpose
    bytes([0x0a, 0x91, 0x00, 0x08, 0x00, 0x14, 0x00, 0x00, 0x01, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
           0xff, 0x35, 0x00, 0x46, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),         # set rumble data?
    bytes([0x0c, 0x91, 0x00, 0x04, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00]),         # enable feature output bits
    bytes([0x01, 0x91, 0x00, 0x0c, 0x00, 0x00, 0x00, 0x00]),                                   # unknown purpose
    bytes([0x01, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),                                   # enable rumble
    bytes([0x08, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00]),         # enable grip buttons on the charging grip
    bytes([0x03, 0x91, 0x00, 0x0a, 0x00, 0x04, 0x00, 0x00, 0x05, 0x00, 0x00, 0x00]),         # set report format: input report 0x05
    bytes([0x03, 0x91, 0x00, 0x0d, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff]),   # start output
]

# ---------------------------------------------------------------- libusb structures (only what is needed to find the bulk endpoints)


class _EndpointDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8), ("bEndpointAddress", ctypes.c_uint8),
                ("bmAttributes", ctypes.c_uint8), ("wMaxPacketSize", ctypes.c_uint16), ("bInterval", ctypes.c_uint8),
                ("bRefresh", ctypes.c_uint8), ("bSynchAddress", ctypes.c_uint8),
                ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


class _InterfaceDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8), ("bInterfaceNumber", ctypes.c_uint8),
                ("bAlternateSetting", ctypes.c_uint8), ("bNumEndpoints", ctypes.c_uint8), ("bInterfaceClass", ctypes.c_uint8),
                ("bInterfaceSubClass", ctypes.c_uint8), ("bInterfaceProtocol", ctypes.c_uint8), ("iInterface", ctypes.c_uint8),
                ("endpoint", ctypes.POINTER(_EndpointDesc)), ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


class _Interface(ctypes.Structure):
    _fields_ = [("altsetting", ctypes.POINTER(_InterfaceDesc)), ("num_altsetting", ctypes.c_int)]


class _ConfigDesc(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8), ("wTotalLength", ctypes.c_uint16),
                ("bNumInterfaces", ctypes.c_uint8), ("bConfigurationValue", ctypes.c_uint8), ("iConfiguration", ctypes.c_uint8),
                ("bmAttributes", ctypes.c_uint8), ("MaxPower", ctypes.c_uint8),
                ("interface", ctypes.POINTER(_Interface)), ("extra", ctypes.c_void_p), ("extra_length", ctypes.c_int)]


# ---------------------------------------------------------------- Windows HID (reads the input reports from interface 0)
class _GUID(ctypes.Structure):
    _fields_ = [("D1", wt.DWORD), ("D2", wt.WORD), ("D3", wt.WORD), ("D4", ctypes.c_ubyte * 8)]


class _SPDID(ctypes.Structure):                 # SP_DEVICE_INTERFACE_DATA
    _fields_ = [("cbSize", wt.DWORD), ("Guid", _GUID), ("Flags", wt.DWORD), ("Reserved", ctypes.c_void_p)]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p), ("Offset", wt.DWORD), ("OffsetHigh", wt.DWORD),
                ("hEvent", ctypes.c_void_p)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wt.USHORT), ("UsagePage", wt.USHORT), ("InputReportByteLength", wt.USHORT),
                ("OutputReportByteLength", wt.USHORT), ("FeatureReportByteLength", wt.USHORT),
                ("Reserved", wt.USHORT * 17), ("NumberLinkCollectionNodes", wt.USHORT),
                ("NumberInputButtonCaps", wt.USHORT), ("NumberInputValueCaps", wt.USHORT), ("NumberInputDataIndices", wt.USHORT),
                ("NumberOutputButtonCaps", wt.USHORT), ("NumberOutputValueCaps", wt.USHORT), ("NumberOutputDataIndices", wt.USHORT),
                ("NumberFeatureButtonCaps", wt.USHORT), ("NumberFeatureValueCaps", wt.USHORT), ("NumberFeatureDataIndices", wt.USHORT)]


def _hid_ids(text):
    """(vendor, product) from a hidraw uevent's HID_ID line, or None.

    HID_ID is ``bus:vid:pid`` in hexadecimal. Matching those fields, rather than
    searching the whole file for the digit strings, avoids a name that merely
    contains them.
    """
    for line in text.splitlines():
        if not line.startswith("HID_ID="):
            continue
        parts = line.split("=", 1)[1].split(":")
        if len(parts) != 3:
            return None
        try:
            return int(parts[1], 16), int(parts[2], 16)
        except ValueError:
            return None
    return None


def _linux_hidraw():
    """hidraw node for a Switch 2 Pro (057e:2069), or None when it isn't there."""
    root = Path("/sys/class/hidraw")
    if not root.is_dir():
        return None
    for node in root.iterdir():
        try:
            text = (node / "device" / "uevent").read_text(errors="replace")
        except OSError:
            continue
        if _hid_ids(text) == (VID, PID):
            return Path("/dev") / node.name
    return None


def _win():
    k32, sa, hid = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("setupapi", use_last_error=True), ctypes.WinDLL("hid")
    V = ctypes.c_void_p
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, V, wt.DWORD, wt.DWORD, V]
    k32.CreateFileW.restype = V
    k32.CloseHandle.argtypes = [V]
    k32.CreateEventW.argtypes = [V, wt.BOOL, wt.BOOL, V]
    k32.CreateEventW.restype = V
    k32.ReadFile.argtypes = [V, V, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.POINTER(_OVERLAPPED)]
    k32.WaitForSingleObject.argtypes = [V, wt.DWORD]
    k32.GetOverlappedResult.argtypes = [V, ctypes.POINTER(_OVERLAPPED), ctypes.POINTER(wt.DWORD), wt.BOOL]
    k32.CancelIo.argtypes = [V]
    k32.ResetEvent.argtypes = [V]
    sa.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR, V, wt.DWORD]
    sa.SetupDiGetClassDevsW.restype = V
    sa.SetupDiEnumDeviceInterfaces.argtypes = [V, V, ctypes.POINTER(_GUID), wt.DWORD, ctypes.POINTER(_SPDID)]
    sa.SetupDiGetDeviceInterfaceDetailW.argtypes = [V, ctypes.POINTER(_SPDID), V, wt.DWORD, ctypes.POINTER(wt.DWORD), V]
    sa.SetupDiDestroyDeviceInfoList.argtypes = [V]
    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(_GUID)]
    hid.HidD_GetPreparsedData.argtypes = [V, ctypes.POINTER(V)]
    hid.HidD_FreePreparsedData.argtypes = [V]
    hid.HidP_GetCaps.argtypes = [V, ctypes.POINTER(_HIDP_CAPS)]
    return k32, sa, hid


def _find_hid_path(sa, hid):
    """The device path of the controller's HID gamepad interface (interface 0)."""
    guid = _GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    info = sa.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x02 | 0x10)          # DIGCF_PRESENT | DIGCF_DEVICEINTERFACE
    if not info or info == ctypes.c_void_p(-1).value:
        return None
    want = f"vid_{VID:04x}&pid_{PID:04x}"
    found, i = None, 0
    try:
        while True:
            d = _SPDID()
            d.cbSize = ctypes.sizeof(_SPDID)
            if not sa.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), i, ctypes.byref(d)):
                break
            i += 1
            need = wt.DWORD(0)
            sa.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(d), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value)
            ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6    # cbSize of the detail struct
            if sa.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(d), buf, need.value, None, None):
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                p = path.lower()
                if want in p and "&mi_00" in p:
                    found = path
                    break
    finally:
        sa.SetupDiDestroyDeviceInfoList(info)
    return found


class Switch2ProBackend:
    name = "Switch 2 Pro (libusb + HID)"

    def __init__(self, index=None):
        self.lib = lib = load_libusb()
        V = ctypes.c_void_p
        lib.libusb_init.argtypes = [ctypes.POINTER(V)]
        lib.libusb_open_device_with_vid_pid.argtypes = [V, ctypes.c_uint16, ctypes.c_uint16]
        lib.libusb_open_device_with_vid_pid.restype = V
        lib.libusb_get_device.argtypes = [V]
        lib.libusb_get_device.restype = V
        lib.libusb_get_config_descriptor.argtypes = [V, ctypes.c_uint8, ctypes.POINTER(ctypes.POINTER(_ConfigDesc))]
        lib.libusb_free_config_descriptor.argtypes = [ctypes.POINTER(_ConfigDesc)]
        lib.libusb_claim_interface.argtypes = [V, ctypes.c_int]
        lib.libusb_release_interface.argtypes = [V, ctypes.c_int]
        lib.libusb_close.argtypes = [V]
        lib.libusb_bulk_transfer.argtypes = [V, ctypes.c_ubyte, V, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
        self.ctx = V()
        if lib.libusb_init(ctypes.byref(self.ctx)) != 0:
            raise OSError("libusb_init failed")
        self.k32 = self.sa = self.hid = None
        if sys.platform == "win32":
            self.k32, self.sa, self.hid = _win()
        self.usb = None                          # libusb device handle (kept so the controller stays started)
        self.fh = None                           # Windows HID file handle
        self.fd = None                           # Linux hidraw fd
        self.ev = None
        self.ov = _OVERLAPPED()
        self.reportlen = 64
        self.buf = ctypes.create_string_buffer(64)
        self.pending = False
        self.next_try = 0.0
        self.last = None
        self.lcal = self.rcal = None            # (neutral, min, max) per axis: ((nx, mx, Mx), (ny, my, My))
        self.out_ep = self.in_ep = None

    # -- the bulk interface (commands)
    def _endpoints(self, handle):
        cfg = ctypes.POINTER(_ConfigDesc)()
        if self.lib.libusb_get_config_descriptor(self.lib.libusb_get_device(handle), 0, ctypes.byref(cfg)) != 0:
            return None
        out = inn = None
        try:
            c = cfg.contents
            for i in range(c.bNumInterfaces):
                itf = c.interface[i]
                for j in range(itf.num_altsetting):
                    a = itf.altsetting[j]
                    if a.bInterfaceNumber != 1:
                        continue
                    for k in range(a.bNumEndpoints):
                        e = a.endpoint[k]
                        if e.bmAttributes & 3 == 2:                              # bulk
                            if e.bEndpointAddress & 0x80:
                                inn = e.bEndpointAddress
                            else:
                                out = e.bEndpointAddress
        finally:
            self.lib.libusb_free_config_descriptor(cfg)
        return (out, inn) if out is not None and inn is not None else None

    def _send(self, data, reply_len=0x40):
        n = ctypes.c_int(0)
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        if self.lib.libusb_bulk_transfer(self.usb, self.out_ep, buf, len(data), ctypes.byref(n), 1000) < 0:
            return None
        got = b""
        while reply_len > len(got):                                              # the controller answers in 64-byte packets
            chunk = (ctypes.c_ubyte * 64)()
            m = ctypes.c_int(0)
            if self.lib.libusb_bulk_transfer(self.usb, self.in_ep, chunk, min(64, reply_len - len(got)), ctypes.byref(m), 100) < 0:
                break
            got += bytes(chunk[:m.value])
            if m.value < 64:
                break
        return got

    def _flash(self, addr):
        cmd = bytes([0x02, 0x91, 0x00, 0x01, 0x00, 0x08, 0x00, 0x00, 0, 0, 0, 0]) + addr.to_bytes(4, "little")
        r = self._send(cmd, 0x50)
        return r[0x10:0x50] if r and len(r) >= 0x50 else None

    @staticmethod
    def _cal(b):
        """12-bit packed neutral / max / min for X and Y -> ((neutral, min, max), (neutral, min, max))."""
        nx = b[0] | ((b[1] & 0x0F) << 8)
        ny = (b[1] >> 4) | (b[2] << 4)
        mx = b[3] | ((b[4] & 0x0F) << 8)
        my = (b[4] >> 4) | (b[5] << 4)
        lx = b[6] | ((b[7] & 0x0F) << 8)
        ly = (b[7] >> 4) | (b[8] << 4)
        return (nx, lx, mx), (ny, ly, my)

    def _calibrate(self):
        for side, factory, user in (("l", 0x13080, 0x1FC040), ("r", 0x130C0, 0x1FC080)):
            cal = None
            blk = self._flash(factory)
            if blk:
                cal = self._cal(blk[0x28:0x28 + 9])
            ublk = self._flash(user)
            if ublk and ublk[0] == 0xB2 and ublk[1] == 0xA1:                    # the user has recalibrated the sticks
                cal = self._cal(ublk[2:2 + 9])
            if side == "l":
                self.lcal = cal
            else:
                self.rcal = cal

    def _start(self):
        """Open the controller, run the startup sequence and open its HID interface. True when it is streaming."""
        now = time.monotonic()
        if now < self.next_try:
            return False
        self.next_try = now + RETRY
        path = _find_hid_path(self.sa, self.hid) if sys.platform == "win32" else None
        if sys.platform == "win32" and not path:
            return False
        h = self.lib.libusb_open_device_with_vid_pid(self.ctx, VID, PID)
        if not h:
            return False
        eps = self._endpoints(h)
        if not eps or not claim(self.lib, h, 1):
            self.lib.libusb_close(h)
            return False
        self.usb, (self.out_ep, self.in_ep) = h, eps
        ok = True
        try:
            self._calibrate()
            for cmd in INIT:
                if self._send(cmd) is None:
                    ok = False
                    break
        except Exception as e:                                                   # a malformed reply must not take the pad loop down
            print(f"[pad] {self.name}: start-up failed: {e}")
            ok = False
        if not ok:
            self._close()
            return False
        if sys.platform != "win32":
            hid = _linux_hidraw()
            if hid is None:
                self._close()
                return False
            try:
                self.fd = os.open(hid, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as e:
                print(f"[pad] {self.name}: cannot read {hid}: {e}")
                self._close()
                return False
            self.reportlen = 64
            print(f"[pad] {self.name}: controller started (hidraw {hid.name}, "
                  f"sticks {'calibrated' if self.lcal and self.rcal else 'uncalibrated'})")
            return True
        bad = ctypes.c_void_p(-1).value
        fh = self.k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, None)   # read+write, shared, OPEN_EXISTING, OVERLAPPED
        if not fh or fh == bad:
            fh = self.k32.CreateFileW(path, 0x80000000, 3, None, 3, 0x40000000, None)   # read only, if something else holds it for writing
        if not fh or fh == bad:
            self._close()
            return False
        pre = ctypes.c_void_p()
        caps = _HIDP_CAPS()
        if self.hid.HidD_GetPreparsedData(fh, ctypes.byref(pre)):
            if self.hid.HidP_GetCaps(pre, ctypes.byref(caps)) is not None and caps.InputReportByteLength:
                self.reportlen = caps.InputReportByteLength
            self.hid.HidD_FreePreparsedData(pre)
        self.buf = ctypes.create_string_buffer(max(self.reportlen, 64))
        self.fh = fh
        self.ev = self.k32.CreateEventW(None, True, False, None)
        self.pending = False
        print(f"[pad] {self.name}: controller started (report length {self.reportlen}, "
              f"sticks {'calibrated' if self.lcal and self.rcal else 'uncalibrated'})")
        return True

    def _close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.k32 and self.fh:
            self.k32.CancelIo(self.fh)
            self.k32.CloseHandle(self.fh)
        if self.k32 and self.ev:
            self.k32.CloseHandle(self.ev)
        if self.usb:
            self.lib.libusb_release_interface(self.usb, 1)
            self.lib.libusb_close(self.usb)
        self.fh = self.ev = self.usb = None
        self.pending = False
        self.last = None

    # -- reading
    def _read(self, timeout_ms):
        """One input report (bytes) or None on timeout; raises OSError when the controller has gone."""
        if self.fd is not None:
            ready, _, _ = select.select([self.fd], [], [], timeout_ms / 1000)
            if not ready:
                return None
            try:
                data = os.read(self.fd, max(self.reportlen, 64))
            except BlockingIOError:
                return None
            if not data:
                raise OSError("HID read failed")
            return data
        if not self.pending:
            self.k32.ResetEvent(self.ev)
            self.ov = _OVERLAPPED()
            self.ov.hEvent = self.ev
            n = wt.DWORD(0)
            ok = self.k32.ReadFile(self.fh, self.buf, self.reportlen, ctypes.byref(n), ctypes.byref(self.ov))
            err = ctypes.get_last_error()
            if not ok and err != 997:                                            # ERROR_IO_PENDING
                raise OSError(f"HID read failed ({err})")
            self.pending = True
        if self.k32.WaitForSingleObject(self.ev, timeout_ms) != 0:
            return None                                                           # still waiting; the read stays pending
        n = wt.DWORD(0)
        self.pending = False
        if not self.k32.GetOverlappedResult(self.fh, ctypes.byref(self.ov), ctypes.byref(n), False):
            raise OSError("HID read failed")
        return bytes(self.buf.raw[:n.value])

    @staticmethod
    def _axis(v, cal, uncal_centre=2048):
        if cal and all(cal):
            n, lo, hi = cal
            d = v - n
            f = d / lo if d < 0 else d / hi
            return int(max(-1.0, min(1.0, f)) * 32767)
        return int(max(-32767, min(32767, (v - uncal_centre) * 16)))

    def poll(self):
        if self.fh is None and self.fd is None and not self._start():
            return None
        try:
            data = self._read(25)
        except OSError:
            self._close()
            return None
        if data is None or len(data) < 17 or data[0] != 0x05:
            return self.last
        b0, b1, b2, b3 = data[5], data[6], data[7], data[8]
        b = 0
        for bit, mask in ((0x01, 0x4000), (0x02, 0x8000), (0x04, 0x1000), (0x08, 0x2000), (0x40, 0x200)):      # Y X B A R
            if b0 & bit:
                b |= mask
        for bit, mask in ((0x01, 0x20), (0x02, 0x10), (0x04, 0x80), (0x08, 0x40), (0x10, 0x400)):               # - + RS LS Home
            if b1 & bit:
                b |= mask
        for bit, mask in ((0x01, 0x2), (0x02, 0x1), (0x04, 0x8), (0x08, 0x4), (0x40, 0x100)):                   # dpad down up right left, L
            if b2 & bit:
                b |= mask
        x = (1 if b1 & 0x20 else 0) | (2 if b3 & 0x01 else 0) | (4 if b3 & 0x02 else 0) | (0x40 if b1 & 0x40 else 0)   # Capture, GR, GL, C
        lx = data[11] | ((data[12] & 0x0F) << 8)
        ly = (data[12] >> 4) | (data[13] << 4)
        rx = data[14] | ((data[15] & 0x0F) << 8)
        ry = (data[15] >> 4) | (data[16] << 4)
        lc = self.lcal or (None, None)
        rc = self.rcal or (None, None)
        self.last = {"c": 1, "b": b, "x": x, "r": b0 | (b1 << 8) | (b2 << 16) | (b3 << 24), "t": [], "ty": 7,
                     "nm": "Nintendo Switch 2 Pro Controller",
                     "lt": 255 if b2 & 0x80 else 0, "rt": 255 if b0 & 0x80 else 0,                     # ZL / ZR are digital
                     "lx": self._axis(lx, lc[0]), "ly": self._axis(ly, lc[1]),
                     "rx": self._axis(rx, rc[0]), "ry": self._axis(ry, rc[1])}
        return self.last
