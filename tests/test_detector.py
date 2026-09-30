import numpy as np
import pytest

from wifisense.detector import PresenceDetector, calibrate
from wifisense.simulator import SCENARIOS, SimConfig, Simulator


@pytest.fixture(scope="module")
def baseline():
    return calibrate(f for f, _ in Simulator(SimConfig(seed=1)).frames(SCENARIOS["empty"]))


def run(baseline, scenario, **cfg):
    det = PresenceDetector(baseline)
    out = []
    for frame, truth in Simulator(SimConfig(**cfg)).frames(scenario):
        r = det.update(frame)
        if r:
            out.append((r, truth))
    return out


def test_empty_room_has_no_false_positives(baseline):
    readings = run(baseline, SCENARIOS["empty"], seed=7)
    assert not any(r.present for r, _ in readings)


def test_walking_is_detected_immediately(baseline):
    readings = run(baseline, [("empty", 30), ("walking", 20)], seed=3)
    walking = [r for r, s in readings if s == "walking" and r.timestamp > 31]
    assert all(r.present and r.motion_hit for r in walking)


def test_still_person_detected_by_breathing_alone(baseline):
    # Weak reflection, shallow breathing: motion should not fire, breathing must.
    readings = run(baseline, [("empty", 30), ("sitting", 60)], seed=4, breath_amp_m=0.002, person_gain=0.2)
    settled = [r for r, s in readings if s == "sitting" and r.timestamp > 30 + 20]
    assert settled
    assert all(r.present for r in settled)
    assert np.mean([r.breath_hit for r in settled]) > 0.9
    assert not any(r.motion_hit for r in settled)
    bpm = [r.breath_bpm for r in settled if r.breath_bpm]
    assert abs(np.median(bpm) - 15) <= 3  # simulated 0.25 Hz


def test_demo_accuracy_outside_transitions(baseline):
    readings = run(baseline, SCENARIOS["demo"], seed=2)
    # Skip windows right after each transition (detector latency / hold time).
    edges = np.cumsum([d for _, d in SCENARIOS["demo"]])
    def near_edge(t):
        return any(0 <= t - e < 35 for e in edges[:-1])
    scored = [(r.present, s != "empty") for r, s in readings if not near_edge(r.timestamp)]
    acc = np.mean([p == g for p, g in scored])
    assert acc > 0.97


def test_rejects_short_calibration():
    with pytest.raises(ValueError):
        calibrate(f for f, _ in Simulator(SimConfig(seed=1)).frames([("empty", 15)]))
