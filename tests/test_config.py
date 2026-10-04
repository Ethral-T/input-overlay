import json

import config


def test_clean_non_dict_gives_defaults():
    for junk in (None, 5, "x", [], [1, 2]):
        p = config.clean(junk)
        assert p["keyboard"] == {"mode": "full", "keys": [], "sizes": {}}
        assert p["controller"] == "auto" and p["pad"] == "auto" and p["align"] == "mc"
        assert p["accent"] == "#38bdf8" and p["theme"] == "default"


def test_keys_out_of_range_or_not_int_are_dropped():
    p = config.clean({"keyboard": {"mode": "custom", "keys": [65, 65, -1, 256, "66", 1.5, 9]}})
    assert p["keyboard"]["keys"] == [9, 65]
    assert config.clean({"keyboard": {"mode": "bogus"}})["keyboard"]["mode"] == "full"


def test_sizes_clamped_and_snapped_to_quarters():
    sizes = config.clean({"keyboard": {"sizes": {"32": 2.6, "65": 100, "66": 0.1, "x": 1, "300": 1, "67": True, "68": "2"}}})["keyboard"]["sizes"]
    assert sizes == {"32": 2.5, "65": 12.0, "66": 0.5}


def test_accent_controller_and_enums():
    assert config.clean({"accent": "red"})["accent"] == "#38bdf8"
    assert config.clean({"accent": "#12abEF"})["accent"] == "#12abEF"
    assert config.clean({"controller": "playstation"})["controller"] == "ps5"
    assert config.clean({"controller": "ps4"})["controller"] == "ps4"
    assert config.clean({"controller": "nope"})["controller"] == "auto"
    assert config.clean({"pad": "maybe"})["pad"] == "auto"
    assert config.clean({"align": "zz"})["align"] == "mc"
    assert config.clean({"gyro": "x"})["gyro"] == "off"
    assert config.clean({"theme": "../evil"})["theme"] == "default"


def test_numeric_clamps_and_bools_rejected():
    p = config.clean({"scale": 99, "sens": -3, "opacity": 0, "fill": 5, "tprot": 1000, "gsens": 0})
    assert (p["scale"], p["sens"], p["opacity"], p["fill"], p["tprot"], p["gsens"]) == (4.0, 0.1, 0.1, 1.0, 45.0, 0.1)
    p = config.clean({"scale": True, "fill": False, "opacity": "1"})
    assert (p["scale"], p["fill"], p["opacity"]) == (1.0, 0.8, 1.0)


def test_clean_settings_defaults():
    s = config.clean_settings(None)
    assert s["swapSide"] is False and s["opacity"] == 1.0 and s["fill"] == 0.8
    assert s["theme"] == "default" and s["gyro"] == "off"
    assert s["scope"] == {"swapSide": True, "opacity": False, "fill": False, "theme": True, "accent": False, "gyro": False}
    assert config.clean_settings({"shareLook": True})["scope"]["opacity"] is True
    assert config.clean_settings({"opacity": True})["opacity"] == 1.0


def test_round_trip(tmp_path):
    path = tmp_path / "config.json"
    c = config.Config(path)
    assert list(c.presets) == [s["name"] for s in config.SEED]      # starter presets seeded on first run
    c.put("Mine", {"mouse": False, "scale": 2})
    c.set_active("Mine")
    again = config.Config(path)
    assert again.active == "Mine" and again.presets["Mine"]["scale"] == 2.0 and again.presets["Mine"]["mouse"] is False


def test_unreadable_json_is_kept_as_bad(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    c = config.Config(path)
    assert (tmp_path / "config.bad").read_text(encoding="utf-8") == "{ not json"
    assert list(c.presets) == [s["name"] for s in config.SEED]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == config.VERSION


def test_find(tmp_path):
    c = config.Config(tmp_path / "config.json")
    assert c.find("Full") == "Full"
    assert c.find("  wasd + MOUSE ") == "WASD + Mouse"
    assert c.find("2") == "WASD + Mouse"
    assert c.find("0") is None and c.find("99") is None and c.find("nothing") is None
