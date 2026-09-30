"""Wi-Fi CSI presence detection."""

from .csi import CSIFrame, parse_line, format_line
from .detector import Baseline, PresenceDetector, Reading

__all__ = ["CSIFrame", "parse_line", "format_line", "Baseline", "PresenceDetector", "Reading"]
__version__ = "0.1.0"
