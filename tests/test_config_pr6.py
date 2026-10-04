import pytest

import config

# These rules come from the numpad / multi-pad / free-layout change; skip until it is in.
pytestmark = pytest.mark.skipif("pads" not in config.clean({}), reason="needs PR #6 (numpad, multi-pad, free layout)")


def test_numpad_is_a_bool_default_off():
    assert config.clean({})["keyboard"]["numpad"] is False
    assert config.clean({"keyboard": {"numpad": 1}})["keyboard"]["numpad"] is True
    assert config.clean({"keyboard": {"numpad": True}})["keyboard"]["numpad"] is True


def test_pads_clamped_to_1_to_4():
    assert config.clean({})["pads"] == 1
    assert config.clean({"pads": 0})["pads"] == 1
    assert config.clean({"pads": 9})["pads"] == 4
    assert config.clean({"pads": 3})["pads"] == 3


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
