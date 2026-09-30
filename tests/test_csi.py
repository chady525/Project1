import numpy as np

from wifisense.csi import CSIFrame, format_line, parse_line


def test_parses_esp_csi_line():
    line = 'CSI_DATA,12,aa:bb:cc:dd:ee:ff,-52,11,1,7,1,0,1,0,0,0,0,-96,0,6,0,1234567,0,128,0,4,0,"[3,4,0,0,-6,8]"'
    f = parse_line(line, timestamp=5.0)
    assert f.rssi == -52
    assert f.timestamp == 5.0
    # pairs are (imag, real)
    np.testing.assert_allclose(f.csi, [4 + 3j, 0, 8 - 6j])
    np.testing.assert_allclose(f.amplitude, [5, 0, 10])


def test_ignores_boot_noise_and_malformed():
    assert parse_line("I (312) wifi: mode : sta") is None
    assert parse_line("CSI_DATA,1,mac,-40,[1,2,3]") is None  # odd length
    assert parse_line("CSI_DATA,1,mac,-40,no array") is None


def test_roundtrip_recorded_format():
    f = CSIFrame(timestamp=1727700000.25, rssi=-47, csi=np.array([1 + 2j, -3 - 4j, 0]))
    g = parse_line(format_line(f, seq=9))
    assert g.timestamp == f.timestamp and g.rssi == -47
    np.testing.assert_allclose(g.csi, f.csi)
