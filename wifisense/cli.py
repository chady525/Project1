"""Command line entry point: record, calibrate, run."""

from __future__ import annotations

import argparse
import json
import sys
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

    args = p.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
