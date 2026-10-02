import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from wifisense.unifi import UniFiClient, UniFiError, ZoneConfig, ZoneTracker

AP1, AP2 = "f0:00:00:00:00:01", "f0:00:00:00:00:02"
APS = {AP1: "F1-North", AP2: "F1-South"}


def sta(mac, ap, last_seen=1000, **kw):
    return {"mac": mac, "ap_mac": ap, "last_seen": last_seen, **kw}


def test_counts_devices_per_zone_and_names_known_people():
    cfg = ZoneConfig(zones={"F1-North": "North"}, people={"aa:00:00:00:00:01": "Chady"})
    t = ZoneTracker(cfg, APS)
    out = t.update([sta("AA:00:00:00:00:01", AP1), sta("aa:00:00:00:00:02", AP1), sta("aa:00:00:00:00:03", AP2)], now=1000)
    assert out == {"North": {"count": 2, "people": ["Chady"]}, "F1-South": {"count": 1, "people": []}}


def test_skips_wired_ignored_and_stale():
    t = ZoneTracker(ZoneConfig(ignore=["aa:00:00:00:00:02"], stale_s=300), APS)
    out = t.update(
        [
            sta("aa:00:00:00:00:01", AP1, is_wired=True),
            sta("aa:00:00:00:00:02", AP1),
            sta("aa:00:00:00:00:03", AP1, last_seen=600),  # 400 s old
            {"mac": "aa:00:00:00:00:04", "last_seen": 1000},  # no AP
        ],
        now=1000,
    )
    assert out == {}


def test_roaming_needs_confirmation_and_bounces_are_ignored():
    t = ZoneTracker(ZoneConfig(confirm_polls=2), APS)
    phone = "aa:00:00:00:00:01"
    assert "F1-North" in t.update([sta(phone, AP1)], now=1000)
    assert "F1-North" in t.update([sta(phone, AP2)], now=1015)  # one sighting: not yet
    assert "F1-North" in t.update([sta(phone, AP1)], now=1030)  # bounced back: reset
    assert "F1-North" in t.update([sta(phone, AP2)], now=1045)
    assert "F1-South" in t.update([sta(phone, AP2)], now=1060)  # second in a row: moved


def test_device_leaving_disappears():
    t = ZoneTracker(ZoneConfig(), APS)
    t.update([sta("aa:00:00:00:00:01", AP1)], now=1000)
    assert t.update([], now=1015) == {}


class FakeController(BaseHTTPRequestHandler):
    unifi_os = True
    logins = 0

    def log_message(self, *a):
        pass

    def _send(self, code, body=None, headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(body or {}).encode())

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        expected = "/api/auth/login" if self.unifi_os else "/api/login"
        if self.path != expected or body["password"] != "pw":
            return self._send(404 if self.path != expected else 401)
        type(self).logins += 1
        self._send(200, headers=[("Set-Cookie", "TOKEN=t; Path=/"), ("X-CSRF-Token", "csrf")])

    def do_GET(self):
        prefix = "/proxy/network" if self.unifi_os else ""
        if "TOKEN=t" not in (self.headers.get("Cookie") or ""):
            return self._send(401)
        if self.unifi_os and self.headers.get("X-CSRF-Token") != "csrf":
            return self._send(403)
        if self.path == f"{prefix}/api/s/default/stat/device":
            return self._send(200, {"data": [{"mac": AP1.upper(), "name": "F1-North", "type": "uap"},
                                             {"mac": "sw", "name": "Switch", "type": "usw"}]})
        if self.path == f"{prefix}/api/s/default/stat/sta":
            return self._send(200, {"data": [sta("aa:00:00:00:00:01", AP1)]})
        self._send(404)


@pytest.fixture(params=[True, False], ids=["unifi-os", "classic"])
def controller(request):
    handler = type("H", (FakeController,), {"unifi_os": request.param, "logins": 0})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_client_logs_in_and_reads_both_controller_flavours(controller):
    c = UniFiClient(controller, "u", "pw")
    assert c.access_points() == {AP1: "F1-North"}
    assert c.clients()[0]["ap_mac"] == AP1


def test_bad_password_is_a_clear_error(controller):
    with pytest.raises(UniFiError, match="LOCAL account"):
        UniFiClient(controller, "u", "wrong").clients()
