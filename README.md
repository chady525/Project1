# wifisense: Wi-Fi presence detection

Detects whether someone is in a room from how their body disturbs Wi-Fi
signals. It reads **Channel State Information (CSI)**, meaning per-subcarrier
amplitude and phase, from a pair of ESP32 boards.

This is phase 1 of a larger goal (activity, posture, vital signs). It
covers **presence only**, including a person sitting perfectly still.

## Why ESP32 and not my router or laptop

Presence sensing needs CSI. Consumer routers, phones, macOS and Windows don't
expose it. They only give you RSSI, a single number per packet that is too
coarse to see a still person reliably. The cheap, well-supported way to get CSI
is an ESP32 running Espressif's `esp-csi` firmware (about $5-10 per board).
The alternatives are old Intel 5300 or Atheros cards with patched drivers, or
a Raspberry Pi with Nexmon.

## How it works

```
ESP32 TX ~~~ room ~~~ ESP32 RX --USB--> wifisense
                                          |- motion score    (amplitude variance, 2 s)
                                          |- breathing score (0.1-0.7 Hz spectral peak, 20 s)
                                          '- thresholds from empty-room calibration
                                                -> PRESENT / EMPTY (+ webhook)
```

* **Motion** catches people walking or fidgeting within about 0.5 s.
* **Breathing** catches a person sitting or lying still. Chest movement of a
  few mm rotates the phase of the body-reflected path and leaves a periodic
  ripple in subcarrier amplitudes. PCA across subcarriers pulls that ripple out
  of the noise. A breathing-rate estimate comes out as a by-product.
* Per-packet gain normalisation removes AGC jumps. Irregular packet timing is
  handled by resampling to a 10 Hz grid.
* The state goes back to EMPTY after `--hold` seconds (default 10) with no
  evidence. Because the breathing window is 20 s, expect about 15-30 s to
  report EMPTY after someone leaves.

## Quick start (no hardware)

```bash
pip install -e '.[dev]'
wifisense calibrate --source sim:empty@1 -o baseline.json
wifisense run --source sim:demo --baseline baseline.json
# t=   40.4s  PRESENT  (motion)
# t=  139.4s  EMPTY    (no evidence)
pytest
```

The `demo` scenario is: empty 40 s, walk in, sit still 60 s, walk out, empty.

## With hardware

1. Flash two ESP32s, as described in [firmware/README.md](firmware/README.md).
2. Calibrate with the **room empty**. Leave the room, and leave your phone in
   there too:
   ```bash
   wifisense calibrate --source serial:/dev/ttyUSB1 --seconds 120 -o baseline.json
   ```
3. Run:
   ```bash
   wifisense run --source serial:/dev/ttyUSB1 --baseline baseline.json
   wifisense run --source serial:/dev/ttyUSB1 --json            # every reading
   wifisense run --source serial:/dev/ttyUSB1 --webhook http://homeassistant.local:8123/api/webhook/room_presence
   ```
4. Capture data for offline tuning:
   ```bash
   wifisense record --source serial:/dev/ttyUSB1 --seconds 300 -o sitting.csi
   wifisense run --source file:sitting.csi --baseline baseline.json --json
   ```

## Zone occupancy from UniFi access points (no extra hardware)

If you already run UniFi access points, `wifisense zones` asks the controller
which access point every Wi-Fi device is connected to. It then reports how many
devices are in each access point's area, and optionally who.

**This tracks phones, not people.** It can't see anyone without a device on
the Wi-Fi, a phone left on a desk counts as a person, and locked phones
(especially iPhones) go quiet for minutes. Resolution is one access point's
coverage area, usually several rooms, never a single room unless each room has
its own access point. Phones on mobile data are invisible to it.

```bash
export UNIFI_URL=https://192.168.1.1     # Dream Machine / UDR / Cloud Key; classic controllers use :8443
export UNIFI_USER=presence               # a LOCAL read-only account, not your UI.com login
export UNIFI_PASS=...
wifisense aps                            # list access points with client counts
wifisense zones                          # print occupancy per zone whenever it changes
wifisense zones --config zones.json --json --webhook http://homeassistant.local:8123/api/webhook/zones
```

Zones default to the access point names set in the UniFi app, so naming them
by area ("F1 North wing") is often all the setup you need.
`zones.example.json` shows how to rename zones, map device MACs to people,
and ignore fixed devices (printers, TVs) that would otherwise inflate counts.
A device has to show up on a new access point on two polls in a row before it
counts as having moved, which stops it flickering between two zones near
doorways.

**Before naming people:** tracking identifiable staff or visitors by their
phones is personal-data processing in most jurisdictions. Get consent or give
clear notice, or stick to anonymous counts (no `people` map).

## Known limits

* **All validation so far is on synthetic data.** The simulator has no
  pets, fans, HVAC, neighbours' Wi-Fi or people in adjacent rooms, and all of
  those will cause false positives in real rooms. Expect to tune the
  calibration margins (`calibrate()` in `wifisense/detector.py`) against real
  recordings.
* Fans, moving curtains and pets register as "someone is here".
* Through-wall leakage is possible. Someone walking just outside the room can
  trip the motion score.
* One TX/RX pair only tells you *someone* is present, not how many or where.

## Roadmap

1. **Presence** (this). Validate on real captures, then tune.
2. Activity and posture (walking / sitting / lying / fall) using a labelled
   dataset recorded with `wifisense record` and a small classifier.
3. Breathing rate, which is already estimated here and needs validation
   against ground truth.
4. Heart rate. Research-grade with commodity CSI, unreliable outside lab
   conditions. Treat it as an experiment, not a feature.
