"""Command line entry point: record, calibrate, run."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime

from .csi import format_line
from .detector import Baseline, PresenceDetector, calibrate
from .sources import open_source


def cmd_record(args: argparse.Namespace) -> None:
    n = 0
    with open(args.output, "w", encoding="ascii") as out:
        for n, frame in enumerate(open_source(args.source, duration=args.seconds), 1):
            out.write(format_line(frame, n) + "\n")
    print(f"wrote {n} frames to {args.output}", file=sys.stderr)


def cmd_calibrate(args: argparse.Namespace) -> None:
    print(f"calibrating for {args.seconds:.0f} s - keep the room EMPTY and still", file=sys.stderr)
    baseline = calibrate(open_source(args.source, duration=args.seconds))
    baseline.save(args.output)
    print(f"saved {args.output}: {asdict(baseline)}", file=sys.stderr)


def post_webhook(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=5).close()
    except OSError as e:
        print(f"webhook failed: {e}", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> None:
    detector = PresenceDetector(Baseline.load(args.baseline), hold_s=args.hold)
    state = None
    for frame in open_source(args.source, realtime=args.realtime):
        reading = detector.update(frame)
        if reading is None:
            continue
        payload = asdict(reading)
        if args.json:
            print(json.dumps(payload), flush=True)
        if reading.present != state:
            state = reading.present
            if not args.json:
                why = "motion" if reading.motion_hit else "breathing" if reading.breath_hit else "no evidence"
                ts = datetime.fromtimestamp(reading.timestamp).strftime("%H:%M:%S") if reading.timestamp > 1e9 \
                    else f"t={reading.timestamp:7.1f}s"
                print(f"{ts}  {'PRESENT' if state else 'EMPTY  '}  ({why})", flush=True)
            if args.webhook:
                post_webhook(args.webhook, payload)


def _unifi_client(args: argparse.Namespace):
    from .unifi import UniFiClient

    missing = [v for v in ("UNIFI_URL", "UNIFI_USER", "UNIFI_PASS") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} (use a local read-only UniFi account)")
    return UniFiClient(
        os.environ["UNIFI_URL"], os.environ["UNIFI_USER"], os.environ["UNIFI_PASS"],
        site=args.site, verify_tls=args.verify_tls,
    )


def cmd_aps(args: argparse.Namespace) -> None:
    client = _unifi_client(args)
    aps = client.access_points()
    counts: dict = {}
    for c in client.clients():
        if c.get("ap_mac"):
            counts[c["ap_mac"].lower()] = counts.get(c["ap_mac"].lower(), 0) + 1
    for mac, name in sorted(aps.items(), key=lambda kv: kv[1]):
        print(f"{name:30s} {mac}  {counts.get(mac, 0):3d} clients")


def cmd_zones(args: argparse.Namespace) -> None:
    from .unifi import UniFiError, ZoneConfig, ZoneTracker

    client = _unifi_client(args)
    tracker = ZoneTracker(ZoneConfig.load(args.config), client.access_points())
    last = None
    while True:
        try:
            occupancy = tracker.update(client.clients())
        except UniFiError as e:
            print(f"error: {e}", file=sys.stderr)
            time.sleep(args.interval)
            continue
        if occupancy != last:
            last = occupancy
            payload = {"timestamp": time.time(), "zones": occupancy}
            if args.json:
                print(json.dumps(payload), flush=True)
            else:
                print(datetime.now().strftime("%H:%M:%S"), flush=True)
                for zone, z in occupancy.items():
                    who = f"  ({', '.join(z['people'])})" if z["people"] else ""
                    print(f"  {zone:30s} {z['count']:3d}{who}", flush=True)
                if not occupancy:
                    print("  (no devices)", flush=True)
            if args.webhook:
                post_webhook(args.webhook, payload)
        if args.once:
            return
        time.sleep(args.interval)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="wifisense", description="Wi-Fi CSI presence detection")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="save raw CSI to a file for later replay")
    r.add_argument("--source", required=True)
    r.add_argument("--seconds", type=float, default=60)
    r.add_argument("-o", "--output", required=True)
    r.set_defaults(func=cmd_record)

    c = sub.add_parser("calibrate", help="learn thresholds from an empty room")
    c.add_argument("--source", required=True)
    c.add_argument("--seconds", type=float, default=90)
    c.add_argument("-o", "--output", default="baseline.json")
    c.set_defaults(func=cmd_calibrate)

    u = sub.add_parser("run", help="detect presence")
    u.add_argument("--source", required=True)
    u.add_argument("--baseline", default="baseline.json")
    u.add_argument("--hold", type=float, default=10.0, help="seconds without evidence before reporting empty")
    u.add_argument("--json", action="store_true", help="print every reading as JSON lines")
    u.add_argument("--webhook", help="POST the reading as JSON to this URL on every state change")
    u.add_argument("--realtime", action="store_true", help="pace sim: sources at wall-clock speed")
    u.set_defaults(func=cmd_run)

    for name, func, helptext in (
        ("aps", cmd_aps, "list UniFi access points and their client counts"),
        ("zones", cmd_zones, "report device occupancy per access-point zone from UniFi"),
    ):
        z = sub.add_parser(name, help=helptext, description="Reads UNIFI_URL, UNIFI_USER, UNIFI_PASS.")
        z.add_argument("--site", default="default")
        z.add_argument("--verify-tls", action="store_true", help="verify the controller certificate")
        z.set_defaults(func=func)
        if name == "zones":
            z.add_argument("--config", help="zone/people/ignore JSON (see zones.example.json)")
            z.add_argument("--interval", type=float, default=15.0, help="poll period in seconds")
            z.add_argument("--json", action="store_true")
            z.add_argument("--webhook", help="POST occupancy JSON here whenever it changes")
            z.add_argument("--once", action="store_true", help="poll once and exit")

    args = p.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
