# ESP32 firmware

No custom firmware. Use Espressif's official examples from
[espressif/esp-csi](https://github.com/espressif/esp-csi), which print one
`CSI_DATA,...` line per received packet over USB serial. `wifisense` parses
that output directly.

## Option A: two boards (recommended)

| Board | Example | Role |
|---|---|---|
| TX | `examples/get-started/csi_send` | Broadcasts packets at ~100 Hz |
| RX | `examples/get-started/csi_recv` | Receives them, prints CSI to USB serial |

This gives a steady, known packet rate and a fixed TX to RX link across the room.

## Option B: one board and your router

`examples/get-started/csi_recv_router` connects to your Wi-Fi and pings the
router to generate traffic. It's cheaper, but the router is usually not where
you want the link to be, and the packet rate depends on the router.

## Flashing

```bash
# ESP-IDF v5.x installed and exported (. $IDF_PATH/export.sh)
git clone --recursive https://github.com/espressif/esp-csi
cd esp-csi/examples/get-started/csi_send
idf.py set-target esp32s3        # or esp32, esp32c3, esp32c6 ...
idf.py flash -p /dev/ttyUSB0
cd ../csi_recv
idf.py set-target esp32s3
idf.py flash monitor -p /dev/ttyUSB1   # you should see CSI_DATA lines scrolling
```

Close `idf.py monitor` before running `wifisense`, because only one program can hold the port.

**Baud rate:** the examples configure a high console baud rate (921600 at the
time of writing). If `wifisense` sees nothing or garbage, check
`CONFIG_ESP_CONSOLE_UART_BAUDRATE` in the example's `sdkconfig.defaults` and
pass it explicitly: `--source serial:/dev/ttyUSB1@<baud>`.

## Placement

* Put TX and RX on opposite sides of the room, 3-5 m apart, about 1 m high
  (seated chest height).
* Areas between and near the line joining them are detected best. Corners
  behind the RX are weak.
* Don't move the boards after calibrating. Any change to the static
  environment (moved furniture, an open door) means you need to recalibrate.
