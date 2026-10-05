"""The server's own logic that the config, theme and guard tests don't reach: pages, update check, key codes, gyro toggles, stalled clients."""
import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from types import SimpleNamespace as NS

import config
import server


# ---------------------------------------------------------------- pages: nonce and CSP
def test_pages_get_a_fresh_nonce_each_time():
    async def run():
        app = web.Application()
        app.router.add_get("/", server.static_page("index.html"))
        async with TestClient(TestServer(app)) as c:
            out = []
            for _ in range(2):
                r = await c.get("/")
                out.append((await r.text(), r.headers["Content-Security-Policy"], r.headers["X-Content-Type-Options"]))
            return out
    (html1, csp1, nosniff), (html2, csp2, _) = asyncio.run(run())
    n1 = csp1.split("'nonce-")[1].split("'")[0]
    n2 = csp2.split("'nonce-")[1].split("'")[0]
    assert n1 != n2 and nosniff == "nosniff"
    assert f'<script nonce="{n1}">' in html1 and "<script>" not in html1          # every inline script carries this response's nonce
    assert "script-src 'self' 'nonce-" in csp1 and "'unsafe-inline'" not in csp1.split("style-src")[0]
    assert "frame-ancestors 'self'" in csp1


def test_csp_uses_the_host_only_when_it_looks_like_one():
    ok = server.page_csp(NS(host="127.0.0.1:8765", secure=False), "abc")
    assert "connect-src 'self' ws://127.0.0.1:8765;" in ok
    assert "ws://[::1]:8765" in server.page_csp(NS(host="[::1]:8765", secure=False), "abc")
    for odd in ("evil.example; script-src *", "a b", "", None, "x/y"):
        assert "ws://127.0.0.1;" in server.page_csp(NS(host=odd, secure=False), "abc")             # falls back instead of echoing it


def test_every_response_says_nosniff():
    async def run():
        async def ok(request):
            return web.Response(text="ok")
        app = web.Application(middlewares=[server.security_headers])
        app.router.add_get("/x", ok)
        async with TestClient(TestServer(app)) as c:
            return (await c.get("/x")).headers.get("X-Content-Type-Options"), (await c.get("/missing")).headers.get("X-Content-Type-Options")
    assert asyncio.run(run()) == ("nosniff", "nosniff")


# ---------------------------------------------------------------- update check
def test_parse_version():
    assert server.parse_version("v1.2.3") == (1, 2, 3) and server.parse_version("1.2") == (1, 2, 0)
    for bad in ("", None, "v1", "1.2.3.4", "latest", "v1.2.x", "1.2.3-beta", "99999.1.1"):
        assert server.parse_version(bad) is None
    assert server.parse_version("v1.10.0") > server.parse_version("v1.9.9")


def update_with(monkeypatch, reply, status=200):
    """Run server.check_update against a local stand-in for GitHub that answers `reply`."""
    async def run():
        async def handler(request):
            return web.json_response(reply, status=status)
        app = web.Application()
        app.router.add_get("/latest", handler)
        async with TestServer(app) as srv:
            monkeypatch.setattr(server, "UPDATE_API", str(srv.make_url("/latest")))
            monkeypatch.setitem(server.update, "latest", None)
            return await server.check_update()
    return asyncio.run(run())


def test_update_check_accepts_a_good_reply(monkeypatch):
    r = update_with(monkeypatch, {"tag_name": "v9.9.9", "html_url": server.RELEASES_URL + "tag/v9.9.9"})
    assert r["latest"] == "9.9.9" and r["available"] is True and r["error"] is None


def test_update_check_same_version_is_not_an_update(monkeypatch):
    r = update_with(monkeypatch, {"tag_name": "v" + server.VERSION, "html_url": server.RELEASES_URL + "tag/x"})
    assert r["available"] is False and r["error"] is None


@pytest.mark.parametrize("reply", [
    {"tag_name": "v9.9.9", "html_url": "https://evil.example/download"},                       # a link to somewhere else
    {"tag_name": "v9.9.9", "html_url": "https://github.com/someone-else/repo/releases/x"},      # another project's releases
    {"tag_name": "not a version", "html_url": server.RELEASES_URL + "tag/x"},
    {"tag_name": "v9.9.9"}, {"html_url": server.RELEASES_URL + "tag/x"}, [], "text",
])
def test_update_check_refuses_odd_replies(monkeypatch, reply):
    r = update_with(monkeypatch, reply)
    assert r["available"] is False and r["error"]


def test_update_check_refuses_a_non_200(monkeypatch):
    r = update_with(monkeypatch, {"tag_name": "v9.9.9", "html_url": server.RELEASES_URL + "tag/x"}, status=403)
    assert r["available"] is False and "403" in r["error"]


# ---------------------------------------------------------------- keys: Enter and numpad Enter
def key(vk):
    return NS(vk=vk)


@pytest.fixture
def keys(monkeypatch):
    out = []
    monkeypatch.setattr(server.hub, "emit", lambda m: out.append(tuple(m["k"])))
    server._return_flags.clear()
    server._down_keys.clear()
    return out


def feed(events):
    """Like the real thing: every event goes through the hook's filter first, then pynput calls on_press / on_release for them later, in order."""
    for vk, ext, down in events:
        server.kb_filter(0x100 if down else 0x101, NS(vkCode=vk, flags=1 if ext else 0))
    for vk, ext, down in events:
        (server.on_press if down else server.on_release)(key(vk))


def test_enter_and_numpad_enter_stay_apart(keys):
    feed([(13, 0, 1), (13, 0, 0), (13, 1, 1), (13, 1, 0)])
    assert keys == [(13, 1), (13, 0), (269, 1), (269, 0)]


def test_extended_keys_in_between_do_not_confuse_enter(keys):
    # arrows are extended too, and their events arrive between the two Enters
    feed([(13, 0, 1), (38, 1, 1), (13, 1, 1), (37, 1, 1), (13, 0, 0), (13, 1, 0)])
    assert keys == [(13, 1), (38, 1), (269, 1), (37, 1), (13, 0), (269, 0)]


def test_other_keys_are_untouched_and_repeats_are_ignored(keys):
    feed([(65, 0, 1), (65, 0, 1), (65, 0, 1), (65, 0, 0)])
    assert keys == [(65, 1), (65, 0)]


# ---------------------------------------------------------------- gyro toggles (Stream Deck URLs)
def test_gyro_toggles(monkeypatch):
    monkeypatch.setattr(server.hub, "emit", lambda m: None)
    c = config.shared()
    assert server.set_gyro("tilt", "on")["gyro"] == "tilt"
    assert server.set_gyro("aim", "on")["gyro"] == "both"
    assert server.set_gyro("tilt", "toggle")["gyro"] == "aim"
    assert server.set_gyro("all", "toggle")["gyro"] == "off"          # something was on, so "toggle both" turns everything off
    assert server.set_gyro("all", "toggle")["gyro"] == "both"
    assert server.set_gyro("all", "off")["gyro"] == "off"
    assert c.settings["scope"]["gyro"] is True                          # the URLs make Gyro an all-presets setting
    assert server.gyro_state(c) == ("off", False, False)


# ---------------------------------------------------------------- clients that stop reading
class FakeWs:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def test_a_stalled_client_is_dropped_and_the_others_keep_receiving(monkeypatch):
    async def run():
        monkeypatch.setattr(server, "hub", server.Hub())
        server.hub.queue = asyncio.Queue()
        stalled, healthy = FakeWs(), FakeWs()
        sq, hq = asyncio.Queue(maxsize=1), asyncio.Queue(maxsize=10)
        sq.put_nowait("old")                                              # its queue is full and nobody is draining it
        server.hub.clients = {stalled: sq, healthy: hq}
        task = asyncio.create_task(server.broadcaster())
        await server.hub.queue.put("new")
        await asyncio.sleep(0.05)
        task.cancel()
        return stalled, healthy, hq
    stalled, healthy, hq = asyncio.run(run())
    assert stalled.closed and not healthy.closed
    assert hq.get_nowait() == "new"
    assert stalled not in server.hub.clients and healthy in server.hub.clients
