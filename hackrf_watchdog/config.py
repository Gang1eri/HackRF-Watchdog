from dataclasses import dataclass, field
from typing import List


@dataclass
class BandConfig:
    name: str
    center_hz: float
    width_hz: float
    threshold_db: float


@dataclass
class SweepConfig:
    # Sweep range is stored in Hz
    start_hz: float = 700e6
    stop_hz: float = 1300e6
    bin_width_hz: float = 250e3   # hackrf_sweep bin width in Hz


@dataclass
class AlertConfig:
    log_to_console: bool = True
    play_sound: bool = False      # reserved for future use


@dataclass
class DetectorConfig:
    sweep: SweepConfig
    bands: List[BandConfig] = field(default_factory=list)
    smoothing_factor: float = 0.8
    min_alert_interval_s: float = 0.5
