import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import server


def make_app(loopback_only=True):
    async def ok(request):
        return web.Response(text="ok")

    app = web.Application(middlewares=[server.guard])
    app["loopback_only"] = loopback_only
    app.router.add_get("/api/x", ok)
    app.router.add_get("/ws-like", ok)
    return app


def status(path, headers=None, loopback_only=True):
    async def run():
        async with TestClient(TestServer(make_app(loopback_only))) as client:
            r = await client.get(path, headers=headers or {})
            return r.status
    return asyncio.run(run())


def test_no_extra_headers_allowed():
    assert status("/api/x") == 200          # curl / Stream Deck
    assert status("/ws-like") == 200


def test_origin_must_match_host():
    async def run():
        async with TestClient(TestServer(make_app())) as c:
            h = f"{c.host}:{c.port}"
            same = (await c.get("/ws-like", headers={"Origin": f"http://{h}"})).status
            cross = (await c.get("/ws-like", headers={"Origin": "http://evil.example"})).status
            other_port = (await c.get("/ws-like", headers={"Origin": "http://127.0.0.1:1"})).status
            return same, cross, other_port
    assert asyncio.run(run()) == (200, 403, 403)


def test_non_loopback_host_refused_only_when_loopback_only():
    assert status("/ws-like", {"Host": "evil.example"}) == 403
    assert status("/ws-like", {"Host": "evil.example:8765"}) == 403
    assert status("/ws-like", {"Host": "localhost:8765"}) == 200
    assert status("/ws-like", {"Host": "evil.example"}, loopback_only=False) == 200


def test_sec_fetch_site_on_api():
    for v in ("cross-site", "same-site", "bogus"):
        assert status("/api/x", {"Sec-Fetch-Site": v}) == 403
    for v in ("same-origin", "none"):
        assert status("/api/x", {"Sec-Fetch-Site": v}) == 200
    # only /api/ is protected by this check
    assert status("/ws-like", {"Sec-Fetch-Site": "cross-site"}) == 200
