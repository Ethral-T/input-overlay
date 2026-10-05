import json

import config


def test_clean_non_dict_gives_defaults():
    for junk in (None, 5, "x", [], [1, 2]):
        p = config.clean(junk)
        assert p["keyboard"] == {"mode": "full", "keys": [], "sizes": {}, "numpad": False}
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


def test_numpad_is_a_bool_default_off():
    assert config.clean({})["keyboard"]["numpad"] is False
    assert config.clean({"keyboard": {"numpad": 1}})["keyboard"]["numpad"] is True
    assert config.clean({"keyboard": {"numpad": True}})["keyboard"]["numpad"] is True


def test_pads_clamped_to_1_to_4():
    assert config.clean({})["pads"] == 1
    assert config.clean({"pads": 0})["pads"] == 1
    assert config.clean({"pads": 9})["pads"] == 4
    assert config.clean({"pads": 3})["pads"] == 3


def test_pads_auto_is_kept():
    assert config.clean({"pads": "auto"})["pads"] == "auto"
    assert config.clean({"pads": "AUTO"})["pads"] == 1          # only the exact word counts


def test_pads_not_a_number_gives_default():
    assert config.clean({"pads": "x"})["pads"] == 1
    assert config.clean({"pads": True})["pads"] == 1     # booleans are not numbers here


def test_pads_float_is_truncated():
    assert config.clean({"pads": 2.7})["pads"] == 2      # int() after clamping, so it rounds down


def test_player_colors_bool_default_on():
    assert config.clean({})["playerColors"] is True
    assert config.clean({"playerColors": False})["playerColors"] is False
    assert config.clean({"playerColors": 0})["playerColors"] is False


def test_layout_is_none_unless_a_dict():
    for junk in (None, 5, "x", [], [1]):
        assert config.clean({"layout": junk})["layout"] is None
    assert config.clean({})["layout"] is None
    assert config.clean({"layout": {}})["layout"] == {}   # an empty dict is a (blank) free layout, not None


def test_layout_keeps_only_known_pieces():
    pos = {"x": 1, "y": 2}
    wanted = ["kb", "mouse", "pad0", "pad3", "gyro0", "gyro3"]
    unknown = ["pad4", "gyro4", "keyboard", "Mouse", "pad", "xpad0"]
    layout = config.clean({"layout": {k: pos for k in wanted + unknown}})["layout"]
    assert sorted(layout) == sorted(wanted)


def test_layout_positions_are_rounded_floats():
    layout = config.clean({"layout": {"kb": {"x": 10, "y": -5.26}}})["layout"]
    assert layout == {"kb": {"x": 10.0, "y": -5.3}}      # one decimal place; negatives are allowed


def test_layout_bad_positions_are_dropped():
    bad = {
        "kb": {"x": 1},                       # y missing
        "mouse": {"x": "1", "y": 2},          # not a number
        "pad0": {"x": True, "y": 2},          # a bool is not a number
        "pad1": {"x": 20000, "y": 0},         # out of range (must be under 20000 in size)
        "pad2": [1, 2],                       # not a dict
        "gyro0": {"x": 19999, "y": -19999},   # the biggest accepted values
    }
    assert config.clean({"layout": bad})["layout"] == {"gyro0": {"x": 19999.0, "y": -19999.0}}


def test_update_check_is_opt_in_bool():
    assert config.clean_settings({})["updateCheck"] is False
    assert config.clean_settings(None)["updateCheck"] is False
    assert config.clean_settings({"updateCheck": True})["updateCheck"] is True
    assert config.clean_settings({"updateCheck": "yes"})["updateCheck"] is True   # bool(), so any truthy value turns it on


def test_rename_keeps_position_and_active(tmp_path):
    c = config.Config(tmp_path / "config.json")
    second = list(c.presets)[1]
    c.set_active(second)
    c.put("Renamed", {}, rename_from=second)
    assert list(c.presets)[1] == "Renamed" and c.active == "Renamed" and second not in c.presets


def test_rename_to_an_existing_name_is_refused(tmp_path):
    c = config.Config(tmp_path / "config.json")
    first, second = list(c.presets)[:2]
    try:
        c.put(first, {}, rename_from=second)
    except ValueError:
        pass
    else:
        raise AssertionError("renaming onto an existing preset should fail")
    assert second in c.presets


def test_cycle_wraps_around(tmp_path):
    c = config.Config(tmp_path / "config.json")
    names = list(c.presets)
    c.set_active(names[-1])
    assert c.cycle(1) == names[0]
    assert c.cycle(-1) == names[-1]
