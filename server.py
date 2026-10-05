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
import collections
import contextlib
import ctypes
import hmac
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import ssl
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
        self.clients = {}                 # websocket -> the queue of messages waiting to go to it
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
# Numpad Enter is the same virtual key as the main Enter (VK_RETURN), told apart only by the hook's "extended key" flag. pynput hands that flag
# to kb_filter on the hook's thread, but calls on_press / on_release later on its own thread, so one shared variable could belong to a newer event
# (an arrow key typed in between, say) and a plain Enter would be taken for numpad Enter. Instead every Return event queues its own flag, and
# the next Return press or release takes it back off, in the same order.
_return_flags = collections.deque(maxlen=64)


def kb_filter(msg, data):
    """Runs on the hook's thread for every key event, just before pynput queues it: note whether a Return key is the extended (numpad) one."""
    if data.vkCode == 13:
        _return_flags.append(bool(data.flags & 1))       # LLKHF_EXTENDED
    return True


def _vk(key):
    vk = vk_of(key)
    if vk == 13:                                         # main Enter stays 13; numpad Enter is the overlay's own code 269
        return 269 if (_return_flags.popleft() if _return_flags else False) else 13
    return vk


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
    SENSOR_GYRO = 2
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


class BackendRunner(threading.Thread):
    """Polls ONE controller backend on its own thread and keeps its latest controllers in `states`.

    Looking for a device that isn't there can be slow (the GameCube adapter reader scans every USB device, about 0.3 s, every couple of seconds
    while no adapter is plugged in). On the shared loop that froze every controller on screen for that long, over and over, so each backend
    gets a thread of its own and a slow one can only hold up itself.

    A backend that throws is skipped for a while (1 s, doubling up to 30 s, back to normal after one good poll) and contributes nothing meanwhile.
    Its error message is throttled: the first one is printed, then at most one per minute with a repeat count."""

    def __init__(self, backend):
        super().__init__(daemon=True, name="pad-" + backend.name)
        self.backend = backend
        self.states = []                  # replaced (never edited) by this thread; the merging loop only reads it
        self.retry_at = 0.0
        self.delay = 0.0
        self.last_print = -60.0
        self.repeats = 0

    def run(self):
        b = self.backend
        while True:
            now = time.monotonic()
            if now >= self.retry_at:
                try:
                    res = b.poll()
                    self.delay = 0.0                 # one good poll resets the back-off
                    self.states = [res] if isinstance(res, dict) else list(res or [])
                except Exception as e:  # noqa: BLE001 - one misbehaving backend must not stop the others
                    self.states = []
                    self.delay = min(30.0, (self.delay or 0.5) * 2)
                    self.retry_at = now + self.delay
                    if now - self.last_print >= 60:
                        extra = f" (repeated {self.repeats} times)" if self.repeats else ""
                        print(f"[pad] {b.name} error: {e!r}{extra}")
                        self.last_print, self.repeats = now, 0
                    else:
                        self.repeats += 1
            time.sleep(1 / 120 if self.states else 0.25)


def gamepad_thread(index):
    runners = []
    for cls in (SdlBackend, GcAdapterBackend, Switch2ProBackend, XInputBackend):
        try:
            runners.append(BackendRunner(cls(index)))
        except OSError as e:
            print(f"[pad] {cls.name} unavailable: {e}")
    if not runners:
        print("[pad] no controller backend available; controller disabled")
        return
    for r in runners:
        r.start()
    last = None
    while True:
        states = []
        for r in runners:                           # every backend adds the controllers it sees (the fallback only if there are none yet)
            if getattr(r.backend, "fallback", False) and states:
                continue
            states.extend(r.states)
        states = states[:MAX_PADS]
        if states != last:
            if len(states) != len(last or []):
                print(f"[pad] {len(states)} controller(s) connected" if states else "[pad] controller disconnected")
            last = states
            hub.pad_state = states
            hub.emit({"p": states})
        time.sleep(1 / 240 if states else 0.25)


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------
LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
LOOPBACK_ONLY = web.AppKey("loopback_only", bool)          # keys of the web application's shared state
CONFIG = web.AppKey("config", config.Config)
THEMES = web.AppKey("themes", themes.Themes)


# --------------------------------------------------------------------------
# LAN access (a second PC loading the overlay, as in two-PC streaming)
#
# By default the server answers this PC only. It will listen on a network address only when told to with --allow-lan, which takes the address(es)
# that may connect. Then, for anything that is not this PC:
#   1. the connecting address must be one of the allowed ones (a neighbour on the same network, or in the same building, is refused),
#   2. the request must carry a long random secret (a link with ?token=..., which sets a cookie), compared in constant time,
#   3. an address that sends the wrong secret too many times is shut out for a while.
# Allowed addresses can't be a whole big network (0.0.0.0/0 and the like are refused). None of this encrypts the traffic: see the README for how to
# make sure nobody on a shared network can capture it (a direct cable, a VPN such as Tailscale, or --tls-cert / --tls-key).
# --------------------------------------------------------------------------
LAN = {"networks": [], "token": None, "host": None, "port": None, "ssl": None}
TOKEN_FILE = config.APP_DIR / "lan-token.txt"
MIN_PREFIX = {4: 24, 6: 64}                          # the widest network that may be allowed: a /24 (IPv4, 256 addresses) or a /64 (IPv6)
FAIL_LIMIT, FAIL_WINDOW, FAIL_BLOCK = 10, 60.0, 300.0         # this many wrong secrets within 60 s shuts an address out for 5 minutes
_failures, _blocked = {}, {}


def add_network_args(ap):
    """The options every way of starting the server shares."""
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default: this PC only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--pad", type=int, default=None, help="controller index when several are connected (default: first)")
    ap.add_argument("--allow-lan", nargs="+", metavar="ADDRESS", default=None,
                    help="needed to listen on anything but this PC: the address(es) or network(s) allowed to connect, e.g. 192.168.1.50")
    ap.add_argument("--tls-cert", help="certificate file, to serve https/wss (with --tls-key)")
    ap.add_argument("--tls-key", help="private key file for --tls-cert")


def parse_allow_lan(values):
    """The networks named by --allow-lan (addresses or CIDR networks, spaces or commas between them). ValueError says what is wrong."""
    nets = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if not part:
                continue
            try:
                net = ipaddress.ip_network(part, strict=False)
            except ValueError:
                raise ValueError(f"--allow-lan: {part!r} is not an address or a network (an example: 192.168.1.50)") from None
            if net.prefixlen < MIN_PREFIX[net.version]:
                raise ValueError(f"--allow-lan: {part!r} is far too wide: it would let a whole network in. Name the one PC that needs access, "
                                 f"or at most a /{MIN_PREFIX[net.version]} network.")
            nets.append(net)
    return nets


def load_token():
    """The secret other PCs must present: created once, kept in the settings folder, and the same from then on. Delete the file for a new one."""
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(token) >= 20:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


def lan_setup(args):
    """Check the network options and switch LAN access on or off. Returns a message that says what is wrong, or None when all is well."""
    LAN.update(networks=[], token=None, host=args.host, port=args.port, ssl=None)
    cert, key = getattr(args, "tls_cert", None), getattr(args, "tls_key", None)
    if bool(cert) != bool(key):
        return "--tls-cert and --tls-key have to be given together."
    if cert:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert, key)
        except (OSError, ssl.SSLError) as e:
            return f"Could not load the certificate: {e}"
        LAN["ssl"] = ctx
    allow = getattr(args, "allow_lan", None)
    if args.host in LOOPBACK:
        return "--allow-lan only makes sense together with --host set to this PC's network address (it is %s now)." % args.host if allow else None
    if not allow:
        return (f"Input Overlay won't listen on {args.host}: that would let other devices on the network read every key you type.\n\n"
                "To let ONE other PC load the overlay (two-PC streaming), start it with --allow-lan and that PC's address, for example:\n"
                "    InputOverlay.exe --host 0.0.0.0 --allow-lan 192.168.1.50\n\n"
                "Only that address is let in, and it also needs the secret link shown in Settings.")
    try:
        LAN["networks"] = parse_allow_lan(allow)
    except ValueError as e:
        return str(e)
    if not LAN["networks"]:
        return "--allow-lan needs at least one address."
    LAN["token"] = load_token()
    return None


def remote_ip(request):
    """The address a request came from (an IPv4 address that arrived as ::ffff:a.b.c.d counts as the IPv4 address), or None if there isn't one."""
    try:
        ip = ipaddress.ip_address((request.remote or "").split("%")[0])
    except ValueError:
        return None
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip


def lan_verdict(ip, supplied, now=None):
    """None when a request may go on, otherwise why it is refused. This PC is always let in; anything else needs LAN access switched on, an
    allowed address and the secret."""
    now = time.monotonic() if now is None else now
    if ip is not None and ip.is_loopback:
        return None
    if not LAN["networks"]:
        return "this program only answers on this PC"
    if ip is None or not any(ip in net for net in LAN["networks"]):
        return "address not allowed"
    if _blocked.get(ip, 0) > now:
        return "too many wrong secrets: try again later"
    if supplied and hmac.compare_digest(supplied.encode(), LAN["token"].encode()):
        _failures.pop(ip, None)
        return None
    recent = [t for t in _failures.get(ip, []) if now - t < FAIL_WINDOW] + [now]
    _failures[ip] = recent
    if len(recent) >= FAIL_LIMIT:
        _blocked[ip] = now + FAIL_BLOCK
        _failures.pop(ip, None)
    return "wrong or missing secret"


def supplied_token(request):
    return request.cookies.get("io_token") or request.query.get("token") or request.headers.get("X-IO-Token") or ""


def local_addresses(host):
    """The addresses other PCs can use to reach this one: the one it listens on, or (when it listens on all of them) this PC's own."""
    if host not in ("0.0.0.0", "::", ""):
        return [host]
    found = []
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            a = info[4][0]
            if a not in found and not a.startswith(("127.", "169.254.")):
                found.append(a)
    return found


async def api_lan(request):
    """How another PC reaches this one, with the secret. Only ever answered to this PC itself, never to the network."""
    ip = remote_ip(request)
    if ip is None or not ip.is_loopback:
        raise web.HTTPForbidden(text="only answered to this PC")
    if not LAN["networks"]:
        return web.json_response({"enabled": False})
    scheme = "https" if LAN["ssl"] else "http"
    return web.json_response({"enabled": True, "tls": bool(LAN["ssl"]), "allowed": [str(n) for n in LAN["networks"]], "port": LAN["port"],
                              "urls": [f"{scheme}://{h}:{LAN['port']}/?token={LAN['token']}" for h in local_addresses(LAN["host"])]})


@web.middleware
async def guard(request, handler):
    """Stop other websites from reading the keystroke stream or editing presets.

    A page on any origin can open ws://127.0.0.1:<port>, so reject cross-origin requests (Origin must match Host)
    and, when bound to loopback, requests whose Host isn't loopback (DNS rebinding).
    """
    why = lan_verdict(remote_ip(request), supplied_token(request))
    if why:
        raise web.HTTPForbidden(text=why)
    host_header = request.headers.get("Host", "")
    origin = request.headers.get("Origin")
    if origin and urlparse(origin).netloc != host_header:
        raise web.HTTPForbidden(text="cross-origin request refused")
    if request.app[LOOPBACK_ONLY] and host_header.rsplit(":", 1)[0] not in LOOPBACK:
        raise web.HTTPForbidden(text="unexpected Host header")
    # Switching presets is a plain GET, so a web page could trigger it with an <img>. Browsers label such requests;
    # Stream Deck / curl / OBS don't send this header at all, so they are unaffected.
    # "same-site" (another program on localhost:<other port>) is refused too: only this page itself may call the API.
    if request.path.startswith("/api/") and request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        raise web.HTTPForbidden(text="cross-site request refused")
    return await handler(request)


@web.middleware
async def security_headers(request, handler):
    """Every response says "don't guess the file type" (the pages and theme files add their own, stricter headers on top)."""
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        e.headers.setdefault("X-Content-Type-Options", "nosniff")
        raise
    if not resp.prepared:
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        ip = remote_ip(request)
        if LAN["token"] and request.query.get("token") and ip is not None and not ip.is_loopback:
            # the guard already checked the secret: remember it, so the page's own requests and its WebSocket carry it without the link
            resp.set_cookie("io_token", LAN["token"], httponly=True, samesite="Strict", secure=request.secure, max_age=90 * 24 * 3600)
    return resp


CLIENT_QUEUE = 2000               # messages a client may have waiting before it counts as stalled (a few seconds of a busy controller)


async def client_sender(ws, queue):
    """Sends one client's messages, in order. It is a task of its own, so a client that stops reading (a frozen OBS source, a suspended tab) only
    holds up itself: after 5 seconds without being able to send, it is dropped, and the overlay reconnects and picks up from the current state."""
    try:
        while True:
            await asyncio.wait_for(ws.send_str(await queue.get()), 5)
    except Exception:  # noqa: BLE001 - closed, reset or too slow: drop this client
        pass
    finally:
        hub.clients.pop(ws, None)
        with contextlib.suppress(Exception):
            await ws.close()


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    await ws.send_str(json.dumps({"v": BUILD_ID}))
    queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
    hub.clients[ws] = queue
    sender = asyncio.create_task(client_sender(ws, queue))
    if hub.pad_state is not None:
        queue.put_nowait(json.dumps({"p": hub.pad_state}, separators=(",", ":")))
    try:
        async for m in ws:
            if m.type == WSMsgType.ERROR:
                break
    finally:
        hub.clients.pop(ws, None)
        sender.cancel()
    return ws


# Themes are CSS + assets that people share, so the overlay pages refuse to load anything from outside this server
# (a theme can't phone home with @import / url(https://...)) and can't run script: only this page's own scripts carry the
# per-request nonce, so script smuggled in through a theme's artwork has no nonce and is blocked.
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:]+\](:\d{1,5})?$")


def page_csp(request, nonce):
    host = request.host if HOST_RE.match(request.host or "") else "127.0.0.1"
    ws = "wss" if request.secure else "ws"
    return (f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            f"font-src 'self' data:; connect-src 'self' {ws}://{host}; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; "
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
    p = request.app[THEMES].resolve(request.match_info["id"], request.match_info["path"])
    if p is None:
        raise web.HTTPNotFound()
    resp = web.FileResponse(p, headers={"Cache-Control": "no-cache", "Content-Security-Policy": THEME_FILE_CSP,
                                        "X-Content-Type-Options": "nosniff"})
    resp.content_type = CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
    return resp


async def api_themes(request):
    t = request.app[THEMES]
    return web.json_response({"themes": t.list(), "dir": str(t.user)})


async def api_open_themes(request):
    folder = request.app[THEMES].ensure_user_dir()
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
    cfg = request.app[CONFIG]
    return web.json_response({"active": cfg.active, "settings": cfg.settings, "presets": cfg.as_list()})


async def api_put_settings(request):
    try:
        saved = request.app[CONFIG].put_settings(await request.json())
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
        return web.json_response({"active": request.app[CONFIG].active})   # /api/switch with no name = just report
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
        cfg = request.app[CONFIG]
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
    cfg = request.app[CONFIG]
    name = request.match_info["name"]
    try:
        saved = cfg.put(name, await request.json(), rename_from=request.query.get("from"))
    except (ValueError, TypeError) as e:
        raise web.HTTPBadRequest(text=str(e))
    hub.emit({"reload": 1})        # running OBS sources pick up the change
    return web.json_response(saved)


async def api_delete(request):
    try:
        request.app[CONFIG].delete(request.match_info["name"])
    except KeyError:
        raise web.HTTPNotFound()
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    hub.emit({"reload": 1})
    return web.json_response({"ok": True})


async def broadcaster():
    while True:
        msg = await hub.queue.get()
        for ws, queue in list(hub.clients.items()):
            try:
                queue.put_nowait(msg)
            except asyncio.QueueFull:               # this client has stopped reading: let it go; the others carry on
                hub.clients.pop(ws, None)
                asyncio.create_task(ws.close())


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
    problem = lan_setup(args)
    if problem:
        raise SystemExit(problem)
    hub.loop = asyncio.get_running_loop()
    hub.queue = asyncio.Queue()

    def quiet_resets(loop, context):
        """A browser or OBS dropping its connection makes Windows' asyncio log a ConnectionResetError traceback every time: nothing to act on."""
        if not isinstance(context.get("exception"), ConnectionResetError):
            loop.default_exception_handler(context)
    hub.loop.set_exception_handler(quiet_resets)

    keyboard.Listener(on_press=on_press, on_release=on_release, win32_event_filter=kb_filter).start()
    mouse.Listener(on_click=on_click, on_scroll=on_scroll).start()
    threading.Thread(target=raw_mouse_thread, daemon=True).start()
    threading.Thread(target=gamepad_thread, args=(args.pad,), daemon=True).start()

    app = web.Application(middlewares=[security_headers, guard])
    app[CONFIG] = config.shared()
    app[THEMES] = themes.Themes(ROOT / "themes")
    app[THEMES].ensure_user_dir()          # so the themes folder (and its README) exists for people to drop themes into
    app[LOOPBACK_ONLY] = args.host in LOOPBACK
    app.add_routes([web.get("/", static_page("index.html")), web.get("/settings", static_page("settings.html")),
                    web.get("/index.html", static_page("index.html")), web.get("/settings.html", static_page("settings.html")),
                    web.get("/ws", ws_handler), web.get("/api/presets", api_list), web.put("/api/settings", api_put_settings),
                    web.get("/api/themes", api_themes), web.post("/api/themes/open", api_open_themes),
                    web.get("/api/update", api_update), web.post("/api/update/check", api_update_check), web.get("/api/lan", api_lan),
                    web.get("/themes/{id}/{path:.+}", theme_file),
                    web.put("/api/presets/{name}", api_put), web.delete("/api/presets/{name}", api_delete),
                    web.route("*", "/api/switch", api_switch), web.route("*", "/api/switch/{name}", api_switch),
                    web.route("*", "/api/next", api_next), web.route("*", "/api/prev", api_prev),
                    web.route("*", "/api/gyro", api_gyro), web.route("*", "/api/gyro/{kind}", api_gyro),
                    web.route("*", "/api/gyro/{kind}/{action}", api_gyro)])
    app.router.add_static("/", ROOT)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port, ssl_context=LAN["ssl"]).start()

    scheme = "https" if LAN["ssl"] else "http"
    print(f"Input Overlay running.\n  Settings: {scheme}://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/settings  (copy each preset's OBS URL from there)")
    if LAN["networks"]:
        print(f"LAN access is ON. Allowed: {', '.join(str(n) for n in LAN['networks'])}. Other PCs open one of these links (they carry the secret):")
        for h in local_addresses(args.host):
            print(f"    {scheme}://{h}:{args.port}/?token={LAN['token']}")
        if not LAN["ssl"]:
            print("  The traffic is not encrypted: use a direct cable, a VPN or --tls-cert/--tls-key if others share your network (see the README).")
    print("Press Ctrl+C to stop.")
    await asyncio.gather(broadcaster(), motion_flusher(), update_checker(), stuck_key_sweeper())


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_network_args(p)
    # The global hook threads can keep the process alive after Ctrl+C, so exit hard.
    signal.signal(signal.SIGINT, lambda *_: os._exit(0))
    signal.signal(signal.SIGBREAK, lambda *_: os._exit(0))
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
