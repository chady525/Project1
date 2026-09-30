"""Where CSI frames come from.

Source specs (used by the CLI):
    serial:/dev/ttyUSB0[@921600]   live ESP32 running esp-csi `csi_recv`
    file:capture.csi               replay a `wifisense record` capture
    sim:<scenario>[@seed]          synthetic data (empty, walking, sitting, demo)
"""

from __future__ import annotations

import time
from typing import Iterator, Optional

from .csi import CSIFrame, parse_line
from .simulator import SCENARIOS, SimConfig, Simulator


def serial_lines(port: str, baud: int = 921600) -> Iterator[str]:
    try:
        import serial  # pyserial
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pyserial is required for serial sources: pip install pyserial") from e
    with serial.Serial(port, baud, timeout=1) as ser:
        while True:
            raw = ser.readline()
            if raw:
                yield raw.decode("ascii", errors="ignore")


def open_source(spec: str, realtime: bool = False, duration: Optional[float] = None) -> Iterator[CSIFrame]:
    kind, _, arg = spec.partition(":")
    if kind == "serial":
        port, _, baud = arg.partition("@")
        frames = (f for f in map(parse_line, serial_lines(port, int(baud or 921600))) if f)
    elif kind == "file":
        frames = (f for f in map(parse_line, open(arg, encoding="ascii", errors="ignore")) if f)
    elif kind == "sim":
        name, _, seed = arg.partition("@")
        if name not in SCENARIOS:
            raise SystemExit(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
        sim = Simulator(SimConfig(seed=int(seed or 0)))
        start = time.time() if realtime else 0.0
        frames = (f for f, _ in sim.frames(SCENARIOS[name], start=start))
        if realtime:
            frames = _paced(frames)
    else:
        raise SystemExit(f"bad source {spec!r}: expected serial:, file: or sim:")
    return _limit(frames, duration) if duration else frames


def _paced(frames: Iterator[CSIFrame]) -> Iterator[CSIFrame]:
    for f in frames:
        delay = f.timestamp - time.time()
        if delay > 0:
            time.sleep(delay)
        yield f


def _limit(frames: Iterator[CSIFrame], seconds: float) -> Iterator[CSIFrame]:
    start = None
    for f in frames:
        if start is None:
            start = f.timestamp
        if f.timestamp - start > seconds:
            return
        yield f
