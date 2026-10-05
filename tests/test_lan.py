"""LAN access (a second PC loading the overlay): who is let in, and who never is."""
import argparse
import asyncio
import ipaddress

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import server

ALLOWED = ipaddress.ip_address("192.168.1.50")
NEIGHBOUR = ipaddress.ip_address("192.168.1.77")          # same network, not the PC that was named
STRANGER = ipaddress.ip_address("203.0.113.9")
TOKEN = "t" * 32


@pytest.fixture(autouse=True)
def lan_off_after():
    """Every test starts with LAN access off and leaves it off."""
    saved = dict(server.LAN)
    server._failures.clear()
    server._blocked.clear()
    yield
    server.LAN.clear()
    server.LAN.update(saved)
    server._failures.clear()
    server._blocked.clear()


def lan_on(networks=("192.168.1.50",)):
    server.LAN.update(networks=[ipaddress.ip_network(n) for n in networks], token=TOKEN, host="0.0.0.0", port=8765, ssl=None)


def options(*argv):
    ap = argparse.ArgumentParser()
    server.add_network_args(ap)
    return ap.parse_args(list(argv))


# ---------------------------------------------------------------- the options
def test_default_is_this_pc_only():
    assert server.lan_setup(options()) is None
    assert server.LAN["networks"] == [] and server.LAN["token"] is None


def test_listening_on_the_network_needs_allow_lan():
    msg = server.lan_setup(options("--host", "0.0.0.0"))
    assert msg and "--allow-lan" in msg and server.LAN["networks"] == []
    assert server.lan_setup(options("--host", "192.168.1.10"))                 # a specific address needs it too


def test_allow_lan_names_the_pcs_and_makes_a_secret():
    assert server.lan_setup(options("--host", "0.0.0.0", "--allow-lan", "192.168.1.50")) is None
    assert server.LAN["networks"] == [ipaddress.ip_network("192.168.1.50/32")]
    assert len(server.LAN["token"]) >= 20


def test_the_secret_is_kept_between_starts():
    server.lan_setup(options("--host", "0.0.0.0", "--allow-lan", "192.168.1.50"))
    first = server.LAN["token"]
    server.lan_setup(options("--host", "0.0.0.0", "--allow-lan", "192.168.1.60"))
    assert server.LAN["token"] == first


@pytest.mark.parametrize("wide", ["0.0.0.0/0", "::/0", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "2001:db8::/32"])
def test_whole_networks_are_refused(wide):
    msg = server.lan_setup(options("--host", "0.0.0.0", "--allow-lan", wide))
    assert msg and "too wide" in msg and server.LAN["networks"] == []


@pytest.mark.parametrize("fine", ["192.168.1.50", "192.168.1.0/24", "10.0.0.7,10.0.0.8", "2001:db8::1", "fd00::/64"])
def test_a_pc_or_a_small_network_is_accepted(fine):
    assert server.lan_setup(options("--host", "0.0.0.0", "--allow-lan", fine)) is None


def test_nonsense_and_misplaced_options_are_refused():
    assert "not an address" in server.lan_setup(options("--host", "0.0.0.0", "--allow-lan", "banana"))
    assert server.lan_setup(options("--allow-lan", "192.168.1.50"))                      # without a network --host there is nothing to allow
    assert server.lan_setup(options("--tls-cert", "x.pem"))                             # cert and key come as a pair
    assert "certificate" in server.lan_setup(options("--tls-cert", "nope.pem", "--tls-key", "nope.key")).lower()


# ---------------------------------------------------------------- who is let in
def test_this_pc_is_always_let_in():
    for ip in ("127.0.0.1", "::1"):
        assert server.lan_verdict(ipaddress.ip_address(ip), "") is None
    lan_on()
    assert server.lan_verdict(ipaddress.ip_address("127.0.0.1"), "") is None


def test_nobody_else_is_let_in_while_lan_is_off():
    assert server.lan_verdict(ALLOWED, TOKEN) == "this program only answers on this PC"


def test_the_named_pc_needs_the_secret():
    lan_on()
    assert server.lan_verdict(ALLOWED, TOKEN) is None
    assert server.lan_verdict(ALLOWED, "") == "wrong or missing secret"
    assert server.lan_verdict(ALLOWED, TOKEN[:-1] + "x") == "wrong or missing secret"


def test_a_neighbour_on_the_same_network_is_refused_even_with_the_secret():
    lan_on()
    assert server.lan_verdict(NEIGHBOUR, TOKEN) == "address not allowed"
    assert server.lan_verdict(STRANGER, TOKEN) == "address not allowed"
    assert server.lan_verdict(None, TOKEN) == "address not allowed"


def test_guessing_the_secret_gets_an_address_shut_out():
    lan_on()
    for i in range(server.FAIL_LIMIT):
        assert server.lan_verdict(ALLOWED, "guess%d" % i, now=100.0 + i) == "wrong or missing secret"
    assert "too many" in server.lan_verdict(ALLOWED, TOKEN, now=111.0)               # even the right one is refused for now
    assert server.lan_verdict(ALLOWED, TOKEN, now=111.0 + server.FAIL_BLOCK + 1) is None


def test_slow_wrong_tries_do_not_shut_anyone_out():
    lan_on()
    for i in range(server.FAIL_LIMIT * 2):
        server.lan_verdict(ALLOWED, "x", now=i * (server.FAIL_WINDOW + 1))
    assert server.lan_verdict(ALLOWED, TOKEN, now=10_000.0) is None


def test_an_ipv4_address_that_arrives_as_ipv6_counts_as_ipv4():
    assert server.remote_ip(type("R", (), {"remote": "::ffff:192.168.1.50"})()) == ALLOWED
    assert server.remote_ip(type("R", (), {"remote": "fe80::1%12"})()) == ipaddress.ip_address("fe80::1")
    assert server.remote_ip(type("R", (), {"remote": None})()) is None


# ---------------------------------------------------------------- through the real request pipeline
def served(requests, remote, lan=True):
    """Make `requests` (a list of (path, headers)) to a small app that has the real middlewares, as if they came from `remote`."""
    async def run():
        async def ok(request):
            return web.Response(text="ok")

        async def lan_info(request):
            return await server.api_lan(request)

        app = web.Application(middlewares=[server.security_headers, server.guard])
        app[server.LOOPBACK_ONLY] = False
        app.router.add_get("/page", ok)
        app.router.add_get("/api/lan", lan_info)
        if lan:
            lan_on()
        server.remote_ip = lambda request: remote
        async with TestClient(TestServer(app)) as c:
            out = []
            for path, headers in requests:
                r = await c.get(path, headers=headers)
                out.append((r.status, r.headers.get("Set-Cookie", ""), await r.text()))
            return out
    real = server.remote_ip
    try:
        return asyncio.run(run())
    finally:
        server.remote_ip = real


def test_requests_from_the_named_pc(monkeypatch):
    # (the test client keeps cookies like a browser does, so the refused tries come before the one that leaves a cookie behind)
    out = served([("/page", {}), ("/page?token=wrong", {}), (f"/page?token={TOKEN}", {}), ("/page", {}),
                  ("/page", {"X-IO-Token": TOKEN})], ALLOWED)
    assert [o[0] for o in out] == [403, 403, 200, 200, 200]
    assert "io_token=" + TOKEN in out[2][1] and "HttpOnly" in out[2][1] and "SameSite=Strict" in out[2][1]      # the link leaves a cookie behind
    assert out[0][2] == "wrong or missing secret" and out[3][1] == ""                                           # later requests just carry it


def test_requests_from_anywhere_else_never_work():
    for who in (NEIGHBOUR, STRANGER):
        out = served([(f"/page?token={TOKEN}", {}), ("/page", {"Cookie": f"io_token={TOKEN}"})], who)
        assert [o[0] for o in out] == [403, 403] and all("io_token" not in o[1] for o in out)


def test_the_secret_is_only_ever_handed_to_this_pc():
    this_pc = ipaddress.ip_address("127.0.0.1")
    status, _, body = served([("/api/lan", {})], this_pc)[0]
    assert status == 200 and TOKEN in body
    # the allowed PC passes the gate (it has the secret) but is still not told the secret
    status, _, body = served([(f"/api/lan?token={TOKEN}", {})], ALLOWED)[0]
    assert status == 403 and TOKEN not in body


def test_api_lan_when_lan_is_off():
    status, _, body = served([("/api/lan", {})], ipaddress.ip_address("127.0.0.1"), lan=False)[0]
    assert status == 200 and '"enabled": false' in body


# ---------------------------------------------------------------- https
def test_wss_is_allowed_only_for_https_pages():
    from types import SimpleNamespace as NS
    assert "ws://127.0.0.1:8765" in server.page_csp(NS(host="127.0.0.1:8765", secure=False), "n")
    assert "wss://127.0.0.1:8765" in server.page_csp(NS(host="127.0.0.1:8765", secure=True), "n")
