"""Zone occupancy from a UniFi controller: which access point each phone is on.

This does not sense bodies. It reports where *Wi-Fi devices* are, at the
resolution of one access point's coverage area ("zone"), and is only as good
as phones staying connected (locked iPhones go quiet for minutes).

Works with both controller flavours:
  * UniFi OS (Dream Machine / UDR / Cloud Key Gen2+ / UniFi OS Server):
        POST /api/auth/login, data under /proxy/network/api/...
  * Classic Network application (usually port 8443):
        POST /api/login, data under /api/...
"""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


class UniFiError(RuntimeError):
    pass


class UniFiClient:
    def __init__(self, url: str, username: str, password: str, site: str = "default", verify_tls: bool = False):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.site = site
        ctx = ssl.create_default_context()
        if not verify_tls:  # controllers ship with self-signed certificates
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self._prefix: Optional[str] = None
        self._csrf: Optional[str] = None

    def _request(self, method: str, path: str, body: Optional[dict] = None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        with self._opener.open(req, timeout=10) as resp:
            token = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token")
            if token:
                self._csrf = token
            return json.loads(resp.read() or b"{}")

    def login(self) -> None:
        creds = {"username": self.username, "password": self.password, "remember": True}
        errors = []
        for login_path, prefix in (("/api/auth/login", "/proxy/network"), ("/api/login", "")):
            try:
                self._request("POST", login_path, creds)
                self._prefix = prefix
                return
            except urllib.error.HTTPError as e:
                errors.append(f"{login_path}: HTTP {e.code}")
            except urllib.error.URLError as e:
                raise UniFiError(f"cannot reach controller at {self.url}: {e.reason}") from e
        raise UniFiError("login failed (" + "; ".join(errors) + "). Use a LOCAL account, not a UI.com/SSO one.")

    def _get(self, endpoint: str) -> List[dict]:
        if self._prefix is None:
            self.login()
        path = f"{self._prefix}/api/s/{self.site}/{endpoint}"
        try:
            return self._request("GET", path).get("data", [])
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise UniFiError(f"GET {path}: HTTP {e.code}") from e
        self.login()  # session expired
        return self._request("GET", path).get("data", [])

    def clients(self) -> List[dict]:
        return self._get("stat/sta")

    def access_points(self) -> Dict[str, str]:
        """AP MAC -> AP name as set in the UniFi app."""
        return {d["mac"].lower(): d.get("name") or d["mac"] for d in self._get("stat/device") if d.get("type") == "uap"}


@dataclass
class ZoneConfig:
    """Loaded from a JSON file; every key is optional.

    zones:   AP name or MAC -> zone label (default: the AP's own name)
    people:  client MAC -> person label (named tracking, opt-in)
    ignore:  client MACs never counted (printers, TVs, fixed IoT)
    """

    zones: Dict[str, str] = field(default_factory=dict)
    people: Dict[str, str] = field(default_factory=dict)
    ignore: List[str] = field(default_factory=list)
    stale_s: float = 300.0  # drop a device not heard from for this long
    confirm_polls: int = 2  # polls on a new AP before believing a move

    @classmethod
    def load(cls, path: Optional[str]) -> "ZoneConfig":
        if not path:
            return cls()
        raw = json.loads(Path(path).read_text())
        cfg = cls(**raw)
        cfg.people = {k.lower(): v for k, v in cfg.people.items()}
        cfg.ignore = [m.lower() for m in cfg.ignore]
        return cfg


@dataclass
class _Track:
    zone: str
    pending: Optional[str] = None
    pending_count: int = 0


class ZoneTracker:
    """Turns raw client lists into stable zone assignments."""

    def __init__(self, config: ZoneConfig, ap_names: Dict[str, str]):
        self.cfg = config
        self.ap_names = ap_names
        self._tracks: Dict[str, _Track] = {}

    def zone_for_ap(self, ap_mac: str) -> str:
        ap_mac = ap_mac.lower()
        name = self.ap_names.get(ap_mac, ap_mac)
        return self.cfg.zones.get(name) or self.cfg.zones.get(ap_mac) or name

    def update(self, clients: List[dict], now: Optional[float] = None) -> Dict[str, dict]:
        """Returns {zone: {"count": n, "people": [...]}} for occupied zones."""
        now = time.time() if now is None else now
        seen = set()
        for c in clients:
            mac = c.get("mac", "").lower()
            if c.get("is_wired") or not c.get("ap_mac") or mac in self.cfg.ignore:
                continue
            if now - float(c.get("last_seen", now)) > self.cfg.stale_s:
                continue
            seen.add(mac)
            zone = self.zone_for_ap(c["ap_mac"])
            track = self._tracks.get(mac)
            if track is None:
                self._tracks[mac] = _Track(zone)
            elif zone == track.zone:
                track.pending, track.pending_count = None, 0
            else:
                # Phones roam late and bounce near doorways; require repeat sightings.
                if zone == track.pending:
                    track.pending_count += 1
                else:
                    track.pending, track.pending_count = zone, 1
                if track.pending_count >= self.cfg.confirm_polls:
                    self._tracks[mac] = _Track(zone)
        for mac in list(self._tracks):
            if mac not in seen:
                del self._tracks[mac]

        out: Dict[str, dict] = {}
        for mac, track in self._tracks.items():
            z = out.setdefault(track.zone, {"count": 0, "people": []})
            z["count"] += 1
            if mac in self.cfg.people:
                z["people"].append(self.cfg.people[mac])
        for z in out.values():
            z["people"].sort()
        return dict(sorted(out.items()))
