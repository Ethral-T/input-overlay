"""Input Overlay server.

Captures global keyboard, mouse and controller input on Windows and Linux and
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
import re
import secrets
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web, WSMsgType

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


# pynput is the Windows hook. On Linux its XRecord listener only sees keys this
# process injects, so the Linux path uses XInput2 instead (see linux_input_thread).
if sys.platform == "win32":
    from pynput import keyboard, mouse


# --------------------------------------------------------------------------
# Keyboard + mouse buttons/scroll
# --------------------------------------------------------------------------
# X11 keysyms for the keys whose Windows virtual-key code is not the ASCII value.
# Letters and digits use the character (Windows VK for "A" is 65, not the keysym).
_XSYM_VK = {
    0xFF08: 8, 0xFF09: 9, 0xFF0D: 13, 0xFF1B: 27, 0xFF13: 19, 0xFF14: 145,
    0xFF50: 36, 0xFF51: 37, 0xFF52: 38, 0xFF53: 39, 0xFF54: 40, 0xFF55: 33, 0xFF56: 34, 0xFF57: 35,
    0xFF61: 44, 0xFF63: 45, 0xFF67: 93, 0xFF7F: 144, 0xFFFF: 46,
    0xFFE1: 160, 0xFFE2: 161, 0xFFE3: 162, 0xFFE4: 163, 0xFFE5: 20,
    0xFFE9: 164, 0xFFEA: 165, 0xFE03: 165, 0xFFEB: 91, 0xFFEC: 92,
}
_XSYM_VK.update({0xFFBE + i: 112 + i for i in range(12)})   # F1..F12
# Keypad. XKB stores navigation on level 1 and the digit on level 2; Num Lock picks the level.
_XSYM_VK.update({
    0xFF8D: 13,                                                    # KP_Enter -> Enter
    0xFF95: 36, 0xFF96: 37, 0xFF97: 38, 0xFF98: 39, 0xFF99: 40,   # Home Left Up Right Down
    0xFF9A: 33, 0xFF9B: 34, 0xFF9C: 35, 0xFF9D: 12,               # PgUp PgDn End Clear (numpad 5)
    0xFF9E: 45, 0xFF9F: 46,                                        # Insert Delete
    0xFFAA: 106, 0xFFAB: 107, 0xFFAC: 108, 0xFFAD: 109, 0xFFAE: 110, 0xFFAF: 111,
})
_XSYM_VK.update({0xFFB0 + i: 96 + i for i in range(10)})           # KP_0..KP_9
# Level 1 of a keypad key (num lock off) and level 2 (num lock on).
_KP_NAV = {0xFF95, 0xFF96, 0xFF97, 0xFF98, 0xFF99, 0xFF9A, 0xFF9B, 0xFF9C, 0xFF9D, 0xFF9E, 0xFF9F}
_KP_NUMBER = set(range(0xFFB0, 0xFFBA)) | {0xFFAE}
_CHAR_VK = {
    " ": 32, "`": 192, "-": 189, "=": 187, "[": 219, "]": 221, "\\": 220,
    ";": 186, "'": 222, ",": 188, ".": 190, "/": 191,
}


def _linux_vk(keysym, char):
    """Windows virtual-key code for an X11 keysym / typed character."""
    if char and len(char) == 1:
        if "a" <= char <= "z" or "A" <= char <= "Z":
            return ord(char.upper())
        if "0" <= char <= "9":
            return ord(char)
        if char in _CHAR_VK:
            return _CHAR_VK[char]
    if not isinstance(keysym, int):
        return None
    if keysym in _XSYM_VK:
        return _XSYM_VK[keysym]
    if 0x20 <= keysym <= 0x7E:
        return _linux_vk(None, chr(keysym))
    return None


def vk_of(key):
    """Windows virtual-key code from a pynput key. The Windows hooks are the only caller;
    on Linux the XInput2 thread resolves keysyms with _linux_vk instead."""
    vk = getattr(key, "vk", None)
    if vk is None:
        vk = getattr(getattr(key, "value", None), "vk", None)
    return vk


_down_keys = set()
_down_buttons = set()
# X keycode -> the virtual-key emitted on press. The release uses this rather
# than the modifiers at release time, so a Num Lock or Shift change in between
# cannot leave the pressed key lit.
_held_keycodes = {}


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
def _parse_xi_raw_motion(data):
    """(sourceid, x, y) from an XI_RawMotion generic-event payload, or None.

    `data` is the event bytes after the 10-byte generic-event header. Axes 0 and 1
    are the pointer's X and Y. The raw (unaccelerated) values are used.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 22:
        return None
    _device, _time, _detail, source, vlen, _flags = struct.unpack_from("<HIIHHI", data, 0)
    off = 22
    if vlen < 1 or len(data) < off + 4 * vlen:
        return None
    mask = 0
    for i in range(vlen):
        mask |= struct.unpack_from("<I", data, off)[0] << (32 * i)
        off += 4
    n = mask.bit_count()
    if n == 0 or len(data) < off + n * 16:
        return None
    raw_at = off + n * 8                                 # axis values, then the raw values

    def fp3232(o):
        integ, frac = struct.unpack_from("<iI", data, o)
        return integ + frac / 4294967296

    vals = {}
    bit_i = 0
    for bit in range(vlen * 32):
        if mask & (1 << bit):
            vals[bit] = fp3232(raw_at + bit_i * 8)
            bit_i += 1
            if bit_i >= n:
                break
    if 0 not in vals or 1 not in vals:
        return None
    return source, vals[0], vals[1]


def _pointer_kind(dev, valuator_class):
    """'absolute', 'xtest', or 'relative'.

    XIModeAbsolute valuators are coordinates (a tablet or a touchscreen).
    A relative-mode device sends deltas, including a real mouse whose valuator
    happens to read back as the cursor position under Xorg. Treating that
    valuator as a coordinate turned the next delta into a jump across the screen.

    XTEST is the one relative-mode device that sends both. An absolute XTest
    move carries the cursor coordinate; a relative XTest move carries the delta.
    The device name is not enough to decide which, and neither is the valuator
    value at startup, so each XTEST sample is split later.
    """
    axes = {}
    for item in dev.classes:
        if getattr(item, "type", None) == valuator_class and getattr(item, "number", None) in (0, 1):
            axes[item.number] = item
    if 0 not in axes or 1 not in axes:
        return "relative"
    if axes[0].mode == 1 and axes[1].mode == 1:          # XIModeAbsolute
        return "absolute"
    if "XTEST" in (getattr(dev, "name", "") or ""):
        return "xtest"
    return "relative"


def _position_delta(source, x, y, last):
    """Pixel change of an absolute valuator. The first sample is only a baseline.

    The numbers are device coordinates. A tablet's range is not the screen size,
    so a sample past the edge of the monitor is still a real move.
    """
    prev = last.get(source)
    last[source] = (x, y)
    if prev is None:
        return 0.0, 0.0
    return x - prev[0], y - prev[1]


def _clamp_screen(x, y, width, height):
    """Cursor position after the server clamps a coordinate into the screen."""
    cx = 0 if x < 0 else (width - 1 if x >= width else int(round(x)))
    cy = 0 if y < 0 else (height - 1 if y >= height else int(round(y)))
    return cx, cy


def _offscreen(x, y, width, height):
    return x < 0 or y < 0 or x >= width or y >= height


def _xtest_absolute(samples, origin, width, height):
    """Deltas if every sample is a cursor coordinate, and the cursor that ends on."""
    prev = origin
    deltas = []
    for x, y in samples:
        end = _clamp_screen(x, y, width, height)
        if _offscreen(x, y, width, height):
            deltas.append((0, 0))
        else:
            deltas.append((end[0] - prev[0], end[1] - prev[1]))
        prev = end
    return deltas, prev


def _xtest_relative_end(samples, origin, width, height):
    """Where the cursor ends if every sample is a delta."""
    pos = origin
    for x, y in samples:
        pos = _clamp_screen(pos[0] + x, pos[1] + y, width, height)
    return pos


def _xtest_deltas(samples, origin, pointer, width, height):
    """Deltas for one batch of XTEST samples, and the cursor to remember.

    The raw event does not say whether a sample is a coordinate or a delta, and
    the two are not the same number: a relative +40,+8 while the cursor is at
    (2, 0) arrives as (40, 8), not (42, 8). A sample that lands on the cursor is
    a coordinate. A sample past the edge of the screen, while the cursor sits on
    that edge, is the corner clamp (XTEST reports -1, or a point past the far
    edge, and the cursor stays put). Subtracting that sample from an older
    cursor is the jump a 1px nudge used to cause, so the clamp contributes no
    delta and the remembered cursor becomes the real one.

    Several samples can be waiting, and the cursor has already moved through all
    of them. Each sample is then either a coordinate or a delta. The combination
    that starts at `origin` and finishes on `pointer` is the one that happened;
    a coordinate is preferred when both readings fit, which is also what a
    relative move from the origin looks like, and both readings give the same
    delta in that case. One delta is returned per sample, including a long
    drag: a coordinate batch is never summed by treating the positions as deltas.
    """
    px, py = int(round(pointer[0])), int(round(pointer[1]))
    ox, oy = int(round(origin[0])), int(round(origin[1]))
    if not samples:
        return [], (px, py)
    # A coordinate reading of the whole batch. Checked first, for any length:
    # a drag of dozens of samples is all coordinates, and searching every mix
    # of coordinate and delta is not possible once the batch is long. The
    # per-sample deltas sum to the pointer movement. An off-screen sample
    # (the corner clamp) contributes nothing.
    absolute, abs_end = _xtest_absolute(samples, (ox, oy), width, height)
    if abs_end == (px, py):
        return absolute, (px, py)
    if _xtest_relative_end(samples, (ox, oy), width, height) == (px, py):
        return [(x, y) for x, y in samples], (px, py)
    if len(samples) > 12:
        # Neither pure reading lands on the cursor. Passing the coordinates
        # through as deltas is how a long drag turned into tens of thousands
        # of pixels, so this batch contributes nothing rather than that.
        return [(0, 0)] * len(samples), (px, py)

    chosen = []

    def walk(index, pos, path):
        if chosen:
            return
        if index == len(samples):
            if pos == (px, py):
                chosen.append(list(path))
            return
        x, y = samples[index]
        end = _clamp_screen(x, y, width, height)
        path.append(("abs", x, y, end))
        walk(index + 1, end, path)
        path.pop()
        if chosen:
            return
        moved = _clamp_screen(pos[0] + x, pos[1] + y, width, height)
        path.append(("rel", x, y, moved))
        walk(index + 1, moved, path)
        path.pop()

    walk(0, (ox, oy), [])
    if not chosen:
        return [(x, y) for x, y in samples], (px, py)
    deltas = []
    prev = (ox, oy)
    for kind, x, y, end in chosen[0]:
        if kind == "rel":
            deltas.append((x, y))
        elif _offscreen(x, y, width, height):
            deltas.append((0, 0))
        else:
            deltas.append((end[0] - prev[0], end[1] - prev[1]))
        prev = end
    return deltas, (px, py)


def _numlock_mask(display):
    """Modifier bit for Num Lock, or 0 when the keymap has no such key."""
    code = display.keysym_to_keycode(0xFF7F)
    if not code:
        return 0
    for index, codes in enumerate(display.get_modifier_mapping()):
        if code in codes:
            return 1 << index
    return 0


def _keysym_for_keycode(display, keycode, numlock, shift):
    """Keysym for a keycode, honouring the keypad's Num Lock / Shift levels.

    XKB's KEYPAD type puts navigation on level 1 and the digit on level 2.
    Num Lock selects level 2, and Shift swaps the two, matching Windows.
    """
    sym0 = display.keycode_to_keysym(keycode, 0) or 0
    sym1 = display.keycode_to_keysym(keycode, 1) or 0
    if sym0 in _KP_NAV and sym1 in _KP_NUMBER and (numlock != shift):
        return sym1
    return sym0


def _release_held_input():
    """Key-up and button-up for everything still held, so a dropped connection can't leave keys lit."""
    _held_keycodes.clear()
    for vk in list(_down_keys):
        _down_keys.discard(vk)
        hub.emit({"k": [vk, 0]})
    for name in list(_down_buttons):
        _down_buttons.discard(name)
        hub.emit({"m": [name, 0]})


# XInput2 button numbers. 4/5 (and 6/7) are the scroll wheel, not real buttons.
_XI_BUTTONS = {1: "left", 2: "middle", 3: "right", 8: "x1", 9: "x2"}
_XI_SCROLL = {4: (0, 1), 5: (0, -1), 6: (-1, 0), 7: (1, 0)}


def _xi_detail(data):
    """Keycode or button number from an XI raw-event payload, or None."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 10:
        return None
    return struct.unpack_from("<I", data, 6)[0]


def _linux_devices(display, root, xinput):
    """Slave keyboards and pointers, and how each pointer reports motion.

    Masters are left out: a master repeats every slave event. Floating slaves
    (not attached to a seat) are included. The masks are returned and not
    selected yet, so a rescan can compare them with the current set first.
    `absolute` devices send coordinates. `xtest` is the virtual pointer, which
    has to be split per sample. Every other pointer sends deltas.
    """
    info = display.xinput_query_device(xinput.AllDevices)
    pos = root.query_pointer()
    pointer_mask = xinput.RawMotionMask | xinput.RawButtonPressMask | xinput.RawButtonReleaseMask
    key_mask = xinput.RawKeyPressMask | xinput.RawKeyReleaseMask
    pointers, keyboards, absolute, xtest, masks = [], [], set(), set(), []
    for dev in info.devices:
        if not dev.enabled:
            continue
        classes = {getattr(item, "type", None) for item in dev.classes}
        floating_key = dev.use == xinput.FloatingSlave and xinput.KeyClass in classes and xinput.ValuatorClass not in classes
        floating_ptr = dev.use == xinput.FloatingSlave and xinput.ValuatorClass in classes
        if dev.use == xinput.SlaveKeyboard or floating_key:
            keyboards.append(dev.deviceid)
            masks.append((dev.deviceid, key_mask))
        elif dev.use == xinput.SlavePointer or floating_ptr:
            pointers.append(dev.deviceid)
            masks.append((dev.deviceid, pointer_mask))
            kind = _pointer_kind(dev, xinput.ValuatorClass)
            if kind == "absolute":
                absolute.add(dev.deviceid)
            elif kind == "xtest":
                xtest.add(dev.deviceid)
    if not masks:
        return None
    return {
        "pointers": pointers, "keyboards": keyboards, "absolute": absolute, "xtest": xtest,
        "masks": masks, "width": display.screen().width_in_pixels,
        "height": display.screen().height_in_pixels, "seed": (pos.root_x, pos.root_y),
    }


def _select_linux_devices(display, root, xinput):
    """Choose the current slaves and select their raw events."""
    found = _linux_devices(display, root, xinput)
    if not found:
        return None
    root.xinput_select_events(found["masks"])
    display.flush()
    return found


# How often to look for keyboards and mice that were plugged in. Short enough
# that a hotplug is noticed while keys are still being held.
_DEVICE_SCAN_S = 1.5
# A remote-desktop drag can queue far more than a dozen motion events before
# this process reads them. They have to be classified together: the cursor
# read afterwards is the cursor after every event still waiting.
_EVENT_BATCH = 4096


def _x_socket_ready(display, timeout):
    """True when an X event is waiting. A timeout means the caller should rescan devices."""
    if display.pending_events():
        return True
    return bool(select.select([display], [], [], timeout)[0])


def _batch_has_xtest(events, xinput, xtest_ids):
    """True when a queued event is raw motion from the XTEST pointer."""
    if not xtest_ids:
        return False
    for event in events:
        if getattr(event, "evtype", None) != xinput.RawMotion:
            continue
        parsed = _parse_xi_raw_motion(getattr(event, "data", b""))
        if parsed and parsed[0] in xtest_ids:
            return True
    return False


def _shift_keycodes(display):
    """Keycodes that are Shift. Modifier index 0 is Shift on the standard map."""
    mapping = display.get_modifier_mapping()
    if not mapping:
        return set()
    return {code for code in mapping[0] if code}


def _keycode_is_down(keymap, code):
    """True when query_keymap()'s bit vector says this keycode is held."""
    if code < 0 or code // 8 >= len(keymap):
        return False
    return bool(keymap[code // 8] & (1 << (code % 8)))


def _shifts_held(display, shift_codes):
    """Shift keycodes that are down right now. Left and right are separate bits."""
    keymap = display.query_keymap()
    return {code for code in shift_codes if _keycode_is_down(keymap, code)}


def _linux_input_session(display, xinput, X):
    """Read one X connection until it drops. Raises ConnectionClosedError when the server goes away."""
    display.xinput_query_version()
    root = display.screen().root
    display._overlay_ready = True
    num_mask = _numlock_mask(display)
    numlock_code = display.keysym_to_keycode(0xFF7F) or 0
    shift_codes = _shift_keycodes(display)
    # Num Lock and Shift are tracked from the events themselves. query_pointer()
    # is the live mask, so a Shift release already sitting in the queue would
    # otherwise change which keypad key the press maps to.
    mask = root.query_pointer().mask
    held_shifts = _shifts_held(display, shift_codes)
    mods = {"shift": bool(held_shifts), "shifts": held_shifts, "numlock": bool(mask & num_mask)}
    # The cursor, for XTEST only. An absolute device is not seeded from the mouse:
    # its own first sample is the baseline.
    last = {}
    selected = None
    announced = False
    none_logged = False
    last_scan = 0.0

    def remember(fresh):
        """Keep XTEST's cursor and drop devices that left. Does not select events."""
        nonlocal selected
        if not fresh:
            selected = None
            return
        for source in fresh["xtest"]:
            if source not in last:
                last[source] = fresh["seed"]
        keep = set(fresh["xtest"]) | set(fresh["absolute"])
        for source in list(last):
            if source not in keep:
                last.pop(source, None)
        selected = fresh

    def emit_motion(dx, dy):
        if dx or dy:
            hub.add_motion(int(round(dx)), int(round(dy)))

    def dispatch(events, pointer):
        """Handle a batch of events. `pointer` is the cursor after the batch, if an XTEST move was in it."""
        if not selected:
            return
        grouped = {}
        for index, event in enumerate(events):
            if getattr(event, "evtype", None) != xinput.RawMotion:
                continue
            parsed = _parse_xi_raw_motion(getattr(event, "data", b""))
            if not parsed or parsed[0] not in selected["xtest"]:
                continue
            grouped.setdefault(parsed[0], []).append((index, parsed[1], parsed[2]))
        xtest_at = {}
        if pointer is not None:
            width, height = selected["width"], selected["height"]
            for source, items in grouped.items():
                origin = last.get(source, pointer)
                deltas, new_origin = _xtest_deltas([(x, y) for _, x, y in items], origin, pointer, width, height)
                last[source] = new_origin
                # Every XTEST sample is consumed here. One that was left out used
                # to be read as a relative delta, so a coordinate became a jump.
                for index, _x, _y in items:
                    xtest_at[index] = (0, 0)
                for (index, _x, _y), (dx, dy) in zip(items, deltas):
                    xtest_at[index] = (dx, dy)
        for index, event in enumerate(events):
            if not selected:
                return
            try:
                _dispatch_one(display, root, xinput, X, event, index, xtest_at, selected, mods,
                              shift_codes, numlock_code, last, emit_motion, remember)
            except Exception as exc:  # noqa: BLE001 - one bad event must not stop capture
                if type(exc).__name__ == "ConnectionClosedError":
                    raise
                print(f"[input] event dropped ({type(exc).__name__}: {exc})")

    while True:
        # Plugged-in keyboards and mice show up on this timer, including while
        # keys are still arriving. XI_HierarchyChanged is the event meant for
        # that, but selecting its mask is a BadValue here and python-xlib then
        # closes the connection, so the device list is polled instead. Masks are
        # only rewritten when a keyboard or pointer actually appeared or left.
        now = time.monotonic()
        remaining = _DEVICE_SCAN_S - (now - last_scan)
        due = remaining <= 0
        ready = _x_socket_ready(display, 0 if due else remaining)
        if due or not ready:
            fresh = _linux_devices(display, root, xinput)
            last_scan = time.monotonic()
            if not fresh:
                remember(None)
                if not none_logged:
                    print("[input] no input devices; keyboard and mouse disabled")
                    none_logged = True
            else:
                none_logged = False
                ids = (set(fresh["keyboards"]), set(fresh["pointers"]))
                prev = None if selected is None else (set(selected["keyboards"]), set(selected["pointers"]))
                if ids != prev:
                    root.xinput_select_events(fresh["masks"])
                    display.flush()
                    if announced:
                        print(f"[input] devices changed ({len(fresh['keyboards'])} keyboard(s), "
                              f"{len(fresh['pointers'])} pointer(s))")
                    else:
                        print(f"[input] XInput2 ({len(fresh['keyboards'])} keyboard(s), "
                              f"{len(fresh['pointers'])} pointer(s), "
                              f"{len(fresh['absolute'])} absolute, {len(fresh['xtest'])} XTEST)")
                        announced = True
                remember(fresh)
            if not ready:
                # Nothing is queued, so the server's mask matches the events already
                # handled, and a warp that produced no raw event can be caught up.
                # Skipped while events are waiting: the cursor would then be ahead
                # of samples we have not read yet.
                if fresh and not display.pending_events():
                    px, py = fresh["seed"]
                    for source in fresh["xtest"]:
                        prev_xy = last.get(source)
                        if prev_xy is None or abs(prev_xy[0] - px) > 2 or abs(prev_xy[1] - py) > 2:
                            last[source] = (px, py)
                    live = root.query_pointer().mask
                    held = _shifts_held(display, shift_codes)
                    if not display.pending_events():
                        mods["shifts"] = held
                        mods["shift"] = bool(held)
                        mods["numlock"] = bool(live & num_mask)
                continue
        if not selected:
            # A readable socket with no device selected has nothing we can use.
            # Drain one event so a surprise doesn't turn into a tight loop.
            if ready or display.pending_events() or _x_socket_ready(display, 0):
                display.next_event()
            continue
        events = [display.next_event()]
        while len(events) < _EVENT_BATCH and (display.pending_events() or _x_socket_ready(display, 0)):
            events.append(display.next_event())
        pointer = None
        if _batch_has_xtest(events, xinput, selected["xtest"]):
            # The cursor distinguishes an XTEST coordinate from an XTEST delta.
            # A real mouse never takes this round trip.
            pos = root.query_pointer()
            while len(events) < _EVENT_BATCH and display.pending_events():
                events.append(display.next_event())
            pointer = (pos.root_x, pos.root_y)
        dispatch(events, pointer)


def _dispatch_one(display, root, xinput, X, event, index, xtest_at, selected, mods, shift_codes, numlock_code, last, emit_motion, remember):
    """One raw event. The input loop catches a failure here so capture keeps running."""
    evtype = getattr(event, "evtype", None)
    if evtype == xinput.HierarchyChanged:
        fresh = _linux_devices(display, root, xinput)
        if fresh:
            root.xinput_select_events(fresh["masks"])
            display.flush()
        remember(fresh)
        return
    if index in xtest_at:
        emit_motion(*xtest_at[index])
        return
    data = getattr(event, "data", b"")
    if evtype == xinput.RawMotion:
        parsed = _parse_xi_raw_motion(data)
        if not parsed or not selected:
            return
        source, x, y = parsed
        if source in selected["absolute"]:
            dx, dy = _position_delta(source, x, y, last)
        else:
            dx, dy = x, y
        emit_motion(dx, dy)
        return
    if evtype in (xinput.RawKeyPress, xinput.RawKeyRelease):
        _dispatch_key(display, xinput, X, data, evtype, mods, shift_codes, numlock_code)
    elif evtype in (xinput.RawButtonPress, xinput.RawButtonRelease):
        _dispatch_button(xinput, data, evtype)


def _dispatch_key(display, xinput, X, data, evtype, mods, shift_codes, numlock_code):
    """Key up or down. Shift and Num Lock are applied after this event's lookup."""
    detail = _xi_detail(data)
    if detail is None:
        return
    down = evtype == xinput.RawKeyPress
    if not (down and detail in _held_keycodes):   # auto-repeat keeps the original vk
        if not down and detail in _held_keycodes:
            vk = _held_keycodes.pop(detail)
        else:
            keysym = _keysym_for_keycode(display, detail, mods["numlock"], mods["shift"])
            vk = None if keysym in (None, 0, X.NoSymbol) else _linux_vk(keysym, None)
            if down and vk is not None:
                _held_keycodes[detail] = vk
        if vk is not None:
            if down:
                if vk not in _down_keys:
                    _down_keys.add(vk)
                    hub.emit({"k": [vk, 1]})
            else:
                _down_keys.discard(vk)
                hub.emit({"k": [vk, 0]})
        if detail in shift_codes:
            # Left and right are tracked on their own. Releasing one must not
            # clear Shift while the other is still held.
            if down:
                mods["shifts"].add(detail)
            else:
                mods["shifts"].discard(detail)
            mods["shift"] = bool(mods["shifts"])
        elif numlock_code and detail == numlock_code and down:
            mods["numlock"] = not mods["numlock"]


def _dispatch_button(xinput, data, evtype):
    detail = _xi_detail(data)
    if detail in _XI_SCROLL:
        if evtype == xinput.RawButtonPress:
            hub.emit({"s": list(_XI_SCROLL[detail])})
        return
    name = _XI_BUTTONS.get(detail)
    if not name:
        return
    down = evtype == xinput.RawButtonPress
    if down:
        _down_buttons.add(name)
    else:
        _down_buttons.discard(name)
    hub.emit({"m": [name, 1 if down else 0]})


def linux_input_thread():
    """Keyboard, buttons, scroll and raw pointer motion via XInput2.

    This is the Linux counterpart of the Windows low-level hooks and Raw Input.
    If the X server disappears the held keys and buttons are released and the
    thread connects again, backing off so a dead display is not polled in a loop.
    """
    try:
        from Xlib import X, display, error
        from Xlib.ext import xinput
    except Exception as e:  # noqa: BLE001 - python-xlib is the Linux extra
        print(f"[input] XInput unavailable; keyboard and mouse disabled ({e})")
        return
    delay = 0.5
    told_user = False
    while True:
        try:
            connection = display.Display()
        except (error.DisplayError, OSError) as e:
            # DisplayNameError (an empty or nonsense DISPLAY) is a DisplayError,
            # not a DisplayConnectionError, so it used to escape as a traceback.
            # Say it once; a dead display should not fill the log.
            if not told_user:
                print(f"[input] no X display ({e}); retrying in {delay:.1f}s")
                told_user = True
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
            continue
        told_user = False
        delay = 0.5
        try:
            _linux_input_session(connection, xinput, X)
        except error.ConnectionClosedError as e:
            _release_held_input()
            if getattr(connection, "_overlay_ready", False):
                delay = 0.5
            print(f"[input] X connection lost ({e}); released held input, retrying in {delay:.1f}s")
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - the socket is already dead
                pass
            time.sleep(delay)
            delay = min(delay * 2, 5.0)


def raw_mouse_thread():
    if sys.platform != "win32":
        return
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
        self.sdl = sdl = _load_sdl()
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


def _load_sdl():
    """The vendored SDL3.dll on Windows, or libSDL3 on Linux."""
    if sys.platform == "win32":
        dll = BASE / "lib" / "SDL3.dll"
        if not dll.exists():
            raise OSError(f"{dll} not found")
        return ctypes.CDLL(str(dll))
    names = [str(BASE / "lib" / "libSDL3.so"), "libSDL3.so.0", "libSDL3.so"]
    last = None
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError as e:
            last = e
    raise OSError(f"SDL3 library not found ({last})")


# evdev button code -> XInput bit. The kernel's gamepad spec names the diamond by
# compass point (south is A / Cross, north is Y / Triangle, west is X / Square).
# PlayStation and Switch pads send those compass codes. xpad and xpadneo do not:
# physical X is BTN_X (0x133, the same number as north) and physical Y is BTN_Y
# (0x134, west). A Microsoft Bluetooth pad on hid-generic or hid-microsoft does
# the same. Whether to swap is decided from the driver, below.
# BTN_TRIGGER..BTN_BASE6 (0x120-0x12b) are a plain joystick, in button order.
_JS_BTN = {
    0x120: 0x1000, 0x121: 0x2000, 0x122: 0x4000, 0x123: 0x8000,   # trigger thumb thumb2 top -> A B X Y
    0x124: 0x0100, 0x125: 0x0200,                                 # top2 pinkie -> LB RB
    0x126: 0x0020, 0x127: 0x0010,                                 # base base2 -> back start
    0x128: 0x0040, 0x129: 0x0080, 0x12A: 0x0400,                 # base3 base4 base5 -> LS RS guide
    0x130: 0x1000, 0x131: 0x2000, 0x133: 0x8000, 0x134: 0x4000,   # south east north west
    0x136: 0x0100, 0x137: 0x0200,                                 # LB RB
    0x13A: 0x0020, 0x13B: 0x0010, 0x13C: 0x0400,               # back start guide
    0x13D: 0x0040, 0x13E: 0x0080,                                 # LS RS
    0x220: 0x0001, 0x221: 0x0002, 0x222: 0x0004, 0x223: 0x0008, # d-pad up down left right
}
# BTN_BASE6 has no XInput face bit left. It is SDL's misc button (the `x` field).
_JS_EXTRA = {0x12B: 0x0001}
_JS_TRIG = {0x138: "lt", 0x139: "rt"}                            # digital triggers
# ABS_X Y Z RX RY RZ, hat X, hat Y. Used only if the device has no axis map.
_JS_FALLBACK_AXES = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11)
_JS_FALLBACK_BTNS = (0x130, 0x131, 0x133, 0x134, 0x136, 0x137, 0x138, 0x139, 0x13A, 0x13B, 0x13C, 0x13D, 0x13E)
# JSIOCGAXMAP is __u8[64], JSIOCGBTNMAP is __u16[512], JSIOCGNAME(128) is the
# identifier string. Numbers from linux/joystick.h (_IOC_READ, 'j', ...).
_JSIOCGAXMAP = 0x80406A32
_JSIOCGBTNMAP = 0x84006A34
_JSIOCGNAME = 0x80806A13


def _js_trigger(value):
    """joydev trigger axis (-32767..32767 or 0..32767) to the overlay's 0..255."""
    if value < 0:
        value += 32768
    return max(0, min(255, int(value) * 255 // 32767))


# joydev and evdev sit on top of the real driver. The hardware driver is further
# up the sysfs `device` links: xpad for the wired driver, xpadneo for Bluetooth.
_JS_SKIP_DRIVERS = {"joydev", "evdev"}
# These two report Xbox X as BTN_X and Xbox Y as BTN_Y. xpadneo's usage map
# sends 0x90003 to BTN_X and 0x90004 to BTN_Y, and SDL maps both as x:b2 y:b3.
_JS_SWAP_DRIVERS = {"xpad", "xpadneo"}
# hid-microsoft registers its sysfs driver as "microsoft". The module name is
# accepted as well. hid-generic is the fallback when neither driver claims the pad.
_JS_MS_DRIVERS = {"hid-generic", "hid-microsoft", "microsoft"}
_JS_MS_VENDOR = 0x045E


def _js_swaps_xy(driver, vendor=None):
    """True when physical X is BTN_X and physical Y is BTN_Y.

    xpad and xpadneo both do that, whatever the product string is. A Microsoft
    Bluetooth pad (USB vendor 045e) on hid-generic or hid-microsoft does too.
    PlayStation, Switch and generic joysticks follow the compass names, so
    BTN_X stays north (Y) and BTN_Y stays west (X).
    """
    name = driver or ""
    if name in _JS_SWAP_DRIVERS:
        return True
    if name in _JS_MS_DRIVERS and vendor == _JS_MS_VENDOR:
        return True
    return False


def _js_read_vendor(node):
    """Vendor id on this sysfs node, or None.

    Input devices publish it as `id/vendor` (four hex digits). USB devices
    publish `idVendor` in the same form.
    """
    for rel in ("id/vendor", "idVendor"):
        path = node / rel
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="ascii", errors="replace")[:32].strip()
        except OSError:
            continue
        if text.lower().startswith("0x"):
            text = text[2:]
        try:
            return int(text, 16)
        except ValueError:
            continue
    return None


def _js_identity(js_path, sysfs_root=None):
    """(driver, vendor) behind a /dev/input/js* node.

    `sysfs_root` defaults to /sys/class/input. Tests pass a stand-in tree.
    vendor is an int, or None when the tree has no id file. The driver is ''
    when no hardware driver is linked.
    """
    root = Path(sysfs_root) if sysfs_root is not None else Path("/sys/class/input")
    current = root / Path(js_path).name
    seen = set()
    vendor = None
    for _ in range(8):
        try:
            resolved = current.resolve()
        except OSError:
            break
        if resolved in seen:
            break
        seen.add(resolved)
        if vendor is None:
            vendor = _js_read_vendor(current)
        link = current / "driver"
        if link.is_symlink():
            try:
                name = Path(os.readlink(link)).name
            except OSError:
                name = ""
            if name and name not in _JS_SKIP_DRIVERS:
                return name, vendor
        parent = current / "device"
        if not parent.exists():
            break
        current = parent
    return "", vendor


def _js_driver(js_path, sysfs_root=None):
    """Kernel driver behind a /dev/input/js* node, or '' if it cannot be seen.

    `sysfs_root` defaults to /sys/class/input. Tests pass a stand-in tree.
    """
    return _js_identity(js_path, sysfs_root)[0]


def _js_device_name(fd):
    """Identifier from JSIOCGNAME, or '' when the ioctl is missing."""
    import fcntl
    buf = bytearray(128)
    try:
        fcntl.ioctl(fd, _JSIOCGNAME, buf)
    except OSError:
        return ""
    return buf.split(b"\x00", 1)[0].decode("utf-8", "replace")


def _js_apply_button(state, code, down, swap_xy=False):
    """Apply one evdev button code to the overlay state. Unknown codes are ignored."""
    if not code:
        return
    if swap_xy and code == 0x133:
        code = 0x134
    elif swap_xy and code == 0x134:
        code = 0x133
    trigger = _JS_TRIG.get(code)
    if trigger:
        state[trigger] = 255 if down else 0
        return
    extra = _JS_EXTRA.get(code)
    if extra:
        field = state.get("x", 0)
        state["x"] = (field | extra) if down else (field & ~extra)
        return
    mask = _JS_BTN.get(code)
    if not mask:
        return
    if down:
        state["b"] |= mask
    else:
        state["b"] &= ~mask


def _js_apply_event(state, axes, buttons, swap_xy, data):
    """One joydev event (`<IhBB` time, value, kind, number) applied to `state`."""
    if len(data) < 8:
        raise OSError("short joystick event")
    _time, value, kind, number = struct.unpack("<IhBB", data)
    kind &= ~0x80                                              # drop the init flag
    if kind == 1 and number < len(buttons):
        _js_apply_button(state, buttons[number], bool(value), swap_xy)
    elif kind == 2 and number < len(axes):
        _js_apply_axis(state, axes[number], value)


def _js_apply_axis(state, code, value):
    """Apply one ABS_* code. Stick Y is negated so up is positive, matching XInput."""
    if code == 0x00:
        state["lx"] = value
    elif code == 0x01:
        state["ly"] = -value - (value == -32768)
    elif code == 0x02:
        state["lt"] = _js_trigger(value)
    elif code == 0x03:
        state["rx"] = value
    elif code == 0x04:
        state["ry"] = -value - (value == -32768)
    elif code == 0x05:
        state["rt"] = _js_trigger(value)
    elif code in (0x10, 0x11):
        bit_lo, bit_hi = (0x4, 0x8) if code == 0x10 else (0x1, 0x2)   # left/right or up/down
        state["b"] &= ~(bit_lo | bit_hi)
        if value < 0:
            state["b"] |= bit_lo
        elif value > 0:
            state["b"] |= bit_hi


def _js_device_maps(fd):
    """(axis ABS codes, button evdev codes) from JSIOCGAXMAP / JSIOCGBTNMAP.

    The index in each list is the number joydev puts in an event. Falls back to
    the kernel's usual order if the ioctls are missing, and says so.
    """
    import fcntl
    try:
        axes = bytearray(64)
        buttons = bytearray(1024)
        fcntl.ioctl(fd, _JSIOCGAXMAP, axes)
        fcntl.ioctl(fd, _JSIOCGBTNMAP, buttons)
    except OSError as e:
        print(f"[pad] joystick map unavailable ({e}); using the standard button order")
        return list(_JS_FALLBACK_AXES), list(_JS_FALLBACK_BTNS)
    return list(axes), list(struct.unpack("<512H", buttons))


class LinuxJsBackend:
    """Gamepads that show up as /dev/input/js* (the kernel joydev interface)."""

    name = "Linux joystick"

    def __init__(self, index):
        if sys.platform == "win32":
            raise OSError("Linux joystick is not used on Windows")
        root = Path("/dev/input")
        paths = sorted(p for p in root.glob("js*") if p.name[2:].isdigit()) if root.is_dir() else []
        if not paths:
            raise OSError("no /dev/input/js* device")
        slot = 0 if index is None else index
        if slot >= len(paths):
            raise OSError(f"joystick index {slot} out of range ({len(paths)} device(s))")
        try:
            self.fd = os.open(paths[slot], os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            raise OSError(f"cannot read {paths[slot]}: {e}") from e
        self.path = paths[slot]
        self.axes, self.buttons = _js_device_maps(self.fd)
        self.pad_name = _js_device_name(self.fd) or paths[slot].name
        self.driver, vendor = _js_identity(paths[slot])
        self.swap_xy = _js_swaps_xy(self.driver, vendor)
        self.state = {"c": 1, "b": 0, "x": 0, "r": 0, "t": [], "ty": 0, "nm": self.pad_name,
                      "lt": 0, "rt": 0, "lx": 0, "ly": 0, "rx": 0, "ry": 0}
        label = f"{self.pad_name} [{self.driver}]" if self.driver else self.pad_name
        print(f"[pad] {self.name}: {label}")

    def poll(self):
        while True:
            try:
                data = os.read(self.fd, 8)
            except BlockingIOError:
                break
            if len(data) < 8:
                raise OSError(f"{self.path} closed")
            _js_apply_event(self.state, self.axes, self.buttons, self.swap_xy, data)
        return self.state


class XInputBackend:
    name = "XInput"

    def __init__(self, slot):
        if sys.platform != "win32":
            raise OSError("XInput is Windows-only")
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
    classes = [SdlBackend, GcAdapterBackend, Switch2ProBackend]
    classes.append(XInputBackend if sys.platform == "win32" else LinuxJsBackend)
    for cls in classes:
        try:
            backends.append(cls(index))
        except OSError as e:
            print(f"[pad] {cls.name} unavailable: {e}")
    if not backends:
        print("[pad] no controller backend available; controller disabled")
        return
    last = None
    while True:
        state = None
        for b in backends:  # first backend that sees a pad wins
            try:
                state = b.poll()
            except Exception as e:  # noqa: BLE001 - one misbehaving backend must not stop the controller loop
                print(f"[pad] {b.name} error: {e!r}")
                state = None
                time.sleep(1)
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
    # "same-site" (another program on localhost:<other port>) is refused too: only this page itself may call the API.
    if request.path.startswith("/api/") and request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
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


def open_folder(folder):
    """Open a directory in the file manager (Explorer on Windows, xdg-open elsewhere)."""
    if hasattr(os, "startfile"):
        os.startfile(str(folder))
    else:
        subprocess.Popen(["xdg-open", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def api_open_themes(request):
    folder = request.app["themes"].ensure_user_dir()
    try:
        open_folder(folder)
    except OSError as e:
        raise web.HTTPInternalServerError(text=str(e))
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
            except Exception:  # noqa: BLE001 - a broken client must never stop the stream for everyone else
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

    if sys.platform == "win32":
        keyboard.Listener(on_press=on_press, on_release=on_release).start()
        mouse.Listener(on_click=on_click, on_scroll=on_scroll).start()
        threading.Thread(target=raw_mouse_thread, daemon=True).start()
    else:
        threading.Thread(target=linux_input_thread, daemon=True).start()
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
    if hasattr(signal, "SIGBREAK"):                 # Windows console break; Linux has no equivalent
        signal.signal(signal.SIGBREAK, lambda *_: os._exit(0))
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
