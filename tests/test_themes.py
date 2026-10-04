import os

import pytest

import themes


@pytest.fixture
def th(tmp_path, monkeypatch):
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    (builtin / "pixel").mkdir(parents=True)
    (builtin / "pixel" / "theme.css").write_text("/* builtin */")
    (builtin / "pixel" / "evil.js").write_text("alert(1)")
    (builtin / "pixel" / "page.html").write_text("<html>")
    user.mkdir()
    monkeypatch.setattr(themes, "USER_DIR", user)
    t = themes.Themes(builtin)
    t.user = user
    return t


def test_resolves_valid_file(th):
    p = th.resolve("pixel", "theme.css")
    assert p is not None and p.read_text() == "/* builtin */"
    assert th.resolve("pixel", "missing.css") is None


def test_refuses_traversal_absolute_and_bad_suffix(th, tmp_path):
    (tmp_path / "builtin" / "secret.css").write_text("x")
    assert th.resolve("pixel", "../secret.css") is None
    assert th.resolve("pixel", "a/../../secret.css") is None
    assert th.resolve("pixel", str(tmp_path / "builtin" / "secret.css")) is None
    assert th.resolve("pixel", "evil.js") is None
    assert th.resolve("pixel", "page.html") is None


def test_refuses_bad_theme_ids(th):
    for bad in ("", None, "..", "../pixel", "a/b", ".hidden", "x" * 41):
        assert th.resolve(bad, "theme.css") is None


def test_refuses_symlink_escaping_theme_folder(th, tmp_path):
    outside = tmp_path / "outside.css"
    outside.write_text("secret")
    try:
        os.symlink(outside, tmp_path / "builtin" / "pixel" / "link.css")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted here")
    assert th.resolve("pixel", "link.css") is None


def test_user_theme_overrides_builtin(th):
    (th.user / "pixel").mkdir()
    (th.user / "pixel" / "theme.css").write_text("/* user */")
    assert th.resolve("pixel", "theme.css").read_text() == "/* user */"
    # a file only the built-in has still falls back to the built-in
    (th.builtin / "pixel" / "extra.css").write_text("/* only builtin */")
    assert th.resolve("pixel", "extra.css").read_text() == "/* only builtin */"
