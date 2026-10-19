"""What the two libusb readers (gcadapter.py, switch2.py) share: finding libusb, opening a context, and claiming an interface.

Needs lib/libusb-1.0.dll on Windows, or the system libusb on Linux (libusb, LGPL-2.1+; see lib/libusb-NOTICE.txt).
"""
import ctypes
import sys

import config


def load_libusb():
    """Vendored libusb-1.0.dll on Windows, or the system libusb-1.0 on Linux."""
    if sys.platform == "win32":
        dll = config.BASE / "lib" / "libusb-1.0.dll"
        if not dll.exists():
            raise OSError(f"{dll} not found")
        return ctypes.CDLL(str(dll))
    last = None
    for name in (str(config.BASE / "lib" / "libusb-1.0.so"), "libusb-1.0.so.0", "libusb-1.0.so"):
        try:
            return ctypes.CDLL(name)
        except OSError as e:
            last = e
    raise OSError(f"libusb not found ({last})")


def claim(lib, handle, interface):
    """Claim a USB interface, detaching a kernel driver first when the OS has one."""
    try:
        detach = lib.libusb_set_auto_detach_kernel_driver
    except AttributeError:
        detach = None
    if detach is not None:
        detach.argtypes = [ctypes.c_void_p, ctypes.c_int]
        detach(handle, 1)
    return lib.libusb_claim_interface(handle, interface) == 0


def open_libusb():
    """libusb with the calls both readers use declared, and a started context. Returns (lib, ctx); OSError when it can't."""
    lib = load_libusb()
    V = ctypes.c_void_p
    lib.libusb_init.argtypes = [ctypes.POINTER(V)]
    lib.libusb_open_device_with_vid_pid.argtypes = [V, ctypes.c_uint16, ctypes.c_uint16]
    lib.libusb_open_device_with_vid_pid.restype = V
    lib.libusb_claim_interface.argtypes = [V, ctypes.c_int]
    lib.libusb_release_interface.argtypes = [V, ctypes.c_int]
    lib.libusb_close.argtypes = [V]
    ctx = V()
    if lib.libusb_init(ctypes.byref(ctx)) != 0:
        raise OSError("libusb_init failed")
    return lib, ctx
