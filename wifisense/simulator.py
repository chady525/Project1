"""Synthetic ESP32-like CSI so the pipeline can be developed without hardware.

Channel model: a sum of static multipath reflections plus, when someone is in
the room, one extra path off the body whose length changes as they move or
breathe. A 5 mm chest displacement changes the reflected path by ~1 cm, which
at 2.4 GHz (lambda ~12.5 cm) rotates that path's phase by ~0.5 rad and shows
up as a small periodic ripple in subcarrier amplitudes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Tuple

import numpy as np

from .csi import CSIFrame

C = 299_792_458.0
FC = 2.437e9  # channel 6
SUBCARRIER_SPACING = 312.5e3
N_SUB = 64
# LLTF on ESP32: DC and guard subcarriers are reported as zero.
NULL_SUBCARRIERS = [0, *range(27, 38)]

# (state, seconds). States: empty, walking, sitting.
SCENARIOS = {
    "empty": [("empty", 120)],
    "walking": [("walking", 120)],
    "sitting": [("sitting", 120)],
    "demo": [("empty", 40), ("walking", 15), ("sitting", 60), ("walking", 10), ("empty", 60)],
}


@dataclass
class SimConfig:
    rate_hz: float = 100.0
    snr_db: float = 25.0
    breath_hz: float = 0.25
    breath_amp_m: float = 0.005
    person_gain: float = 0.35
    agc_jitter: float = 0.02
    seed: int = 0


class Simulator:
    def __init__(self, config: SimConfig | None = None):
        self.cfg = config or SimConfig()
        self.rng = np.random.default_rng(self.cfg.seed)
        k = np.arange(N_SUB) - N_SUB // 2
        self.freqs = FC + k * SUBCARRIER_SPACING
        n_paths = 6
        self.static_len = self.rng.uniform(3.0, 25.0, n_paths)
        self.static_gain = self.rng.uniform(0.2, 1.0, n_paths) * np.exp(1j * self.rng.uniform(0, 2 * np.pi, n_paths))
        self.static_gain[0] = 1.0  # line of sight
        self.static_h = self._paths(self.static_len, self.static_gain)
        self.scale = 25.0 / np.mean(np.abs(self.static_h))  # int8 range like ESP32
        self.mask = np.ones(N_SUB, bool)
        self.mask[NULL_SUBCARRIERS] = False
        self._pos = 8.0  # person reflected path length (m)
        self._vel = 0.0

    def _paths(self, lengths: np.ndarray, gains: np.ndarray) -> np.ndarray:
        phase = -2j * np.pi * np.outer(lengths, self.freqs) / C
        return (gains[:, None] * np.exp(phase)).sum(axis=0) / np.sqrt(len(lengths))

    def _person_len(self, state: str, t: float, dt: float) -> float | None:
        if state == "empty":
            return None
        if state == "walking":
            # Bounded random walk at ~1 m/s.
            self._vel = 0.9 * self._vel + self.rng.normal(0, 0.4)
            self._vel = float(np.clip(self._vel, -1.5, 1.5))
            self._pos = float(np.clip(self._pos + self._vel * dt, 4.0, 14.0))
            return self._pos
        # sitting: breathing plus tiny postural sway
        breath = self.cfg.breath_amp_m * np.sin(2 * np.pi * self.cfg.breath_hz * t)
        sway = 0.001 * np.sin(2 * np.pi * 0.03 * t)
        return self._pos + 2 * (breath + sway)  # reflection doubles the displacement

    def frames(self, scenario: List[Tuple[str, float]], start: float = 0.0) -> Iterator[Tuple[CSIFrame, str]]:
        """Yield (frame, ground_truth_state) for the whole scenario."""
        cfg = self.cfg
        t = start
        noise_sigma = np.mean(np.abs(self.static_h)) * 10 ** (-cfg.snr_db / 20)
        for state, duration in scenario:
            end = t + duration
            while t < end:
                dt = self.rng.uniform(0.7, 1.3) / cfg.rate_hz  # irregular packet timing
                h = self.static_h.copy()
                length = self._person_len(state, t, dt)
                if length is not None:
                    h = h + cfg.person_gain * self._paths(np.array([length]), np.array([1.0 + 0j]))
                noise = noise_sigma * (self.rng.normal(size=N_SUB) + 1j * self.rng.normal(size=N_SUB)) / np.sqrt(2)
                agc = 1.0 + cfg.agc_jitter * self.rng.normal()
                h = (h + noise) * agc * self.scale
                h = np.round(h.real) + 1j * np.round(h.imag)
                h[~self.mask] = 0
                rssi = int(-45 + 20 * np.log10(agc) + self.rng.normal(0, 1))
                yield CSIFrame(timestamp=t, rssi=rssi, csi=h), state
                t += dt


def ground_truth_present(state: str) -> bool:
    return state != "empty"
