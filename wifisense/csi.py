"""CSI frame model and parser for ESP32 `CSI_DATA` serial lines.

Espressif's esp-csi examples print one line per received packet:

    CSI_DATA,<seq>,<mac>,<rssi>,...,"[i0,r0,i1,r1,...]"

The bracketed array holds int8 pairs per subcarrier, imaginary part first.
The number of header fields differs between esp-csi versions, so the parser
only relies on the prefix, the RSSI position and the trailing array.

Recordings made by `wifisense record` prefix each line with the host receive
time and a tab: `<unix_seconds>\tCSI_DATA,...`.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class CSIFrame:
    timestamp: float  # seconds (host clock or simulated clock)
    rssi: int
    csi: np.ndarray  # complex, one value per subcarrier

    @property
    def amplitude(self) -> np.ndarray:
        return np.abs(self.csi)


def parse_line(line: str, timestamp: Optional[float] = None) -> Optional[CSIFrame]:
    """Parse one serial/recorded line. Returns None for anything that isn't CSI."""
    line = line.strip()
    if "\t" in line:
        prefix, _, rest = line.partition("\t")
        try:
            timestamp = float(prefix)
            line = rest
        except ValueError:
            pass
    start = line.find("CSI_DATA")
    if start < 0:
        return None
    line = line[start:]

    lb, rb = line.find("["), line.rfind("]")
    if lb < 0 or rb < lb:
        return None
    try:
        raw = np.array(line[lb + 1 : rb].replace(",", " ").split(), dtype=np.float32)
    except ValueError:
        return None
    if raw.size < 2 or raw.size % 2:
        return None
    csi = raw[1::2] + 1j * raw[0::2]

    header = next(csv.reader([line[:lb]]), [])
    rssi = 0
    if len(header) > 3:
        try:
            rssi = int(header[3])
        except ValueError:
            pass

    return CSIFrame(timestamp=time.time() if timestamp is None else timestamp, rssi=rssi, csi=csi)


def format_line(frame: CSIFrame, seq: int = 0) -> str:
    """Serialise a frame in the recorded-line format (used by the simulator/recorder)."""
    pairs = np.empty(frame.csi.size * 2, dtype=np.int64)
    pairs[0::2] = np.round(frame.csi.imag).astype(np.int64)
    pairs[1::2] = np.round(frame.csi.real).astype(np.int64)
    body = ",".join(map(str, pairs))
    return f'{frame.timestamp:.6f}\tCSI_DATA,{seq},00:00:00:00:00:00,{frame.rssi},"[{body}]"'
