"""Input Overlay server.

Captures global keyboard / mouse input and controller input on Windows and
streams it over a WebSocket to the overlay page (overlay/index.html), which is
meant to be loaded as an OBS Browser Source. Controllers are read through SDL3,
the GameCube adapter and Switch 2 Pro readers (both via libusb) and XInput.

Messages (JSON, server -> overlay):
  {"v": "<build id>"}           sent first on connect; overlays reload themselves if it changed
  {"reload": 1}                 presets or settings changed; overlays re-read them
  {"k": [vk, 1|0]}              key down / up (Windows virtual-key code)
  {"m": [button, 1|0]}          mouse button: left right middle x1 x2
  {"d": [dx, dy]}               raw mouse movement since last flush
  {"s": [dx, dy]}               scroll wheel ticks
  {"p": [{...}, ...]}           gamepad states, one per connected controller ([] when none is connected), each with these fields:
      c   always 1 (a controller that disconnects is simply left out of the list)
      b   buttons as an XInput bitmask
      x   extra SDL buttons (misc, paddles, touchpad click ...) as a bitmask, bit = SDL index - 15
      r   raw joystick buttons as a bitmask (debugging)
      t   touchpads: [[down, x 0..1, y 0..1, pressure], ...]
      ty  SDL gamepad type number
      nm  controller name
      lt, rt          triggers 0..255
      lx, ly, rx, ry  sticks -32767..32767 (y up)
      g   [pitch, yaw, roll] in rad/s, only when the pad reports a gyroscope
"""
import argparse
import asyncio
import ctypes
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web, WSMsgType
from pynput import keyboard, mouse

import config
import themes
from version import VERSION
from gcadapter import GcAdapterBackend
from switch2 import Switch2ProBackend

# Bundled files live next to this script, or in PyInstaller's extraction dir when frozen.
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
ROOT = BASE / "overlay"
# Changes whenever the bundled overlay files change; overlays reload themselves when it differs from the one they loaded with.
BUILD_ID = str(int(max((f.stat().st_mtime for f in ROOT.rglob("*") if f.is_file()), default=0)))


# --------------------------------------------------------------------------
# Event plumbing: worker threads -> asyncio loop -> websocket clients
# --------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.loop = None
        self.queue = None
        self.clients = set()
        self.pad_state = None
        self._dx = self._dy = 0
        self._lock = threading.Lock()

    def emit(self, msg):
        if self.loop:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, json.dumps(msg, separators=(",", ":")))

    def add_motion(self, dx, dy):
        with self._lock:
            self._dx += dx
            self._dy += dy

    def take_motion(self):
        with self._lock:
            d = (self._dx, self._dy)
            self._dx = self._dy = 0
        return d


hub = Hub()


# --------------------------------------------------------------------------
# Keyboard + mouse buttons/scroll (pynput low-level hooks)
# --------------------------------------------------------------------------
def vk_of(key):
    vk = getattr(key, "vk", None)
    if vk is None:
        vk = getattr(getattr(key, "value", None), "vk", None)
    return vk


_down_keys = set()
_extended = False                      # the low-level hook's "extended key" flag of the key event being handled


def kb_filter(msg, data):
    """Runs just before on_press / on_release, on the same thread: remember whether Windows marks this key as an extended key."""
    global _extended
    _extended = bool(data.flags & 1)   # LLKHF_EXTENDED
    return True


def _vk(key):
    vk = vk_of(key)
    return 269 if vk == 13 and _extended else vk        # numpad Enter is an extended Return: 269 is the overlay's own code for it


def on_press(key):
    vk = _vk(key)
    if vk is None or vk in _down_keys:  # ignore auto-repeat
        return
    _down_keys.add(vk)
    hub.emit({"k": [vk, 1]})


def on_release(key):
    vk = _vk(key)
    if vk is None:
        return
    _down_keys.discard(vk)
    hub.emit({"k": [vk, 0]})


def on_click(x, y, button, pressed):
    hub.emit({"m": [button.name, 1 if pressed else 0]})


def on_scroll(x, y, dx, dy):
    hub.emit({"s": [dx, dy]})


# --------------------------------------------------------------------------
# Raw mouse movement (Windows Raw Input) - keeps working when a game locks and
# recentres the cursor, unlike reading cursor position.
# --------------------------------------------------------------------------
def raw_mouse_thread():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
                    ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE),
                    ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", wt.USHORT), ("usUsage", wt.USHORT),
                    ("dwFlags", wt.DWORD), ("hwndTarget", wt.HWND)]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", wt.DWORD), ("dwSize", wt.DWORD),
                    ("hDevice", wt.HANDLE), ("wParam", wt.WPARAM)]

    class RAWMOUSE(ctypes.Structure):
        _fields_ = [("usFlags", wt.USHORT), ("ulButtons", wt.ULONG), ("ulRawButtons", wt.ULONG),
                    ("lLastX", wt.LONG), ("lLastY", wt.LONG), ("ulExtraInformation", wt.ULONG)]

    class RAWINPUT(ctypes.Structure):
        _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]

    user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.GetRawInputData.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_void_p,
                                       ctypes.POINTER(wt.UINT), wt.UINT]
    user32.GetRawInputData.restype = wt.UINT
    user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wt.UINT, wt.UINT]
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND,
                                       wt.HANDLE, wt.HINSTANCE, ctypes.c_void_p]
    user32.CreateWindowExW.restype = wt.HWND
    kernel32.GetModuleHandleW.restype = wt.HINSTANCE

    WM_INPUT, RID_INPUT, RIM_TYPEMOUSE = 0x00FF, 0x10000003, 0
    MOUSE_MOVE_ABSOLUTE = 0x01

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            raw = RAWINPUT()
            size = wt.UINT(ctypes.sizeof(raw))
            got = user32.GetRawInputData(lparam, RID_INPUT, ctypes.byref(raw), ctypes.byref(size),
                                         ctypes.sizeof(RAWINPUTHEADER))
            if got != 0xFFFFFFFF and raw.header.dwType == RIM_TYPEMOUSE \
                    and not raw.mouse.usFlags & MOUSE_MOVE_ABSOLUTE:
                if raw.mouse.lLastX or raw.mouse.lLastY:
                    hub.add_motion(raw.mouse.lLastX, raw.mouse.lLastY)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)  # keep a reference so it isn't garbage collected
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW()
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = "InputOverlayRawInput"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "", 0, 0, 0, 0, 0, wt.HWND(-3), None, hinst, None)  # HWND_MESSAGE
    if not hwnd:
        print("[raw-mouse] could not create message window; mouse movement disabled")
        return

    rid = RAWINPUTDEVICE(0x01, 0x02, 0x100, hwnd)  # generic desktop / mouse, INPUTSINK
    if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
        print("[raw-mouse] RegisterRawInputDevices failed; mouse movement disabled")
        return

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# --------------------------------------------------------------------------
# Controller input. Backends, both reduced to the same state dict (buttons
# use the XInput bitmask so the overlay only needs one format):
#   * SDL3 - sees the Steam Controller directly, even when Steam is not
#     exposing a virtual Xbox pad. Needs lib/SDL3.dll (official SDL release).
#   * GC adapter - reads the official Nintendo GameCube adapter through libusb (gcadapter.py), which SDL can't open here.
#   * Switch 2 Pro - reads that controller over USB (switch2.py), which SDL can't.
#   * XInput - fallback for Xbox pads and Steam's virtual pad.
#   A backend that has no pad right now returns None, so the next one gets a turn.
# --------------------------------------------------------------------------
MAX_PADS = 4                                       # controllers shown at once (the preset's "pads" setting goes up to this)


class _XGAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wt.WORD), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _XSTATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", _XGAMEPAD)]


class SdlBackend:
    name = "SDL3"
    INIT_GAMEPAD, INIT_EVENTS = 0x2000, 0x4000
    SENSOR_ACCEL, SENSOR_GYRO = 1, 2
    # SDL_GamepadButton -> XInput bitmask
    BUTTONS = {0: 0x1000, 1: 0x2000, 2: 0x4000, 3: 0x8000, 4: 0x20, 5: 0x400, 6: 0x10, 7: 0x40, 8: 0x80,
               9: 0x100, 10: 0x200, 11: 0x1, 12: 0x2, 13: 0x4, 14: 0x8}

    def __init__(self, index):
        dll = BASE / "lib" / "SDL3.dll"
        if not dll.exists():
            raise OSError(f"{dll} not found")
        self.sdl = sdl = ctypes.CDLL(str(dll))
        sdl.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        sdl.SDL_Init.argtypes = [ctypes.c_uint32]
        sdl.SDL_GetGamepads.argtypes = [ctypes.POINTER(ctypes.c_int)]
        sdl.SDL_GetGamepads.restype = ctypes.POINTER(ctypes.c_uint32)
        sdl.SDL_free.argtypes = [ctypes.c_void_p]
        sdl.SDL_OpenGamepad.argtypes = [ctypes.c_uint32]
        sdl.SDL_OpenGamepad.restype = ctypes.c_void_p
        sdl.SDL_CloseGamepad.argtypes = [ctypes.c_void_p]
        sdl.SDL_GamepadConnected.argtypes = [ctypes.c_void_p]
        sdl.SDL_GamepadConnected.restype = ctypes.c_bool
        sdl.SDL_GetGamepadButton.argtypes = [ctypes.c_void_p, ctypes.c_int]
        sdl.SDL_GetGamepadButton.restype = ctypes.c_bool
        sdl.SDL_GetGamepadAxis.argtypes = [ctypes.c_void_p, ctypes.c_int]
        sdl.SDL_GetGamepadAxis.restype = ctypes.c_int16
        sdl.SDL_GetGamepadName.argtypes = [ctypes.c_void_p]
        sdl.SDL_GetGamepadName.restype = ctypes.c_char_p
        sdl.SDL_GetGamepadType.argtypes = [ctypes.c_void_p]
        sdl.SDL_GetGamepadType.restype = ctypes.c_int
        sdl.SDL_GetError.restype = ctypes.c_char_p
        sdl.SDL_GetNumGamepadTouchpads.argtypes = [ctypes.c_void_p]
        sdl.SDL_GetGamepadJoystick.argtypes = [ctypes.c_void_p]
        sdl.SDL_GetGamepadJoystick.restype = ctypes.c_void_p
        sdl.SDL_GetNumJoystickButtons.argtypes = [ctypes.c_void_p]
        sdl.SDL_GetJoystickButton.argtypes = [ctypes.c_void_p, ctypes.c_int]
        sdl.SDL_GetJoystickButton.restype = ctypes.c_bool
        sdl.SDL_GetGamepadTouchpadFinger.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_bool),
            ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float)]
        sdl.SDL_GetGamepadTouchpadFinger.restype = ctypes.c_bool
        # Motion sensors (SDL_SensorType: 1 accelerometer, 2 gyroscope). Only the gyroscope is used (rates in rad/s).
        sdl.SDL_GamepadHasSensor.argtypes = [ctypes.c_void_p, ctypes.c_int]
        sdl.SDL_GamepadHasSensor.restype = ctypes.c_bool
        sdl.SDL_SetGamepadSensorEnabled.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_bool]
        sdl.SDL_SetGamepadSensorEnabled.restype = ctypes.c_bool
        sdl.SDL_GetGamepadSensorData.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_float), ctypes.c_int]
        sdl.SDL_GetGamepadSensorData.restype = ctypes.c_bool
        sdl.SDL_SetHint(b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1")
        # Otherwise SDL_Init installs its own SIGINT handler, which swallows Ctrl+C.
        sdl.SDL_SetHint(b"SDL_NO_SIGNAL_HANDLERS", b"1")
        if not sdl.SDL_Init(self.INIT_GAMEPAD | self.INIT_EVENTS):
            raise OSError("SDL_Init failed: " + sdl.SDL_GetError().decode())
        self.index = index
        self.pads = {}                            # SDL instance id -> {"pad": handle, "name", "type", "gyro", "active"}, in the order they were opened
        self.next_scan = 0.0

    def _open(self, iid):
        pad = self.sdl.SDL_OpenGamepad(iid)
        if not pad:
            return
        name = (self.sdl.SDL_GetGamepadName(pad) or b"").decode(errors="replace")
        ptype = self.sdl.SDL_GetGamepadType(pad)
        print(f"[pad] {self.name}: {name} (SDL type {ptype})")
        has = bool(self.sdl.SDL_GamepadHasSensor(pad, self.SENSOR_GYRO))
        gyro = has and bool(self.sdl.SDL_SetGamepadSensorEnabled(pad, self.SENSOR_GYRO, True))
        print(f"[pad] {self.name}: gyro " + ("on" if gyro else "available but could not be enabled" if has else "not reported by this controller"))
        self.pads[iid] = {"pad": pad, "name": name, "type": ptype, "gyro": gyro, "active": False}

    def _close(self, iid):
        self.sdl.SDL_CloseGamepad(self.pads.pop(iid)["pad"])

    def _scan(self):
        """Open controllers that have appeared and close the ones that have gone. --pad N limits it to the Nth controller."""
        n = ctypes.c_int(0)
        ids = self.sdl.SDL_GetGamepads(ctypes.byref(n))
        if not ids:
            wanted = []
        else:
            try:
                wanted = [ids[i] for i in range(n.value)]
            finally:
                self.sdl.SDL_free(ids)
        if self.index is not None:
            wanted = wanted[self.index:self.index + 1] or wanted[:1]
        for iid in [i for i in self.pads if i not in wanted]:
            self._close(iid)
        for iid in wanted:
            if iid not in self.pads:
                self._open(iid)

    def poll(self):
        self.sdl.SDL_UpdateGamepads()
        for iid in [i for i, r in self.pads.items() if not self.sdl.SDL_GamepadConnected(r["pad"])]:
            self._close(iid)
        now = time.monotonic()
        if now >= self.next_scan:
            self.next_scan = now + (1.0 if self.pads else 0.5)
            self._scan()
        recs = list(self.pads.values())
        states = [self._state(r) for r in recs]
        # A GameCube adapter in PC mode shows up as four gamepads whether or not anything is plugged in. When there is more than one,
        # only the ones that have been touched are listed (the first of them if none has been yet).
        gc = [r for r in recs if r["type"] == 11]
        if len(gc) > 1:
            keep = [(r, s) for r, s in zip(recs, states) if r["type"] != 11 or r["active"]]
            states = [s for _, s in keep] or states[:1]
        return states

    def _state(self, r):
        pad = r["pad"]
        gb, ga = self.sdl.SDL_GetGamepadButton, self.sdl.SDL_GetGamepadAxis
        b = 0
        for sdl_btn, mask in self.BUTTONS.items():
            if gb(pad, sdl_btn):
                b |= mask
        # Extra buttons (SDL 15..25: misc1, paddles, touchpad click, misc2-6) as a bitmask, bit = index - 15.
        x = 0
        for idx in range(15, 26):
            if gb(pad, idx):
                x |= 1 << (idx - 15)
        # Raw joystick buttons (before SDL's gamepad mapping) as a bitmask, bit = raw index. For debugging.
        joy = self.sdl.SDL_GetGamepadJoystick(pad)
        raw = 0
        for idx in range(min(self.sdl.SDL_GetNumJoystickButtons(joy), 32) if joy else 0):
            if self.sdl.SDL_GetJoystickButton(joy, idx):
                raw |= 1 << idx
        # Touchpads: [down, x 0..1, y 0..1 (down), pressure] for each pad (0 = left, 1 = right).
        touch = []
        for t in range(min(self.sdl.SDL_GetNumGamepadTouchpads(pad), 2)):
            down, fx, fy, fp = ctypes.c_bool(), ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
            if self.sdl.SDL_GetGamepadTouchpadFinger(pad, t, 0, ctypes.byref(down), ctypes.byref(fx),
                                                     ctypes.byref(fy), ctypes.byref(fp)):
                touch.append([int(down.value), round(fx.value, 3), round(fy.value, 3), round(fp.value, 2)])
        # SDL: stick Y is positive-down, triggers are 0..32767.
        state = {"c": 1, "b": b, "x": x, "r": raw, "t": touch, "ty": r["type"], "nm": r["name"],
                 "lt": ga(pad, 4) * 255 // 32767, "rt": ga(pad, 5) * 255 // 32767,
                 "lx": ga(pad, 0), "ly": -ga(pad, 1) - (ga(pad, 1) == -32768),
                 "rx": ga(pad, 2), "ry": -ga(pad, 3) - (ga(pad, 3) == -32768)}
        if r["gyro"]:                     # angular speed in rad/s about SDL's x (pitch), y (yaw) and z (roll); rounded so a still pad sends nothing new
            g = (ctypes.c_float * 3)()
            if self.sdl.SDL_GetGamepadSensorData(pad, self.SENSOR_GYRO, g, 3):
                state["g"] = [round(g[0], 2), round(g[1], 2), round(g[2], 2)]
        if not r["active"] and (b or x or abs(state["lx"]) > 8000 or abs(state["ly"]) > 8000 or abs(state["rx"]) > 8000
                                or abs(state["ry"]) > 8000 or state["lt"] > 80 or state["rt"] > 80):
            r["active"] = True            # (only used to tell a plugged-in GameCube pad from an empty adapter port)
        return state


class XInputBackend:
    name = "XInput"
    fallback = True                              # only used when no other backend has found a controller (SDL sees Xbox pads too)

    def __init__(self, slot):
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self.dll = ctypes.WinDLL(name)
                break
            except OSError:
                continue
        else:
            raise OSError("XInput not available")
        self.slot, self.active = slot, ()

    def poll(self):
        states, active = [], []
        for i in ([self.slot] if self.slot is not None else range(4)):
            st = _XSTATE()
            if self.dll.XInputGetState(i, ctypes.byref(st)) == 0:
                g = st.Gamepad
                active.append(i)
                states.append({"c": 1, "b": g.wButtons, "lt": g.bLeftTrigger, "rt": g.bRightTrigger,
                               "lx": g.sThumbLX, "ly": g.sThumbLY, "rx": g.sThumbRX, "ry": g.sThumbRY})
        if tuple(active) != self.active:
            self.active = tuple(active)
            print(f"[pad] XInput slots {list(active)}" if active else "[pad] XInput: none")
        return states


def gamepad_thread(index):
    backends = []
    for cls in (SdlBackend, GcAdapterBackend, Switch2ProBackend, XInputBackend):
        try:
            backends.append(cls(index))
        except OSError as e:
            print(f"[pad] {cls.name} unavailable: {e}")
    if not backends:
        print("[pad] no controller backend available; controller disabled")
        return
    last = None
    # A backend that throws is skipped for a while (1 s, doubling up to 30 s, back to normal after one good poll) so the
    # others keep running at full rate. Its error message is also throttled: the first one is printed, then at most one per minute.
    retry_at = {}              # backend -> time.monotonic() before which it is not polled
    delay = {}                 # backend -> current back-off in seconds
    last_print = {}            # backend -> time.monotonic() of the last printed error
    repeats = {}               # backend -> errors seen since that print
    while True:
        states = []
        for b in backends:                          # every backend adds the controllers it sees (the fallback only if there are none yet)
            if getattr(b, "fallback", False) and states:
                continue
            now = time.monotonic()
            if now < retry_at.get(b, 0):            # backing off after an error: this backend adds nothing this time
                continue
            try:
                res = b.poll()
                delay.pop(b, None)                  # one good poll resets the back-off
            except Exception as e:  # noqa: BLE001 - one misbehaving backend must not stop the controller loop
                res = None
                delay[b] = min(30, delay.get(b, 0.5) * 2)
                retry_at[b] = now + delay[b]
                if now - last_print.get(b, -60) >= 60:
                    extra = f" (repeated {repeats.get(b, 0)} times)" if repeats.get(b) else ""
                    print(f"[pad] {b.name} error: {e!r}{extra}")
                    last_print[b] = now
                    repeats[b] = 0
                else:
                    repeats[b] = repeats.get(b, 0) + 1
            states.extend([res] if isinstance(res, dict) else res or [])
        states = states[:MAX_PADS]
        if states != last:
            if len(states) != len(last or []):
                print(f"[pad] {len(states)} controller(s) connected" if states else "[pad] controller disconnected")
            last = states
            hub.pad_state = states
            hub.emit({"p": states})
        time.sleep(1 / 120 if states else 0.5)


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------
LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}


@web.middleware
async def guard(request, handler):
    """Stop other websites from reading the keystroke stream or editing presets.

    A page on any origin can open ws://127.0.0.1:<port>, so reject cross-origin requests (Origin must match Host)
    and, when bound to loopback, requests whose Host isn't loopback (DNS rebinding).
    """
    host_header = request.headers.get("Host", "")
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != host_header:
        raise web.HTTPForbidden(text="cross-origin request refused")
    if request.app["loopback_only"] and host_header.rsplit(":", 1)[0] not in LOOPBACK:
        raise web.HTTPForbidden(text="unexpected Host header")
    # Switching presets is a plain GET, so a web page could trigger it with an <img>. Browsers label such requests;
    # Stream Deck / curl / OBS don't send this header at all, so they are unaffected.
    # "same-site" (another program on localhost:<other port>) is refused too: only this page itself may call the API.
    if request.path.startswith("/api/") and request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        raise web.HTTPForbidden(text="cross-site request refused")
    return await handler(request)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    await ws.send_str(json.dumps({"v": BUILD_ID}))
    hub.clients.add(ws)
    if hub.pad_state is not None:
        await ws.send_str(json.dumps({"p": hub.pad_state}, separators=(",", ":")))
    try:
        async for m in ws:
            if m.type == WSMsgType.ERROR:
                break
    finally:
        hub.clients.discard(ws)
    return ws


# Themes are CSS + assets that people share, so the overlay pages refuse to load anything from outside this server
# (a theme can't phone home with @import / url(https://...)) and can't run script: only this page's own scripts carry the
# per-request nonce, so script smuggled in through a theme's artwork has no nonce and is blocked.
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:]+\](:\d{1,5})?$")


def page_csp(request, nonce):
    host = request.host if HOST_RE.match(request.host or "") else "127.0.0.1"
    return (f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            f"font-src 'self' data:; connect-src 'self' ws://{host}; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'")


# Files from a theme folder are only ever sub-resources; if someone opens one directly, nothing in it may run.
THEME_FILE_CSP = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:"


def static_page(name):
    async def handler(request):
        nonce = secrets.token_urlsafe(16)
        html = (ROOT / name).read_text(encoding="utf-8").replace("<script>", f'<script nonce="{nonce}">')
        return web.Response(text=html, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store", "Content-Security-Policy": page_csp(request, nonce),
                                     "X-Content-Type-Options": "nosniff"})
    return handler


CONTENT_TYPES = {".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".gif": "image/gif",
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".ttf": "font/ttf", ".otf": "font/otf",
                 ".woff": "font/woff", ".woff2": "font/woff2"}


async def theme_file(request):
    p = request.app["themes"].resolve(request.match_info["id"], request.match_info["path"])
    if p is None:
        raise web.HTTPNotFound()
    resp = web.FileResponse(p, headers={"Cache-Control": "no-cache", "Content-Security-Policy": THEME_FILE_CSP,
                                        "X-Content-Type-Options": "nosniff"})
    resp.content_type = CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
    return resp


async def api_themes(request):
    t = request.app["themes"]
    return web.json_response({"themes": t.list(), "dir": str(t.user)})


async def api_open_themes(request):
    folder = request.app["themes"].ensure_user_dir()
    os.startfile(str(folder))            # opens the folder in Explorer (Windows)
    return web.json_response({"ok": True, "dir": str(folder)})


# --------------------------------------------------------------------------
# Update notice. About once a day (if "Check for new versions" is on) the program asks GitHub for the latest release of this project and
# remembers its version number and page. Nothing is downloaded or installed, nothing about you is sent, and the only place it shows is the
# Settings page and the tray menu. INPUT_OVERLAY_UPDATE_API exists for testing against a local stand-in.
# --------------------------------------------------------------------------
UPDATE_API = os.environ.get("INPUT_OVERLAY_UPDATE_API") or "https://api.github.com/repos/Ethral-T/input-overlay/releases/latest"
RELEASES_URL = "https://github.com/Ethral-T/input-overlay/releases/"
UPDATE_EVERY = 24 * 3600
update = {"checked": 0.0, "latest": None, "url": None, "error": None}


def parse_version(text):
    m = re.fullmatch(r"v?(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?", text or "")
    return tuple(int(g or 0) for g in m.groups()) if m else None


def update_snapshot():
    latest = parse_version(update["latest"])
    return {"enabled": config.shared().settings["updateCheck"], "current": VERSION, "latest": update["latest"], "url": update["url"],
            "available": bool(latest and latest > parse_version(VERSION)), "checked": update["checked"], "error": update["error"]}


async def check_update():
    """Ask GitHub for the latest release. Only a version number and a link on this project's own releases page are accepted from the reply."""
    try:
        headers = {"User-Agent": f"InputOverlay/{VERSION}", "Accept": "application/vnd.github+json"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), headers=headers) as s:
            async with s.get(UPDATE_API) as r:
                if r.status != 200:
                    raise ValueError(f"GitHub answered {r.status}")
                data = await r.json(content_type=None)
        tag, url = data.get("tag_name"), data.get("html_url")
        if parse_version(tag) is None or not isinstance(url, str) or not url.startswith(RELEASES_URL):
            raise ValueError("unexpected reply")
        update.update(latest=tag.lstrip("v"), url=url, error=None, checked=time.time())
    except Exception as e:  # noqa: BLE001 - no network, GitHub down, odd reply: just try again later
        update.update(error=str(e) or type(e).__name__, checked=time.time() - UPDATE_EVERY + 3600)         # try again in an hour
    return update_snapshot()


async def update_checker():
    await asyncio.sleep(20)                                   # let the program settle first
    while True:
        if config.shared().settings["updateCheck"] and time.time() - update["checked"] >= UPDATE_EVERY:
            await check_update()
        await asyncio.sleep(600)


async def api_update(request):
    return web.json_response(update_snapshot())


async def api_update_check(request):
    return web.json_response(await check_update())


async def api_list(request):
    cfg = request.app["config"]
    return web.json_response({"active": cfg.active, "settings": cfg.settings, "presets": cfg.as_list()})


async def api_put_settings(request):
    try:
        saved = request.app["config"].put_settings(await request.json())
    except (ValueError, TypeError) as e:
        raise web.HTTPBadRequest(text=str(e))
    hub.emit({"reload": 1})
    return web.json_response(saved)


def set_active(ref=None, step=0):
    """Switch the active preset (by name / 1-based position, or +1/-1 to cycle) and tell the overlays."""
    cfg = config.shared()
    name = cfg.cycle(step) if step else cfg.set_active(ref)
    hub.emit({"reload": 1})
    print(f"[preset] active: {name}")
    return name


async def api_switch(request):
    ref = request.match_info.get("name") or request.query.get("preset")
    if not ref:
        return web.json_response({"active": request.app["config"].active})   # /api/switch with no name = just report
    try:
        return web.json_response({"active": set_active(ref)})
    except KeyError:
        raise web.HTTPNotFound(text=f"no preset called {ref!r}")


def gyro_state(cfg):
    """The gyro mode the active preset shows right now: the shared (all presets) value if Gyro is shared, else the preset's own."""
    mode = cfg.settings["gyro"] if cfg.settings["scope"].get("gyro") else cfg.presets[cfg.active].get("gyro", "off")
    return mode, mode in ("tilt", "both"), mode in ("aim", "both")


def set_gyro(kind, action):
    """Turn the gyro tilt and/or aim display on, off or over (toggle) for EVERY preset: it sets the shared "all presets" gyro value (and marks
    Gyro as shared), saves it, and tells the overlays. kind: "tilt", "aim" or "all" (both); action: "on", "off" or "toggle"."""
    cfg = config.shared()
    with cfg.lock:
        _, tilt, aim = gyro_state(cfg)
        if kind == "all" and action == "toggle":          # "toggle both": off if either is showing, otherwise both on
            action = "off" if tilt or aim else "on"
        change = lambda cur: {"on": True, "off": False, "toggle": not cur}[action]
        if kind in ("tilt", "all"):
            tilt = change(tilt)
        if kind in ("aim", "all"):
            aim = change(aim)
        mode = "both" if tilt and aim else "tilt" if tilt else "aim" if aim else "off"
        cfg.put_settings({**cfg.settings, "gyro": mode, "scope": {**cfg.settings["scope"], "gyro": True}})
    hub.emit({"reload": 1})
    print(f"[gyro] all presets: {mode}")
    return {"applies_to": "all presets", "gyro": mode, "tilt": tilt, "aim": aim}


async def api_gyro(request):
    """/api/gyro (report), /api/gyro/on|off|toggle (both displays), /api/gyro/tilt[/on|off|toggle], /api/gyro/aim[/on|off|toggle]"""
    kind, action = request.match_info.get("kind"), request.match_info.get("action")
    if not kind:
        cfg = request.app["config"]
        mode, tilt, aim = gyro_state(cfg)
        return web.json_response({"applies_to": "all presets" if cfg.settings["scope"].get("gyro") else "each preset on its own",
                                  "gyro": mode, "tilt": tilt, "aim": aim})
    if kind in ("on", "off", "toggle") and not action:
        kind, action = "all", kind
    if kind not in ("tilt", "aim", "all") or (action or "toggle") not in ("on", "off", "toggle"):
        raise web.HTTPNotFound(text="use /api/gyro/{on|off|toggle} or /api/gyro/{tilt|aim}/{on|off|toggle}")
    return web.json_response(set_gyro(kind, action or "toggle"))


async def api_next(request):
    return web.json_response({"active": set_active(step=1)})


async def api_prev(request):
    return web.json_response({"active": set_active(step=-1)})


async def api_put(request):
    cfg = request.app["config"]
    name = request.match_info["name"]
    try:
        saved = cfg.put(name, await request.json(), rename_from=request.query.get("from"))
    except (ValueError, TypeError) as e:
        raise web.HTTPBadRequest(text=str(e))
    hub.emit({"reload": 1})        # running OBS sources pick up the change
    return web.json_response(saved)


async def api_delete(request):
    try:
        request.app["config"].delete(request.match_info["name"])
    except KeyError:
        raise web.HTTPNotFound()
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    hub.emit({"reload": 1})
    return web.json_response({"ok": True})


async def broadcaster():
    while True:
        msg = await hub.queue.get()
        for ws in list(hub.clients):
            try:
                await ws.send_str(msg)
            except Exception:  # noqa: BLE001 - a broken client must never stop the stream for everyone else
                hub.clients.discard(ws)


async def motion_flusher():
    while True:
        await asyncio.sleep(1 / 60)
        dx, dy = hub.take_motion()
        if dx or dy:
            hub.emit({"d": [dx, dy]})


async def stuck_key_sweeper():
    """Release keys whose key-up Windows never delivered (Win+L, a UAC prompt, focus moving to an admin game).
    Without this they stay in _down_keys forever: the overlay shows them held and the next real press is swallowed
    by the auto-repeat filter in on_press. Caveat: while a higher-integrity (admin) window has focus Windows may
    report keys as up, but that only releases keys the hook couldn't see properly anyway.

    The low-level keyboard hook runs a moment BEFORE Windows updates the state GetAsyncKeyState reads, so a key that was
    pressed just now can briefly read as "up". To never drop a key that is really held, a key is only released after it
    has read as up on two sweeps in a row (a quarter of a second apart)."""
    user32 = ctypes.WinDLL("user32")              # loaded here, not at import time, so the module still imports off Windows (tests)
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    looked_up = set()                             # keys that read as up on the previous sweep
    while True:
        await asyncio.sleep(0.25)
        up_now = set()
        for vk in list(_down_keys):                       # iterate a copy: the keyboard hook thread edits the set
            real = 13 if vk == 269 else vk                    # 269 = numpad Enter's own code, not a real VK; VK_RETURN covers both Enters
            if not user32.GetAsyncKeyState(real) & 0x8000:    # high bit set = down right now
                if vk in looked_up:                       # up on two sweeps in a row: the key-up really was missed
                    _down_keys.discard(vk)
                    hub.emit({"k": [vk, 0]})
                else:
                    up_now.add(vk)                        # maybe just the hook being ahead of Windows: check again next time
        looked_up = up_now


async def main(args):
    hub.loop = asyncio.get_running_loop()
    hub.queue = asyncio.Queue()

    keyboard.Listener(on_press=on_press, on_release=on_release, win32_event_filter=kb_filter).start()
    mouse.Listener(on_click=on_click, on_scroll=on_scroll).start()
    threading.Thread(target=raw_mouse_thread, daemon=True).start()
    threading.Thread(target=gamepad_thread, args=(args.pad,), daemon=True).start()

    app = web.Application(middlewares=[guard])
    app["config"] = config.shared()
    app["themes"] = themes.Themes(ROOT / "themes")
    app["themes"].ensure_user_dir()          # so the themes folder (and its README) exists for people to drop themes into
    app["loopback_only"] = args.host in LOOPBACK
    app.add_routes([web.get("/", static_page("index.html")), web.get("/settings", static_page("settings.html")),
                    web.get("/index.html", static_page("index.html")), web.get("/settings.html", static_page("settings.html")),
                    web.get("/ws", ws_handler), web.get("/api/presets", api_list), web.put("/api/settings", api_put_settings),
                    web.get("/api/themes", api_themes), web.post("/api/themes/open", api_open_themes),
                    web.get("/api/update", api_update), web.post("/api/update/check", api_update_check),
                    web.get("/themes/{id}/{path:.+}", theme_file),
                    web.put("/api/presets/{name}", api_put), web.delete("/api/presets/{name}", api_delete),
                    web.route("*", "/api/switch", api_switch), web.route("*", "/api/switch/{name}", api_switch),
                    web.route("*", "/api/next", api_next), web.route("*", "/api/prev", api_prev),
                    web.route("*", "/api/gyro", api_gyro), web.route("*", "/api/gyro/{kind}", api_gyro),
                    web.route("*", "/api/gyro/{kind}/{action}", api_gyro)])
    app.router.add_static("/", ROOT)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()

    print(f"Input Overlay running.\n  Settings: http://{args.host}:{args.port}/settings  (copy each preset's OBS URL from there)")
    if not app["loopback_only"]:
        print("WARNING: listening on a non-loopback address - anyone who can reach it can read your keystrokes.")
    print("Press Ctrl+C to stop.")
    await asyncio.gather(broadcaster(), motion_flusher(), update_checker(), stuck_key_sweeper())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--pad", type=int, default=None, help="controller index when several are connected (default: first)")
    # The global hook threads can keep the process alive after Ctrl+C, so exit hard.
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    signal.signal(signal.SIGBREAK, lambda *_: os._exit(0))
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
