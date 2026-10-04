"""Test setup: put the repo root on sys.path and, where the real pynput can't be imported (Linux CI, no display), stub it out.

server.py imports pynput at the top; the tests never use the real hooks, so an empty stand-in is enough.
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pynput.keyboard, pynput.mouse  # noqa: F401
except Exception:
    pynput = types.ModuleType("pynput")
    pynput.__path__ = []
    for sub in ("keyboard", "mouse"):
        mod = types.ModuleType("pynput." + sub)
        setattr(pynput, sub, mod)
        sys.modules["pynput." + sub] = mod
    sys.modules["pynput"] = pynput
