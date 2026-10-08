"""Theme discovery and safe file lookup.

A theme is a folder containing theme.json plus a stylesheet and any assets (fonts, images, controller artwork):

    overlay/themes/<id>/          built in, ships with the app
    <app dir>/themes/<id>/        added by the user; same id as a built-in = the user's copy wins
                                  (Windows: %APPDATA%/InputOverlay, Linux: ~/.config/InputOverlay
                                  unless ~/InputOverlay already exists)

theme.json:  {"name": "...", "author": "...", "description": "...", "version": "1.0", "css": "theme.css"}
See docs/THEMES.md for what a theme can restyle.
"""
import json
import re
from pathlib import Path

import config

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,39}$")
# Only passive file types are ever served from a theme folder: no scripts, no HTML.
ALLOWED = {".css", ".json", ".svg", ".png", ".gif", ".jpg", ".jpeg", ".webp", ".ttf", ".otf", ".woff", ".woff2"}
USER_DIR = config.APP_DIR / "themes"
MAX_ASSETS = 300

USER_README = """Input Overlay themes
====================

Each folder in here is one theme. To add a theme, drop its folder in here (so you end up with
  ...\\InputOverlay\\themes\\<theme name>\\theme.json
) and press Refresh in Settings > Appearance > Theme. Delete the folder to remove it.

A theme can restyle the keys, mouse and controller, bring its own font, and even replace the controller artwork.
It is only ever CSS, fonts and images: themes cannot run code, and the overlay blocks them from loading anything from the internet.
Even so, only install themes from people you trust.

To make your own, copy one of the built-in themes (see docs/THEMES.md in the project) and edit theme.css.
"""


def _clean_manifest(raw, folder_id):
    raw = raw if isinstance(raw, dict) else {}

    def text(key, limit, default=""):
        v = raw.get(key)
        return v.strip()[:limit] if isinstance(v, str) else default

    return {
        "name": text("name", 60, folder_id) or folder_id,
        "author": text("author", 60),
        "description": text("description", 240),
        "version": text("version", 20),
        "css": text("css", 80, "theme.css"),
    }


class Themes:
    def __init__(self, builtin_dir):
        self.builtin = Path(builtin_dir)
        self.user = USER_DIR

    def ensure_user_dir(self):
        self.user.mkdir(parents=True, exist_ok=True)
        readme = self.user / "README.txt"
        if not readme.exists():
            readme.write_text(USER_README, encoding="utf-8")
        return self.user

    def _roots(self):
        return [(self.user, False), (self.builtin, True)]     # user first, so a user copy overrides a built-in

    def resolve(self, theme_id, rel):
        """Absolute path of a theme file, or None. Refuses anything outside the theme folder or of a non-passive type."""
        if not ID_RE.match(theme_id or ""):
            return None
        rel = Path(rel)
        if rel.is_absolute() or ".." in rel.parts or rel.suffix.lower() not in ALLOWED:
            return None
        for root, _ in self._roots():
            base = (root / theme_id).resolve()
            p = (base / rel).resolve()
            if base in p.parents and p.is_file():
                return p
        return None

    def list(self):
        found = {}
        for root, builtin in self._roots()[::-1]:             # built-ins first, user themes overwrite same ids
            if not root.is_dir():
                continue
            for d in sorted(root.iterdir()):
                if not (d.is_dir() and ID_RE.match(d.name) and (d / "theme.json").is_file()):
                    continue
                try:
                    manifest = _clean_manifest(json.loads((d / "theme.json").read_text(encoding="utf-8")), d.name)
                except (OSError, ValueError):
                    continue
                assets = []
                for f in sorted(d.rglob("*")):
                    rel = f.relative_to(d)
                    if f.is_file() and f.suffix.lower() in ALLOWED and len(rel.parts) <= 3 and not any(p.startswith(".") for p in rel.parts):
                        assets.append(rel.as_posix())
                    if len(assets) >= MAX_ASSETS:
                        break
                manifest["css"] = manifest["css"] if manifest["css"] in assets else ""
                found[d.name] = {"id": d.name, "builtin": builtin, **manifest, "assets": assets}
        items = sorted(found.values(), key=lambda t: (t["id"] != "default", t["name"].lower()))
        return items
