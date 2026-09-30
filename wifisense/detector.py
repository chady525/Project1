"""Presence detection from CSI amplitude.

Two independent pieces of evidence, both compared against thresholds learned
from an empty-room calibration:

* motion   - how much subcarrier amplitudes fluctuate over the last couple of
             seconds. Catches anyone walking or shifting position.
* breathing - how strongly the dominant amplitude components oscillate at
             breathing rate (0.1-0.7 Hz) over the last ~20 s, relative to the
             spectral noise floor. This is what catches a person sitting still,
             which motion alone misses.

A person is reported present while either piece of evidence was seen within
the last `hold_s` seconds.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Deque, Iterable, List, Optional, Tuple

import numpy as np

from .csi import CSIFrame

BREATH_BAND = (0.1, 0.7)  # Hz, 6-42 breaths/min
NOISE_BAND = (1.0, 4.5)  # Hz
RESAMPLE_HZ = 10.0
N_COMPONENTS = 3


@dataclass
class Baseline:
    motion_threshold: float
    breath_threshold: float
    n_subcarriers: int

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: str | Path) -> "Baseline":
        return cls(**json.loads(Path(path).read_text()))


@dataclass
class Reading:
    timestamp: float
    present: bool
    motion: float  # raw motion score
    breathing: float  # raw breathing score (0 until the window fills)
    breath_bpm: Optional[float]  # estimated rate when breathing evidence is present
    motion_hit: bool
    breath_hit: bool


class FeatureExtractor:
    """Keeps a sliding window of normalised amplitudes and computes scores."""

    def __init__(self, motion_window_s: float = 2.0, breath_window_s: float = 20.0):
        self.motion_window_s = motion_window_s
        self.breath_window_s = breath_window_s
        self._t: Deque[float] = deque()
        self._amp: Deque[np.ndarray] = deque()
        self.n_subcarriers: Optional[int] = None
        self.dropped = 0

    def push(self, frame: CSIFrame) -> bool:
        amp = frame.amplitude.astype(np.float64)
        if self.n_subcarriers is None:
            self.n_subcarriers = amp.size
        if amp.size != self.n_subcarriers:
            # Mixed packet types (e.g. HT vs legacy) report different lengths.
            self.dropped += 1
            return False
        self._t.append(frame.timestamp)
        self._amp.append(amp)
        horizon = frame.timestamp - self.breath_window_s
        while self._t and self._t[0] < horizon:
            self._t.popleft()
            self._amp.popleft()
        return True

    def _normalised(self, t_from: float) -> Tuple[np.ndarray, np.ndarray]:
        t = np.fromiter(self._t, float)
        sel = t >= t_from
        amp = np.array(self._amp)[sel]
        used = amp.mean(axis=0) > 1.0  # drop null/guard subcarriers
        amp = amp[:, used]
        # Per-frame gain normalisation cancels AGC steps that hit all subcarriers equally.
        amp = amp / np.maximum(amp.mean(axis=1, keepdims=True), 1e-9)
        return t[sel], amp

    def motion(self) -> float:
        if len(self._t) < 10:
            return 0.0
        _, amp = self._normalised(self._t[-1] - self.motion_window_s)
        if amp.shape[0] < 10 or amp.shape[1] == 0:
            return 0.0
        return float(np.mean(amp.std(axis=0)))

    def breathing(self) -> Tuple[float, Optional[float]]:
        if not self._t or self._t[-1] - self._t[0] < 0.9 * self.breath_window_s:
            return 0.0, None
        t, amp = self._normalised(self._t[0])
        if amp.shape[1] < N_COMPONENTS:
            return 0.0, None
        # Bin to a uniform grid (ESP32 packet timing is irregular).
        idx = ((t - t[0]) * RESAMPLE_HZ).astype(int)
        n_bins = idx[-1] + 1
        counts = np.bincount(idx, minlength=n_bins)
        filled = counts > 0
        grid = np.empty((n_bins, amp.shape[1]))
        for k in range(amp.shape[1]):
            sums = np.bincount(idx, weights=amp[:, k], minlength=n_bins)
            grid[filled, k] = sums[filled] / counts[filled]
        if not filled.all():
            x = np.arange(n_bins)
            for k in range(amp.shape[1]):
                grid[:, k] = np.interp(x, x[filled], grid[filled, k])

        # Remove per-subcarrier mean and linear drift, then take principal components.
        x = np.arange(n_bins)
        grid -= np.polynomial.polynomial.polyvander(x, 1) @ np.linalg.lstsq(
            np.polynomial.polynomial.polyvander(x, 1), grid, rcond=None
        )[0]
        _, _, vt = np.linalg.svd(grid, full_matrices=False)
        comps = grid @ vt[:N_COMPONENTS].T

        freqs = np.fft.rfftfreq(n_bins, 1 / RESAMPLE_HZ)
        spec = np.abs(np.fft.rfft(comps * np.hanning(n_bins)[:, None], axis=0)) ** 2
        band = (freqs >= BREATH_BAND[0]) & (freqs <= BREATH_BAND[1])
        noise = (freqs >= NOISE_BAND[0]) & (freqs <= NOISE_BAND[1])
        floor = np.median(spec[noise], axis=0) + 1e-12
        ratios = spec[band] / floor
        best = np.unravel_index(np.argmax(ratios), ratios.shape)
        return float(ratios[best]), float(freqs[band][best[0]] * 60)


class PresenceDetector:
    def __init__(
        self,
        baseline: Baseline,
        hold_s: float = 10.0,
        step_s: float = 0.5,
        motion_window_s: float = 2.0,
        breath_window_s: float = 20.0,
    ):
        self.baseline = baseline
        self.hold_s = hold_s
        self.step_s = step_s
        self.features = FeatureExtractor(motion_window_s, breath_window_s)
        self._next_eval: Optional[float] = None
        self._last_evidence: Optional[float] = None

    def update(self, frame: CSIFrame) -> Optional[Reading]:
        """Feed one frame. Returns a Reading every `step_s` seconds, else None."""
        if not self.features.push(frame):
            return None
        t = frame.timestamp
        if self._next_eval is None:
            self._next_eval = t + self.step_s
        if t < self._next_eval:
            return None
        self._next_eval = t + self.step_s

        motion = self.features.motion()
        breathing, bpm = self.features.breathing()
        motion_hit = motion > self.baseline.motion_threshold
        breath_hit = breathing > self.baseline.breath_threshold
        if motion_hit or breath_hit:
            self._last_evidence = t
        present = self._last_evidence is not None and t - self._last_evidence <= self.hold_s
        return Reading(t, present, motion, breathing, bpm if breath_hit else None, motion_hit, breath_hit)


def calibrate(
    frames: Iterable[CSIFrame],
    motion_margin: float = 1.5,
    breath_margin: float = 1.5,
    step_s: float = 0.5,
) -> Baseline:
    """Learn thresholds from an empty-room capture (aim for 60 s or more)."""
    fx = FeatureExtractor()
    motions: List[float] = []
    breaths: List[float] = []
    next_eval = None
    for frame in frames:
        if not fx.push(frame):
            continue
        if next_eval is None:
            next_eval = frame.timestamp + fx.motion_window_s
        if frame.timestamp < next_eval:
            continue
        next_eval = frame.timestamp + step_s
        motions.append(fx.motion())
        score, _ = fx.breathing()
        if score > 0:
            breaths.append(score)
    if len(motions) < 10:
        raise ValueError("calibration capture too short: need at least ~10 s of CSI")
    if len(breaths) < 10:
        raise ValueError(
            f"calibration capture too short for breathing baseline: need > {fx.breath_window_s + 5:.0f} s"
        )
    return Baseline(
        motion_threshold=float(np.percentile(motions, 99) * motion_margin),
        breath_threshold=float(np.percentile(breaths, 99) * breath_margin),
        n_subcarriers=fx.n_subcarriers or 0,
    )
