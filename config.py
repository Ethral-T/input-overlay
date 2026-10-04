"""Preset storage. Presets live in %APPDATA%\\InputOverlay\\config.json.

A preset is one overlay "variant":
  {"keyboard": {"mode": "full" | "custom" | "off", "keys": [<windows vk codes>], "sizes": {"<vk>": <width in key units>}, "numpad": bool},
   "mouse": bool, "pad": "auto" | "on" | "off",
   "theme": "<theme id>" ("default" = Classic; see themes.py),
   "controller": "auto" | "steam" | "xbox" | "ps4" | "ps5" | "switch" | "switch2" | "gamecube" (artwork; auto picks from the connected pad),
   "swapSide": bool (Mouse Button Invert; used when that setting is "this preset" scoped),
   "align": "tl".."br" (where the overlay sits inside the Browser Source: top/middle/bottom + left/centre/right; default "mc"),
   "accent": "#rrggbb" (used when Highlight colour is "this preset" scoped), "opacity": 0.1..1 (whole overlay), "fill": 0..1 (key/mouse/controller background), "scale": float, "sens": float, "tpt": float, "tprot": float,
   "gyro": "off" | "tilt" | "aim" | "both" (how a controller's gyroscope is shown: the picture tilts and/or an aim dot with a trail; used when
           Gyro is "this preset" scoped, otherwise the shared value in settings applies to every preset),
   "gsens": float (gyro sensitivity),
   "layout": null | {"kb": {"x", "y"}, "mouse": ..., "pad0": ..., "gyro0": ..., ...} (Free layout: where each piece sits; null = in a row),
   "pads": 1..4 (how many controllers to show at once; they are the connected ones in order), "playerColors": bool (a different highlight colour for players 2-4)}
"""
import json
import os
import re
import threading
from pathlib import Path

APP_DIR = Path(os.environ.get("INPUT_OVERLAY_HOME") or Path(os.environ.get("APPDATA") or Path.home()) / "InputOverlay")
CONFIG_PATH = APP_DIR / "config.json"
VERSION = 2                       # config.json format; see Config._load for migrations

CONTROLLER_STYLES = ("auto", "steam", "xbox", "ps4", "ps5", "switch", "switch2", "gamecube")

DEFAULT_ACCENT = "#38bdf8"
ACCENT_RE = re.compile(r"#[0-9a-fA-F]{6}")

# vk codes for the starter "WASD" preset: 1-5, Tab QWER, ASDF, Shift ZXCV, Ctrl Alt Space
# (keep in sync with KB.GROUPS['WASD + common'] in overlay/keyboard.js: same keys, different language, so they can't share one list)
_WASD = [49, 50, 51, 52, 53, 9, 81, 87, 69, 82, 65, 83, 68, 70, 160, 90, 88, 67, 86, 162, 164, 32]

SEED = [
    {"name": "Full", "keyboard": {"mode": "full", "keys": []}, "mouse": True, "pad": "auto"},
    # the spacebar is shrunk to 2.5 units so it ends under the V key instead of trailing off to the right
    {"name": "WASD + Mouse", "keyboard": {"mode": "custom", "keys": _WASD, "sizes": {"32": 2.5}}, "mouse": True, "pad": "auto"},
    {"name": "Keyboard only", "keyboard": {"mode": "full", "keys": []}, "mouse": False, "pad": "off"},
    {"name": "Controller only", "keyboard": {"mode": "off", "keys": []}, "mouse": False, "pad": "on"},
]


LAYOUT_KEYS = re.compile(r"^(kb|mouse|pad[0-3]|gyro[0-3])$")


def clean_layout(v):
    """Free layout: None (the pieces sit in a row) or {piece: {"x": px, "y": px}} for kb, mouse, pad0-3 and gyro0-3."""
    if not isinstance(v, dict):
        return None
    out = {}
    for k, pos in v.items():
        if LAYOUT_KEYS.match(str(k)) and isinstance(pos, dict):
            xy = [pos.get("x"), pos.get("y")]
            if all(isinstance(n, (int, float)) and not isinstance(n, bool) and abs(n) < 20000 for n in xy):
                out[k] = {"x": round(float(xy[0]), 1), "y": round(float(xy[1]), 1)}
    return out


def _num(src, key, default, lo, hi):
    """src[key] as a float clamped to lo..hi; the default if it is missing or not a number (booleans don't count)."""
    v = src.get(key, default)
    return min(hi, max(lo, float(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _accent(v):
    """v if it is a "#rrggbb" colour string, otherwise the default accent."""
    return v if isinstance(v, str) and ACCENT_RE.fullmatch(v) else DEFAULT_ACCENT


def clean(p):
    """Coerce arbitrary JSON into a valid preset (never trust the file or the API caller)."""
    p = p if isinstance(p, dict) else {}
    kb = p.get("keyboard") if isinstance(p.get("keyboard"), dict) else {}
    mode = kb.get("mode") if kb.get("mode") in ("full", "custom", "off") else "full"
    raw_keys = kb.get("keys")
    keys = sorted({int(k) for k in raw_keys if isinstance(k, int) and 0 <= k <= 255}) if isinstance(raw_keys, list) else []
    # per-key width overrides, {vk: units}: clamped to what the overlay lets you drag to, snapped to quarter units
    sizes = {}
    for k, v in (kb.get("sizes") if isinstance(kb.get("sizes"), dict) else {}).items():
        try:
            vk = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= vk <= 255 and isinstance(v, (int, float)) and not isinstance(v, bool) and len(sizes) < 120:
            sizes[str(vk)] = round(min(12.0, max(0.5, float(v))) * 4) / 4

    # "playstation" is the old name for "ps5". Only a string can be a style: a list or dict must never reach the dict lookup (unhashable).
    controller = p.get("controller")
    if isinstance(controller, str):
        controller = {"playstation": "ps5"}.get(controller, controller)
    if not (isinstance(controller, str) and controller in CONTROLLER_STYLES):
        controller = "auto"
    # (the "pad" / "align" / "gyro" checks below are tuple "in" tests, which compare with == and never raise for wrong-typed values)
    return {
        "keyboard": {"mode": mode, "keys": keys, "sizes": sizes, "numpad": bool(kb.get("numpad", False))},
        "mouse": bool(p.get("mouse", True)),
        "swapSide": bool(p.get("swapSide", False)),
        "controller": controller,
        "theme": clean_theme(p.get("theme")),
        "pad": p.get("pad") if p.get("pad") in ("auto", "on", "off") else "auto",
        "align": p.get("align") if p.get("align") in ('tl', 'tc', 'tr', 'ml', 'mc', 'mr', 'bl', 'bc', 'br') else "mc",
        "accent": _accent(p.get("accent")),
        "scale": _num(p, "scale", 1.0, 0.25, 4.0),
        "sens": _num(p, "sens", 1.0, 0.1, 5.0),
        "tpt": _num(p, "tpt", 0.5, 0.05, 1.0),
        "opacity": _num(p, "opacity", 1.0, 0.1, 1.0),
        "fill": _num(p, "fill", 0.8, 0.0, 1.0),
        "tprot": _num(p, "tprot", 9.0, -45.0, 45.0),
        "gyro": p.get("gyro") if p.get("gyro") in ("off", "tilt", "aim", "both") else "off",
        "gsens": _num(p, "gsens", 1.0, 0.1, 5.0),
        "pads": int(_num(p, "pads", 1, 1, 4)),
        "layout": clean_layout(p.get("layout")),
        "playerColors": bool(p.get("playerColors", True)),
    }


THEME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,39}$")


def clean_theme(v):
    return v if isinstance(v, str) and THEME_ID_RE.match(v) else "default"


def clean_settings(s):
    """Values shared by every preset, plus which settings are currently shared.

    scope[k] True  -> setting k uses the shared value below for ALL presets
    scope[k] False -> setting k uses each preset's own value
    """
    s = s if isinstance(s, dict) else {}

    scope_in = s.get("scope") if isinstance(s.get("scope"), dict) else {}
    legacy_share = bool(s.get("shareLook", False))          # older versions had one checkbox for both opacity sliders
    scope = {
        "swapSide": bool(scope_in.get("swapSide", True)),   # Mouse Button Invert was always global
        "opacity": bool(scope_in.get("opacity", legacy_share)),
        "fill": bool(scope_in.get("fill", legacy_share)),
        "theme": bool(scope_in.get("theme", True)),         # one theme for the whole stream unless you say otherwise
        "accent": bool(scope_in.get("accent", False)),      # the highlight colour is per preset until you say otherwise
        "gyro": bool(scope_in.get("gyro", False)),          # the Stream Deck gyro URLs switch this to "all presets"
    }
    return {
        "swapSide": bool(s.get("swapSide", False)),
        "opacity": _num(s, "opacity", 1.0, 0.1, 1.0),
        "fill": _num(s, "fill", 0.8, 0.0, 1.0),
        "theme": clean_theme(s.get("theme")),
        "accent": _accent(s.get("accent")),
        "gyro": s.get("gyro") if s.get("gyro") in ("off", "tilt", "aim", "both") else "off",
        "updateCheck": bool(s.get("updateCheck", False)),       # opt-in: look on GitHub for a newer version about once a day (a notice only)
        "scope": scope,
    }


def clean_name(name):
    name = re.sub(r"\s+", " ", str(name)).strip()[:40]
    return name or None


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.presets = {}          # name -> preset (insertion ordered)
        self.active = None         # name of the preset the plain base URL shows
        self.settings = clean_settings({})
        self._load()

    def _load(self):
        wanted, version = None, VERSION
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            version = data.get("version", 1)
            if not isinstance(version, int):     # a wrong-typed version would break the comparisons below
                version = 1
            wanted = data.get("active")
            self.settings = clean_settings(data.get("settings"))
            for item in data.get("presets", []):
                try:                     # one bad preset is skipped; the rest of the file is still used
                    name = clean_name(item.get("name", ""))
                    if name:
                        self.presets[name] = clean(item)
                except (TypeError, ValueError, AttributeError) as e:
                    print(f"[config] skipped preset {str(item)[:60]!r}: {e!r}")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError):
            try:                         # unreadable: keep a copy instead of silently overwriting the user's presets with the starter ones
                os.replace(self.path, self.path.with_suffix(".bad"))
            except OSError:
                pass
        seeded = not self.presets
        if seeded:
            for item in SEED:
                self.presets[item["name"]] = clean(item)
        self.active = wanted if isinstance(wanted, str) and wanted in self.presets else next(iter(self.presets))
        migrated = version < 2 and self._centre_old_default()
        if seeded or migrated or version != VERSION:
            self._save()

    def _centre_old_default(self):
        """v1 -> v2: the overlay used to default to the top-left of the Browser Source; centre is the default now.
        Presets still on top-left are moved to the centre once. After this, whatever you pick is kept."""
        moved = False
        for p in self.presets.values():
            if p["align"] == "tl":
                p["align"] = "mc"
                moved = True
        return moved

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": VERSION, "active": self.active, "settings": self.settings, "presets": self.as_list()}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def as_list(self):
        with self.lock:
            return [{"name": n, **p} for n, p in self.presets.items()]

    def put(self, name, preset, rename_from=None):
        name = clean_name(name)
        if not name:
            raise ValueError("name required")
        with self.lock:
            if rename_from and rename_from != name and rename_from in self.presets:
                if name in self.presets:
                    raise ValueError("a preset with that name already exists")
                # keep position when renaming
                self.presets = {(name if n == rename_from else n): p for n, p in self.presets.items()}
                if self.active == rename_from:
                    self.active = name
            self.presets[name] = clean(preset)
            self._save()
            return {"name": name, **self.presets[name]}

    def delete(self, name):
        with self.lock:
            if name not in self.presets:
                raise KeyError(name)
            if len(self.presets) == 1:
                raise ValueError("can't delete the last preset")
            del self.presets[name]
            if self.active == name:
                self.active = next(iter(self.presets))
            self._save()

    def put_settings(self, settings):
        with self.lock:
            self.settings = clean_settings(settings)
            self._save()
            return self.settings

    def find(self, ref):
        """Resolve a preset from a name (exact, then case-insensitive) or a 1-based position ("2")."""
        ref = str(ref).strip()
        if ref in self.presets:
            return ref
        for n in self.presets:
            if n.casefold() == ref.casefold():
                return n
        if ref.isdigit() and 1 <= int(ref) <= len(self.presets):
            return list(self.presets)[int(ref) - 1]
        return None

    def set_active(self, ref):
        with self.lock:
            name = self.find(ref)
            if name is None:
                raise KeyError(ref)
            self.active = name
            self._save()
            return name

    def cycle(self, step):
        with self.lock:
            names = list(self.presets)
            return self.set_active(names[(names.index(self.active) + step) % len(names)])


_shared = None
_shared_lock = threading.Lock()


def shared():
    """The one Config used by the server, the tray menu and the API, so they always agree."""
    global _shared
    if _shared is None:                  # fast path: already created
        with _shared_lock:               # the tray and the server thread can get here at the same moment
            if _shared is None:          # check again inside the lock so only one of them creates it
                _shared = Config()
    return _shared
