import time
from dataclasses import dataclass
from typing import Dict, Callable, List, Tuple

from .config import DetectorConfig, BandConfig, AlertConfig


@dataclass
class BandState:
    last_power_db: float = float("-inf")
    smoothed_power_db: float = float("-inf")
    last_alert_time: float = 0.0


def band_freq_range(band: BandConfig) -> Tuple[float, float]:
    half = band.width_hz / 2.0
    return band.center_hz - half, band.center_hz + half


def detect_on_sweep_stream(
    cfg: DetectorConfig,
    alert_cfg: AlertConfig,
    sweep_iter,
    on_alert: Callable[[BandConfig, float], None],
):
    """
    sweep_iter yields frames from iter_sweep_frames.
    For each frame, compute per-band power and trigger alerts.
    """
    band_states: Dict[str, BandState] = {
        band.name: BandState() for band in cfg.bands
    }

    band_ranges = {
        band.name: band_freq_range(band) for band in cfg.bands
    }

    for frame in sweep_iter:
        low_hz = frame["low_hz"]
        bin_width = frame["bin_width_hz"]
        powers = frame["powers_dbm"]

        for band in cfg.bands:
            state = band_states[band.name]
            low, high = band_ranges[band.name]

            band_powers: List[float] = []

            for i, p in enumerate(powers):
                bin_freq = low_hz + i * bin_width
                if low <= bin_freq <= high:
                    band_powers.append(p)

            if not band_powers:
                continue

            inst_power = max(band_powers)

            if state.smoothed_power_db == float("-inf"):
                state.smoothed_power_db = inst_power
            else:
                alpha = cfg.smoothing_factor
                state.smoothed_power_db = (
                    alpha * state.smoothed_power_db
                    + (1 - alpha) * inst_power
                )

            state.last_power_db = inst_power

            if state.smoothed_power_db > band.threshold_db:
                now = time.time()
                if now - state.last_alert_time > cfg.min_alert_interval_s:
                    state.last_alert_time = now
                    if alert_cfg.log_to_console:
                        print(
                            f"[ALERT] {band.name}: "
                            f"smoothed={state.smoothed_power_db:.1f} dB, "
                            f"threshold={band.threshold_db:.1f} dB"
                        )
                    on_alert(band, state.smoothed_power_db)
