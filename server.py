"""Input Overlay server.

Captures global keyboard / mouse / XInput-controller input on Windows and
streams it over a WebSocket to the overlay page (overlay/index.html), which is
meant to be loaded as an OBS Browser Source.

Messages (JSON, server -> overlay):
  {"k": [vk, 1|0]}              key down / up (Windows virtual-key code)
  {"m": [button, 1|0]}          mouse button: left right middle x1 x2
  {"d": [dx, dy]}               raw mouse movement since last flush
  {"s": [dx, dy]}               scroll wheel ticks
  {"p": {...}}                  gamepad state (buttons bitmask, triggers, sticks)
"""
import argparse
import asyncio
import ctypes
import json
import os
import signal
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web, WSMsgType
from pynput import keyboard, mouse

import config
import themes
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


def on_press(key):
    vk = vk_of(key)
    if vk is None or vk in _down_keys:  # ignore auto-repeat
        return
    _down_keys.add(vk)
    hub.emit({"k": [vk, 1]})


def on_release(key):
    vk = vk_of(key)
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
        self.pad = None
        self.pad_name, self.pad_type = "", 0
        self.gyro = False                         # the open pad reports a gyroscope and it is switched on

    def _open(self):
        n = ctypes.c_int(0)
        ids = self.sdl.SDL_GetGamepads(ctypes.byref(n))
        if not ids:
            return
        try:
            if n.value:
                i = self.index if self.index is not None and self.index < n.value else 0
                self.pad = self.sdl.SDL_OpenGamepad(ids[i])
                if self.pad:
                    self.pad_name = (self.sdl.SDL_GetGamepadName(self.pad) or b"").decode(errors="replace")
                    self.pad_type = self.sdl.SDL_GetGamepadType(self.pad)
                    print(f"[pad] {self.name}: {self.pad_name} (SDL type {self.pad_type})")
                    has = bool(self.sdl.SDL_GamepadHasSensor(self.pad, self.SENSOR_GYRO))
                    self.gyro = has and bool(self.sdl.SDL_SetGamepadSensorEnabled(self.pad, self.SENSOR_GYRO, True))
                    print(f"[pad] {self.name}: gyro " + ("on" if self.gyro else "available but could not be enabled" if has else "not reported by this controller"))
        finally:
            self.sdl.SDL_free(ids)

    def poll(self):
        self.sdl.SDL_UpdateGamepads()
        if self.pad and not self.sdl.SDL_GamepadConnected(self.pad):
            self.sdl.SDL_CloseGamepad(self.pad)
            self.pad = None
            self.gyro = False
        if not self.pad:
            self._open()
            if not self.pad:
                return None
        gb, ga = self.sdl.SDL_GetGamepadButton, self.sdl.SDL_GetGamepadAxis
        b = 0
        for sdl_btn, mask in self.BUTTONS.items():
            if gb(self.pad, sdl_btn):
                b |= mask
        # Extra buttons (SDL 15..25: misc1, paddles, touchpad click, misc2-6) as a bitmask, bit = index - 15.
        x = 0
        for idx in range(15, 26):
            if gb(self.pad, idx):
                x |= 1 << (idx - 15)
        # Raw joystick buttons (before SDL's gamepad mapping) as a bitmask, bit = raw index. For debugging.
        joy = self.sdl.SDL_GetGamepadJoystick(self.pad)
        r = 0
        for idx in range(min(self.sdl.SDL_GetNumJoystickButtons(joy), 32) if joy else 0):
            if self.sdl.SDL_GetJoystickButton(joy, idx):
                r |= 1 << idx
        # Touchpads: [down, x 0..1, y 0..1 (down), pressure] for each pad (0 = left, 1 = right).
        touch = []
        for t in range(min(self.sdl.SDL_GetNumGamepadTouchpads(self.pad), 2)):
            down, fx, fy, fp = ctypes.c_bool(), ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
            if self.sdl.SDL_GetGamepadTouchpadFinger(self.pad, t, 0, ctypes.byref(down), ctypes.byref(fx),
                                                     ctypes.byref(fy), ctypes.byref(fp)):
                touch.append([int(down.value), round(fx.value, 3), round(fy.value, 3), round(fp.value, 2)])
        # SDL: stick Y is positive-down, triggers are 0..32767.
        state = {"c": 1, "b": b, "x": x, "r": r, "t": touch, "ty": self.pad_type, "nm": self.pad_name,
                 "lt": ga(self.pad, 4) * 255 // 32767, "rt": ga(self.pad, 5) * 255 // 32767,
                 "lx": ga(self.pad, 0), "ly": -ga(self.pad, 1) - (ga(self.pad, 1) == -32768),
                 "rx": ga(self.pad, 2), "ry": -ga(self.pad, 3) - (ga(self.pad, 3) == -32768)}
        if self.gyro:                     # angular speed in rad/s about SDL's x (pitch), y (yaw) and z (roll); rounded so a still pad sends nothing new
            g = (ctypes.c_float * 3)()
            if self.sdl.SDL_GetGamepadSensorData(self.pad, self.SENSOR_GYRO, g, 3):
                state["g"] = [round(g[0], 2), round(g[1], 2), round(g[2], 2)]
        return state


class XInputBackend:
    name = "XInput"

    def __init__(self, slot):
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self.dll = ctypes.WinDLL(name)
                break
            except OSError:
                continue
        else:
            raise OSError("XInput not available")
        self.slot, self.active = slot, None

    def poll(self):
        found = None
        for i in ([self.slot] if self.slot is not None else range(4)):
            st = _XSTATE()
            if self.dll.XInputGetState(i, ctypes.byref(st)) == 0:
                found = (i, st)
                if self.active is None or i == self.active:
                    break
        if not found:
            self.active = None
            return None
        i, st = found
        if self.active != i:
            self.active = i
            print(f"[pad] XInput slot {i}")
        g = st.Gamepad
        return {"c": 1, "b": g.wButtons, "lt": g.bLeftTrigger, "rt": g.bRightTrigger,
                "lx": g.sThumbLX, "ly": g.sThumbLY, "rx": g.sThumbRX, "ry": g.sThumbRY}


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
    last, announced = None, False
    while True:
        state = None
        for b in backends:  # first backend that sees a pad wins
            state = b.poll()
            if state:
                break
        if state is None:
            state = {"c": 0}
        if state != last:
            if state["c"] != (last or {}).get("c"):
                print("[pad] controller connected" if state["c"] else "[pad] controller disconnected")
            last = state
            hub.pad_state = state
            hub.emit({"p": state})
        time.sleep(1 / 120 if state["c"] else 0.5)


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
    if request.path.startswith("/api/") and request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise web.HTTPForbidden(text="cross-site request refused")
    return await handler(request)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    await ws.send_str(json.dumps({"v": BUILD_ID}))
    hub.clients.add(ws)
    if hub.pad_state:
        await ws.send_str(json.dumps({"p": hub.pad_state}, separators=(",", ":")))
    try:
        async for m in ws:
            if m.type == WSMsgType.ERROR:
                break
    finally:
        hub.clients.discard(ws)
    return ws


# Themes are CSS + assets that people share, so the overlay pages refuse to load anything from outside this server
# (a theme can't phone home with @import / url(https://...)) and, being CSS-only, can't run script.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self' data:; connect-src 'self' ws: wss:; frame-src 'self'; object-src 'none'; base-uri 'self'")


def static_page(name):
    async def handler(request):
        return web.FileResponse(ROOT / name, headers={"Cache-Control": "no-store", "Content-Security-Policy": CSP})
    return handler


CONTENT_TYPES = {".css": "text/css", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".gif": "image/gif",
                 ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".ttf": "font/ttf", ".otf": "font/otf",
                 ".woff": "font/woff", ".woff2": "font/woff2"}


async def theme_file(request):
    p = request.app["themes"].resolve(request.match_info["id"], request.match_info["path"])
    if p is None:
        raise web.HTTPNotFound()
    resp = web.FileResponse(p, headers={"Cache-Control": "no-cache"})
    resp.content_type = CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
    return resp


async def api_themes(request):
    t = request.app["themes"]
    return web.json_response({"themes": t.list(), "dir": str(t.user)})


async def api_open_themes(request):
    folder = request.app["themes"].ensure_user_dir()
    os.startfile(str(folder))            # opens the folder in Explorer (Windows)
    return web.json_response({"ok": True, "dir": str(folder)})


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
            except ConnectionError:
                hub.clients.discard(ws)


async def motion_flusher():
    while True:
        await asyncio.sleep(1 / 60)
        dx, dy = hub.take_motion()
        if dx or dy:
            hub.emit({"d": [dx, dy]})


async def main(args):
    hub.loop = asyncio.get_running_loop()
    hub.queue = asyncio.Queue()

    keyboard.Listener(on_press=on_press, on_release=on_release).start()
    mouse.Listener(on_click=on_click, on_scroll=on_scroll).start()
    threading.Thread(target=raw_mouse_thread, daemon=True).start()
    threading.Thread(target=gamepad_thread, args=(args.pad,), daemon=True).start()

    app = web.Application(middlewares=[guard])
    app["config"] = config.shared()
    app["themes"] = themes.Themes(ROOT / "themes")
    app["themes"].ensure_user_dir()          # so the themes folder (and its README) exists for people to drop themes into
    app["loopback_only"] = args.host in LOOPBACK
    app.add_routes([web.get("/", static_page("index.html")), web.get("/settings", static_page("settings.html")),
                    web.get("/ws", ws_handler), web.get("/api/presets", api_list), web.put("/api/settings", api_put_settings),
                    web.get("/api/themes", api_themes), web.post("/api/themes/open", api_open_themes),
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
    await asyncio.gather(broadcaster(), motion_flusher())


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
