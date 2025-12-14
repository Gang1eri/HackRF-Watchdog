import sys
import time
import statistics
import subprocess
import socket
import os
import shutil
from typing import List, Dict, Any, Optional

from xml.sax.saxutils import escape
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtMultimedia import QSoundEffect

from hackrf_watchdog.sweep_backend import iter_sweep_frames, SweepBackendError


# ---------------------------------------------------------------------------
# ATAK / CoT integration helpers
# ---------------------------------------------------------------------------

# Multicast group ATAK is listening on (matches your "Watchdog bridge" input)
COT_HOST = "239.2.3.1"
COT_PORT = 6969

# Approximate location of your RF station (edit these for your setup)
STATION_LAT = 46.84878
STATION_LON = -114.03891


def _build_cot(lat: float, lon: float, uid: str, callsign: str, remarks: str = "") -> bytes:
    """Build a basic CoT event XML that ATAK understands."""
    now = time.gmtime()
    t = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", now)
    stale = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 300))

    lat_str = f"{lat:.6f}"
    lon_str = f"{lon:.6f}"

    callsign_esc = escape(callsign)
    uid_esc = escape(uid)
    remarks_esc = escape(remarks) if remarks else ""

    cot = f"""<event version="2.0"
    uid="{uid_esc}"
    type="a-f-G-U-C"
    how="m-g"
    time="{t}"
    start="{t}"
    stale="{stale}">
  <point lat="{lat_str}" lon="{lon_str}" hae="0" ce="9999999" le="9999999" />
  <detail>
    <contact callsign="{callsign_esc}" />
    <__group name="Cyan" role="Team" />
    <remarks>{remarks_esc}</remarks>
  </detail>
</event>"""
    return cot.encode("utf-8")


def send_cot_for_detection(det: Dict[str, Any], noise_floor: Optional[float] = None) -> None:
    """
    Build and send a CoT marker for a single detection dict.

    Expected keys (some optional):
      freq_mhz, power_dbm, power_dbm_raw, band, cal_offset_db, freq_ppm, timestamp
    """
    try:
        freq_mhz = float(det.get("freq_mhz", 0.0))
    except (TypeError, ValueError):
        return

    if freq_mhz <= 0:
        return

    power_dbm = float(det.get("power_dbm", 0.0))
    band = det.get("band", "")

    callsign = f"RF-{freq_mhz:.3f}MHz"
    uid = callsign

    parts = [f"Freq: {freq_mhz:.3f} MHz", f"Power: {power_dbm:.1f} dB"]

    # Optional calibration detail
    cal_offset = det.get("cal_offset_db", None)
    raw_power = det.get("power_dbm_raw", None)
    if cal_offset is not None:
        try:
            cal_offset_f = float(cal_offset)
            if abs(cal_offset_f) >= 0.05:
                parts.append(f"Cal offset: {cal_offset_f:+.1f} dB")
        except Exception:
            pass
    if raw_power is not None:
        try:
            raw_power_f = float(raw_power)
            if abs(raw_power_f - power_dbm) >= 0.05:
                parts.append(f"Raw: {raw_power_f:.1f} dB")
        except Exception:
            pass

    ppm = det.get("freq_ppm", None)
    if ppm is not None:
        try:
            ppm_f = float(ppm)
            if abs(ppm_f) >= 0.05:
                parts.append(f"PPM: {ppm_f:+.1f}")
        except Exception:
            pass

    if noise_floor is not None:
        above = power_dbm - noise_floor
        parts.append(f"Noise floor: {noise_floor:.1f} dB")
        parts.append(f"Above noise: {above:.1f} dB")
    if band:
        parts.append(f"Band: {band}")

    remarks = " | ".join(parts)
    payload = _build_cot(STATION_LAT, STATION_LON, uid, callsign, remarks)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, (COT_HOST, COT_PORT))
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Bias-T / antenna power control helper
# ---------------------------------------------------------------------------

def set_bias_tee(enable: bool, log_fn, serial: Optional[str] = None) -> bool:
    """
    Best-effort Bias-T control via hackrf_biast (preferred).
    Returns True if command ran successfully, False otherwise.

    If hackrf_biast isn't found, returns False (caller can still rely on -p in hackrf_sweep).
    """
    exe = shutil.which("hackrf_biast") or shutil.which("hackrf_biast.exe")
    if not exe:
        return False

    mode = "1" if enable else "0"
    cmd = [exe, "-b", mode, "-r", ("on" if enable else "off")]
    if serial:
        cmd += ["-d", str(serial)]

    try:
        # keep short so GUI doesn't hang
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=2)
        log_fn(f"Bias-T set to: {'ON' if enable else 'OFF'} (via hackrf_biast)")
        return True
    except Exception as e:
        log_fn(f"Bias-T command failed (hackrf_biast): {e}")
        return False


# ---------------------------------------------------------------------------
# HackRF device detection
# ---------------------------------------------------------------------------

def list_hackrf_devices() -> List[Dict[str, str]]:
    """
    Run `hackrf_info` and parse connected HackRFs.

    Returns a list of dicts like:
      {"index": "0", "serial": "436c63dc2d7d7563"}
    """
    devices: List[Dict[str, str]] = []

    try:
        result = subprocess.run(
            ["hackrf_info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except Exception:
        return devices

    index = -1
    for line in result.stdout.splitlines():
        raw = line.strip()
        low = raw.lower()

        if low.startswith("found hackrf"):
            index += 1

        if "serial" in low and ":" in raw:
            _, val = raw.split(":", 1)
            serial = val.strip()
            if serial:
                devices.append({"index": str(index), "serial": serial})

    return devices


# ---------------------------------------------------------------------------
# Sweep worker: noise floor + detection only (no spectrum/waterfall)
# ---------------------------------------------------------------------------

class SweepWorker(QtCore.QObject):
    log_message = QtCore.pyqtSignal(str)
    noise_floor_updated = QtCore.pyqtSignal(float)
    detections_found = QtCore.pyqtSignal(list)  # list of detection dicts
    finished = QtCore.pyqtSignal()

    def __init__(
        self,
        bands: List[Dict[str, Any]],
        bin_width_hz: int,
        threshold_db: float,
        use_local_noise_floor: bool,
        only_above_threshold: bool,
        min_hold_time_s: float,
        interval_ms: int,
        device_arg: Optional[str] = None,
        antenna_power: bool = False,
        cal_gain_db: float = 0.0,
        cal_loss_db: float = 0.0,
        freq_ppm: float = 0.0,
        parent=None,
    ):
        super().__init__(parent)
        self.bands = bands
        self.bin_width_hz = bin_width_hz
        self.threshold_db = float(threshold_db)
        self.use_local_noise_floor = bool(use_local_noise_floor)
        self.only_above_threshold = bool(only_above_threshold)
        self.min_hold_time_s = float(min_hold_time_s)
        self.interval_ms = int(interval_ms)
        self.device_arg = device_arg

        self.antenna_power = bool(antenna_power)
        self.cal_gain_db = float(cal_gain_db)
        self.cal_loss_db = float(cal_loss_db)
        self.freq_ppm = float(freq_ppm)

        self._running = True
        self._noise_floor = None
        # freq key -> {"first_seen": float|None, "last_seen": float|None, "above": bool}
        self._hold_state: Dict[float, Dict[str, Any]] = {}

    @QtCore.pyqtSlot()
    def run(self):
        try:
            while self._running:
                cycle_start = time.time()
                any_band = False

                for band in self.bands:
                    if not self._running:
                        break
                    if not band.get("enabled"):
                        continue

                    any_band = True
                    start_hz = band["start_hz"]
                    stop_hz = band["stop_hz"]

                    try:
                        extra_args = ["-1"]  # one sweep across the band

                        if self.device_arg:
                            extra_args += ["-d", self.device_arg]

                        # Compatibility path: hackrf_sweep itself can enable antenna power
                        # (even if hackrf_biast isn't present).
                        if self.antenna_power:
                            extra_args += ["-p", "1"]

                        for frame in iter_sweep_frames(
                            start_hz,
                            stop_hz,
                            self.bin_width_hz,
                            extra_args=extra_args,
                        ):
                            if not self._running:
                                break
                            self._handle_frame(band, frame)

                    except SweepBackendError as e:
                        self.log_message.emit(f"Error from hackrf_sweep: {e}")
                        time.sleep(1.0)
                        if not self._running:
                            break

                if not self._running:
                    break

                if not any_band:
                    self.log_message.emit("No bands enabled; worker sleeping.")
                    time.sleep(1.0)

                if self.interval_ms > 0:
                    elapsed_ms = (time.time() - cycle_start) * 1000.0
                    remaining = self.interval_ms - elapsed_ms
                    if remaining > 0:
                        time.sleep(remaining / 1000.0)
        finally:
            self.finished.emit()

    def stop(self):
        self._running = False

    def _net_cal_offset_db(self) -> float:
        return float(self.cal_gain_db) - float(self.cal_loss_db)

    def _freq_factor(self) -> float:
        # Correction applied to displayed/detected frequency:
        # f_corrected = f_raw * (1 + ppm/1e6)
        return 1.0 + (float(self.freq_ppm) / 1e6)

    def _handle_frame(self, band: Dict[str, Any], frame: Dict[str, Any]) -> None:
        powers_raw = frame["powers_dbm"]
        if not powers_raw:
            return

        cal_offset = self._net_cal_offset_db()
        powers = [p + cal_offset for p in powers_raw]

        # ---- Noise floor estimate (calibrated) ----
        sorted_p = sorted(powers)
        if len(sorted_p) > 10:
            cutoff = int(len(sorted_p) * 0.8)
            noise_candidates = sorted_p[:cutoff]
        else:
            noise_candidates = sorted_p
        median_noise = statistics.median(noise_candidates)

        if self._noise_floor is None:
            self._noise_floor = median_noise
        else:
            alpha = 0.1
            self._noise_floor = (1 - alpha) * self._noise_floor + alpha * median_noise

        self.noise_floor_updated.emit(self._noise_floor)

        # Effective threshold (calibrated)
        if self.use_local_noise_floor:
            abs_threshold = self._noise_floor + float(self.threshold_db)
        else:
            abs_threshold = float(self.threshold_db)

        low_hz = float(frame["low_hz"])
        bin_w = float(frame["bin_width_hz"])
        f_factor = self._freq_factor()

        detections: List[Dict[str, Any]] = []
        max_power = None
        max_freq_mhz = None
        max_power_raw = None
        max_freq_mhz_raw = None

        now = time.time()
        hold = float(self.min_hold_time_s)

        n_bins = len(powers)
        for idx in range(n_bins):
            p_cal = powers[idx]
            p_raw = powers_raw[idx]

            center_hz_raw = low_hz + (idx + 0.5) * bin_w
            center_hz = center_hz_raw * f_factor

            freq_mhz_raw = center_hz_raw / 1e6
            freq_mhz = center_hz / 1e6

            key = round(freq_mhz, 6)
            st = self._hold_state.get(key)

            if p_cal >= abs_threshold:
                if st is None or not st.get("above", False):
                    st = {"first_seen": now, "last_seen": now, "above": True}
                    self._hold_state[key] = st
                else:
                    st["last_seen"] = now
                    st["above"] = True

                dwell = 0.0 if st["first_seen"] is None else now - st["first_seen"]

                if hold <= 0 or dwell >= hold:
                    detections.append(
                        {
                            "freq_mhz": freq_mhz,
                            "freq_mhz_raw": freq_mhz_raw,
                            "power_dbm": p_cal,
                            "power_dbm_raw": p_raw,
                            "cal_offset_db": cal_offset,
                            "freq_ppm": float(self.freq_ppm),
                            "timestamp": now,
                            "band": band.get("name", ""),
                        }
                    )
            else:
                if st is not None and st.get("above", False):
                    st["above"] = False
                    st["first_seen"] = None
                    st["last_seen"] = now

            # Track max (calibrated)
            if max_power is None or p_cal > max_power:
                max_power = p_cal
                max_freq_mhz = freq_mhz
                max_power_raw = p_raw
                max_freq_mhz_raw = freq_mhz_raw

        # Cleanup stale hold-state entries
        cleanup_limit = max(hold * 2.0, 10.0)
        stale_keys = []
        for k, st in self._hold_state.items():
            last_seen = st.get("last_seen")
            if last_seen is not None and (now - last_seen) > cleanup_limit:
                stale_keys.append(k)
        for k in stale_keys:
            del self._hold_state[k]

        # Log max per sweep (not necessarily a "detection")
        span_txt = f"{band['start_mhz']:.3f}-{band['stop_mhz']:.3f} MHz"
        if max_power is not None and max_freq_mhz is not None:
            line = f"Max: {max_power:.1f} dB at {max_freq_mhz:.6f} MHz (span {span_txt})"

            extras = []
            if abs(cal_offset) >= 0.05:
                extras.append(f"cal {cal_offset:+.1f} dB")
            if abs(float(self.freq_ppm)) >= 0.05:
                extras.append(f"ppm {float(self.freq_ppm):+.1f}")
            if max_power_raw is not None and abs(cal_offset) >= 0.05:
                extras.append(f"raw {max_power_raw:.1f} dB")
            if max_freq_mhz_raw is not None and abs(float(self.freq_ppm)) >= 0.05:
                extras.append(f"rawf {max_freq_mhz_raw:.6f} MHz")

            if extras:
                line += " [" + ", ".join(extras) + "]"

            if self.only_above_threshold:
                if max_power >= abs_threshold:
                    self.log_message.emit(line)
            else:
                self.log_message.emit(line)

        if detections:
            self.detections_found.emit(detections)


# ---------------------------------------------------------------------------
# Main Window (no spectrum/waterfall)
# ---------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HackRF Watchdog")

        self.worker_thread: Optional[QtCore.QThread] = None
        self.worker: Optional[SweepWorker] = None

        # freq -> detection dict
        self.detections: Dict[float, Dict[str, Any]] = {}

        self.current_noise_floor: Optional[float] = None
        self.sound_effects: Dict[str, QSoundEffect] = {}

        self.current_bin_width: int = 250_000

        # Bias-T state tracking (best-effort cleanup)
        self.bias_tee_requested: bool = False
        self.bias_tee_engaged: bool = False

        self._build_ui()
        self._create_timers()

        self.refresh_device_list()

    # ---------------- UI ----------------

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        main_layout = QtWidgets.QVBoxLayout(central)

        # Top bar
        top_bar = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.status_label = QtWidgets.QLabel("Idle")

        top_bar.addWidget(self.start_btn)
        top_bar.addWidget(self.stop_btn)
        top_bar.addWidget(self.status_label)
        top_bar.addStretch(1)

        self.clear_log_btn = QtWidgets.QPushButton("Clear log")
        top_bar.addWidget(self.clear_log_btn)

        self.dark_mode_checkbox = QtWidgets.QCheckBox("Dark mode")
        top_bar.addWidget(self.dark_mode_checkbox)

        main_layout.addLayout(top_bar)

        # Detection settings group
        det_group = QtWidgets.QGroupBox("Detection settings")
        det_layout = QtWidgets.QGridLayout(det_group)

        row = 0
        det_layout.addWidget(QtWidgets.QLabel("Threshold (dB)"), row, 0)
        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setRange(0.0, 120.0)
        self.threshold_spin.setSingleStep(0.5)
        self.threshold_spin.setValue(3.0)
        self.threshold_spin.setKeyboardTracking(True)
        self.threshold_spin.setAccelerated(True)
        self.threshold_spin.setToolTip(
            "If 'Use local noise floor' is checked, this is dB ABOVE the\n"
            "estimated noise floor.\n"
            "If unchecked, this is an ABSOLUTE dB level.\n\n"
            "Note: Power calibration (gain/loss) is applied before thresholding."
        )
        det_layout.addWidget(self.threshold_spin, row, 1)

        self.only_above_threshold_cb = QtWidgets.QCheckBox(
            "Only show detections above threshold"
        )
        self.only_above_threshold_cb.setChecked(True)
        det_layout.addWidget(self.only_above_threshold_cb, row, 2, 1, 2)

        row += 1
        self.use_noise_floor_cb = QtWidgets.QCheckBox("Use local noise floor")
        self.use_noise_floor_cb.setChecked(True)
        det_layout.addWidget(self.use_noise_floor_cb, row, 0, 1, 2)

        self.noise_floor_label = QtWidgets.QLabel("Noise floor: --.- dB")
        det_layout.addWidget(self.noise_floor_label, row, 2, 1, 2)

        row += 1
        self.eff_threshold_label = QtWidgets.QLabel("Effective threshold: --.- dB")
        det_layout.addWidget(self.eff_threshold_label, row, 0, 1, 4)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Persistence / hold time (s)"), row, 0)
        self.persistence_spin = QtWidgets.QDoubleSpinBox()
        self.persistence_spin.setDecimals(1)
        self.persistence_spin.setRange(0.0, 3600.0)
        self.persistence_spin.setSingleStep(0.1)
        self.persistence_spin.setValue(3.0)
        self.persistence_spin.setToolTip(
            "Minimum time a signal must stay above threshold before being\n"
            "treated as a detection.\n"
            "0 = every above-threshold blip counts immediately."
        )
        det_layout.addWidget(self.persistence_spin, row, 1)

        det_layout.addWidget(QtWidgets.QLabel("Interval (ms)"), row, 2)
        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(0, 60000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setValue(0)
        self.interval_spin.setToolTip(
            "Extra delay between full sweep cycles.\n"
            "0 = run sweeps back-to-back."
        )
        det_layout.addWidget(self.interval_spin, row, 3)

        row += 1
        self.beep_checkbox = QtWidgets.QCheckBox("Beep on detection")
        self.beep_checkbox.setChecked(False)
        self.beep_checkbox.setToolTip(
            "Play a short sound whenever detections are reported."
        )
        det_layout.addWidget(self.beep_checkbox, row, 0, 1, 4)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Alarm sound"), row, 0)
        self.beep_sound_combo = QtWidgets.QComboBox()
        self.beep_sound_combo.addItem("System beep (default)", userData="system")
        self.beep_sound_combo.addItem("Soft ding", userData="soft_ding")
        self.beep_sound_combo.addItem("Short chirp", userData="short_chirp")
        self.beep_sound_combo.addItem("Alarm", userData="alarm")
        det_layout.addWidget(self.beep_sound_combo, row, 1, 1, 3)

        # --- Power calibration controls ---
        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Antenna/LNA gain (dB)"), row, 0)
        self.cal_gain_spin = QtWidgets.QDoubleSpinBox()
        self.cal_gain_spin.setDecimals(1)
        self.cal_gain_spin.setRange(-200.0, 200.0)
        self.cal_gain_spin.setSingleStep(0.5)
        self.cal_gain_spin.setValue(0.0)
        self.cal_gain_spin.setToolTip(
            "Calibration gain to ADD (e.g., LNA gain if you want to represent\n"
            "an estimated absolute field/chain level).\n\n"
            "Net offset = gain − loss\n"
            "cal_dB = raw_dB + (gain − loss)"
        )
        det_layout.addWidget(self.cal_gain_spin, row, 1)

        det_layout.addWidget(QtWidgets.QLabel("Feedline loss (dB)"), row, 2)
        self.cal_loss_spin = QtWidgets.QDoubleSpinBox()
        self.cal_loss_spin.setDecimals(1)
        self.cal_loss_spin.setRange(0.0, 200.0)
        self.cal_loss_spin.setSingleStep(0.5)
        self.cal_loss_spin.setValue(0.0)
        self.cal_loss_spin.setToolTip(
            "Estimated loss to SUBTRACT (coax/filters/attenuators).\n\n"
            "Net offset = gain − loss\n"
            "cal_dB = raw_dB + (gain − loss)"
        )
        det_layout.addWidget(self.cal_loss_spin, row, 3)

        row += 1
        self.cal_net_label = QtWidgets.QLabel("Net power offset: +0.0 dB (gain − loss)")
        self.cal_net_label.setToolTip(
            "Net offset = gain − loss\n"
            "Applied to all power readings:\n"
            "cal_dB = raw_dB + (gain − loss)"
        )
        det_layout.addWidget(self.cal_net_label, row, 0, 1, 4)

        # --- Frequency ppm correction ---
        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Freq correction (ppm)"), row, 0)
        self.ppm_spin = QtWidgets.QDoubleSpinBox()
        self.ppm_spin.setDecimals(1)
        self.ppm_spin.setRange(-2000.0, 2000.0)
        self.ppm_spin.setSingleStep(0.5)
        self.ppm_spin.setValue(0.0)
        self.ppm_spin.setToolTip(
            "Corrects displayed/detected frequency using:\n"
            "f_corrected = f_raw * (1 + ppm/1e6)\n\n"
            "If you get the sign wrong, just flip it (+/-) until known signals line up."
        )
        det_layout.addWidget(self.ppm_spin, row, 1)

        main_layout.addWidget(det_group)

        # Device settings group
        device_group = QtWidgets.QGroupBox("Device")
        dev_layout = QtWidgets.QGridLayout(device_group)

        dev_layout.addWidget(QtWidgets.QLabel("Type:"), 0, 0)
        self.device_type_combo = QtWidgets.QComboBox()
        self.device_type_combo.addItems(["HackRF (hackrf_sweep)"])
        dev_layout.addWidget(self.device_type_combo, 0, 1)

        dev_layout.addWidget(QtWidgets.QLabel("HackRF:"), 0, 2)
        self.device_combo = QtWidgets.QComboBox()
        dev_layout.addWidget(self.device_combo, 0, 3)

        self.refresh_devices_btn = QtWidgets.QPushButton("Refresh")
        dev_layout.addWidget(self.refresh_devices_btn, 0, 4)

        # Bias-T toggle
        self.bias_tee_checkbox = QtWidgets.QCheckBox("Bias-T / antenna power")
        self.bias_tee_checkbox.setChecked(False)
        self.bias_tee_checkbox.setToolTip(
            "Enables HackRF antenna port power (Bias-T) for active antennas/LNAs.\n"
            "Best-effort control: uses hackrf_biast if available; also passes -p 1 to hackrf_sweep."
        )
        dev_layout.addWidget(self.bias_tee_checkbox, 1, 0, 1, 3)

        dev_layout.setColumnStretch(5, 1)
        main_layout.addWidget(device_group)

        # Band configuration group
        band_group = QtWidgets.QGroupBox("Band configurations")
        bg_layout = QtWidgets.QGridLayout(band_group)

        row = 0
        bg_layout.addWidget(QtWidgets.QLabel("Band"), row, 0)
        bg_layout.addWidget(QtWidgets.QLabel("Enabled"), row, 1)
        bg_layout.addWidget(QtWidgets.QLabel("Start (MHz)"), row, 2)
        bg_layout.addWidget(QtWidgets.QLabel("Stop (MHz)"), row, 3)

        row += 1
        self.bandA_label = QtWidgets.QLabel("Band A")
        self.bandA_enable = QtWidgets.QCheckBox()
        self.bandA_enable.setChecked(True)
        self.bandA_start = QtWidgets.QDoubleSpinBox()
        self.bandA_start.setDecimals(3)
        self.bandA_start.setRange(1.0, 6000.0)
        self.bandA_start.setValue(900.0)
        self.bandA_stop = QtWidgets.QDoubleSpinBox()
        self.bandA_stop.setDecimals(3)
        self.bandA_stop.setRange(1.0, 6000.0)
        self.bandA_stop.setValue(930.0)
        bg_layout.addWidget(self.bandA_label, row, 0)
        bg_layout.addWidget(self.bandA_enable, row, 1)
        bg_layout.addWidget(self.bandA_start, row, 2)
        bg_layout.addWidget(self.bandA_stop, row, 3)

        row += 1
        self.bandB_label = QtWidgets.QLabel("Band B")
        self.bandB_enable = QtWidgets.QCheckBox()
        self.bandB_enable.setChecked(True)
        self.bandB_start = QtWidgets.QDoubleSpinBox()
        self.bandB_start.setDecimals(3)
        self.bandB_start.setRange(1.0, 6000.0)
        self.bandB_start.setValue(144.0)
        self.bandB_stop = QtWidgets.QDoubleSpinBox()
        self.bandB_stop.setDecimals(3)
        self.bandB_stop.setRange(1.0, 6000.0)
        self.bandB_stop.setValue(148.0)
        bg_layout.addWidget(self.bandB_label, row, 0)
        bg_layout.addWidget(self.bandB_enable, row, 1)
        bg_layout.addWidget(self.bandB_start, row, 2)
        bg_layout.addWidget(self.bandB_stop, row, 3)

        row += 1
        self.bandC_label = QtWidgets.QLabel("Band C")
        self.bandC_enable = QtWidgets.QCheckBox()
        self.bandC_enable.setChecked(True)
        self.bandC_start = QtWidgets.QDoubleSpinBox()
        self.bandC_start.setDecimals(3)
        self.bandC_start.setRange(1.0, 6000.0)
        self.bandC_start.setValue(420.0)
        self.bandC_stop = QtWidgets.QDoubleSpinBox()
        self.bandC_stop.setDecimals(3)
        self.bandC_stop.setRange(1.0, 6000.0)
        self.bandC_stop.setValue(450.0)
        bg_layout.addWidget(self.bandC_label, row, 0)
        bg_layout.addWidget(self.bandC_enable, row, 1)
        bg_layout.addWidget(self.bandC_start, row, 2)
        bg_layout.addWidget(self.bandC_stop, row, 3)

        # Bin width + auto mode
        row += 1
        bin_label = QtWidgets.QLabel("Bin width (Hz)")
        bg_layout.addWidget(bin_label, row, 0)
        self.bin_width_spin = QtWidgets.QSpinBox()
        self.bin_width_spin.setRange(2445, 5_000_000)
        self.bin_width_spin.setSingleStep(1000)
        self.bin_width_spin.setValue(250_000)
        bg_layout.addWidget(self.bin_width_spin, row, 1)

        self.auto_bin_checkbox = QtWidgets.QCheckBox("Auto")
        self.auto_bin_checkbox.setToolTip(
            "Automatically choose bin width based on the widest enabled band\n"
            "and the 'Max bins' target."
        )
        self.auto_bin_checkbox.setChecked(True)
        bg_layout.addWidget(self.auto_bin_checkbox, row, 2)

        self.max_bins_spin = QtWidgets.QSpinBox()
        self.max_bins_spin.setRange(50, 2000)
        self.max_bins_spin.setSingleStep(50)
        self.max_bins_spin.setValue(400)
        self.max_bins_spin.setToolTip(
            "In auto mode, this is the approximate maximum number of bins\n"
            "per band. Smaller = coarser bin width; larger = finer bin width."
        )
        bg_layout.addWidget(self.max_bins_spin, row, 3)

        # Presets
        row += 1
        bg_layout.addWidget(QtWidgets.QLabel("Preset:"), row, 0)
        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.addItems(
            [
                "Custom",
                "VHF Ham",
                "UHF Ham + GMRS/FRS",
                "915 MHz ISM",
                "2.4 GHz ISM",
                "5.8 GHz ISM",
            ]
        )
        bg_layout.addWidget(self.preset_combo, row, 1)

        bg_layout.addWidget(QtWidgets.QLabel("Apply to:"), row, 2)
        self.preset_target_combo = QtWidgets.QComboBox()
        self.preset_target_combo.addItems(["All bands", "Band A", "Band B", "Band C"])
        bg_layout.addWidget(self.preset_target_combo, row, 3)

        row += 1
        self.apply_preset_btn = QtWidgets.QPushButton("Apply")
        bg_layout.addWidget(self.apply_preset_btn, row, 3)

        main_layout.addWidget(band_group)

        # Debug / status group
        debug_group = QtWidgets.QGroupBox("Debug / status")
        dbg_layout = QtWidgets.QVBoxLayout(debug_group)
        self.debug_label = QtWidgets.QLabel()
        self.debug_label.setWordWrap(True)
        dbg_layout.addWidget(self.debug_label)

        # Bottom: alarms table + log + debug in a splitter
        bottom_splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Frequency (MHz)", "Power (dB)", "Age (s)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)

        self.log_edit = QtWidgets.QTextEdit()
        self.log_edit.setReadOnly(True)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        font.setPointSize(14)
        self.log_edit.setFont(font)

        bottom_splitter.addWidget(self.table)
        bottom_splitter.addWidget(self.log_edit)
        bottom_splitter.addWidget(debug_group)
        bottom_splitter.setStretchFactor(0, 3)
        bottom_splitter.setStretchFactor(1, 1)
        bottom_splitter.setStretchFactor(2, 1)

        main_layout.addWidget(bottom_splitter, 1)

        # Connections
        self.start_btn.clicked.connect(self.start_watchdog)
        self.stop_btn.clicked.connect(self.stop_watchdog)
        self.apply_preset_btn.clicked.connect(self.on_apply_preset)
        self.dark_mode_checkbox.toggled.connect(self.apply_dark_mode)
        self.clear_log_btn.clicked.connect(self.clear_log)
        self.refresh_devices_btn.clicked.connect(self.refresh_device_list)
        self.use_noise_floor_cb.toggled.connect(self.on_use_noise_floor_toggled)
        self.threshold_spin.valueChanged.connect(self.on_threshold_changed)
        self.auto_bin_checkbox.toggled.connect(self.on_auto_bin_toggled)

        self.cal_gain_spin.valueChanged.connect(self.on_cal_changed)
        self.cal_loss_spin.valueChanged.connect(self.on_cal_changed)
        self.ppm_spin.valueChanged.connect(self.on_ppm_changed)

        # Initial state
        self.on_auto_bin_toggled(self.auto_bin_checkbox.isChecked())
        self.on_use_noise_floor_toggled(self.use_noise_floor_cb.isChecked())
        self.on_cal_changed()
        self.update_effective_threshold_label()
        self.update_debug_info(False)

    def _create_timers(self):
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.setInterval(500)
        self.update_timer.timeout.connect(self.refresh_detection_table)
        self.update_timer.start()

    # ---------------- Device handling ----------------

    def refresh_device_list(self):
        self.device_combo.clear()
        self.device_combo.addItem("Default (first HackRF)", userData=None)

        devices = list_hackrf_devices()
        for dev in devices:
            label = f"HackRF {dev['index']} – {dev['serial']}"
            self.device_combo.addItem(label, userData=dev["serial"])

    # ---------------- Calibration helpers ----------------

    def net_cal_offset_db(self) -> float:
        return float(self.cal_gain_spin.value()) - float(self.cal_loss_spin.value())

    def on_cal_changed(self):
        net = self.net_cal_offset_db()
        self.cal_net_label.setText(f"Net power offset: {net:+.1f} dB (gain − loss)")

        if self.worker is not None:
            self.worker.cal_gain_db = float(self.cal_gain_spin.value())
            self.worker.cal_loss_db = float(self.cal_loss_spin.value())

        self.update_effective_threshold_label()
        self.update_debug_info(self.worker is not None)

    def on_ppm_changed(self, value: float):
        if self.worker is not None:
            self.worker.freq_ppm = float(value)
        self.update_debug_info(self.worker is not None)

    # ---------------- Threshold / status helpers ----------------

    def on_use_noise_floor_toggled(self, checked: bool):
        if checked:
            self.threshold_spin.setRange(0.0, 120.0)
            if self.threshold_spin.value() < 0:
                self.threshold_spin.setValue(3.0)
            self.threshold_spin.setToolTip(
                "Threshold in dB ABOVE the estimated noise floor.\n"
                "Noise floor is shown on the right.\n\n"
                "Note: Power calibration (gain/loss) is applied before thresholding."
            )
        else:
            self.threshold_spin.setRange(-150.0, 50.0)
            self.threshold_spin.setToolTip(
                "Absolute threshold in dB as reported by hackrf_sweep.\n"
                "Use this if you want to ignore the automatic noise floor estimate.\n\n"
                "Note: Power calibration (gain/loss) is applied before thresholding."
            )

        if self.worker is not None:
            self.worker.use_local_noise_floor = checked

        self.update_effective_threshold_label()

    def on_threshold_changed(self, value: float):
        if self.worker is not None:
            self.worker.threshold_db = float(value)
        self.update_effective_threshold_label()

    def update_effective_threshold_label(self):
        thr = float(self.threshold_spin.value())
        net = self.net_cal_offset_db()

        if self.use_noise_floor_cb.isChecked():
            if self.current_noise_floor is None:
                self.eff_threshold_label.setText(
                    f"Effective threshold: (waiting for noise floor, offset {thr:.1f} dB; cal {net:+.1f} dB)"
                )
            else:
                abs_thr = float(self.current_noise_floor) + thr
                self.eff_threshold_label.setText(
                    f"Effective threshold: {abs_thr:.1f} dB "
                    f"(noise {self.current_noise_floor:.1f} + {thr:.1f}; cal {net:+.1f} applied)"
                )
        else:
            self.eff_threshold_label.setText(
                f"Effective threshold: {thr:.1f} dB (absolute; cal {net:+.1f} applied)"
            )

    def on_auto_bin_toggled(self, checked: bool):
        self.bin_width_spin.setEnabled(not checked)
        self.max_bins_spin.setEnabled(checked)
        self.update_debug_info(self.worker is not None)

    def choose_auto_bin_width(self, bands: List[Dict[str, Any]]) -> int:
        max_bins = self.max_bins_spin.value()
        if max_bins <= 0:
            max_bins = 400

        max_span_hz = 0.0
        for b in bands:
            if not b.get("enabled", True):
                continue
            span_hz = (b["stop_mhz"] - b["start_mhz"]) * 1e6
            if span_hz > max_span_hz:
                max_span_hz = span_hz

        if max_span_hz <= 0:
            return int(self.bin_width_spin.value()) or 250_000

        raw_bin = max_span_hz / float(max_bins)

        min_bin = 10_000
        max_bin = 1_000_000
        if raw_bin < min_bin:
            raw_bin = min_bin
        if raw_bin > max_bin:
            raw_bin = max_bin

        nice = int(round(raw_bin / 10_000.0)) * 10_000
        if nice <= 0:
            nice = min_bin

        return nice

    def update_debug_info(self, running: bool):
        state = "RUNNING" if running else "Idle"
        bin_width = self.current_bin_width
        interval_ms = self.interval_spin.value()
        bin_mode = "auto" if self.auto_bin_checkbox.isChecked() else "manual"
        net = self.net_cal_offset_db()
        ppm = float(self.ppm_spin.value())
        bias = "ON" if self.bias_tee_checkbox.isChecked() else "OFF"

        bands_info = []
        for label, enabled_cb, start_spin, stop_spin in [
            ("A", self.bandA_enable, self.bandA_start, self.bandA_stop),
            ("B", self.bandB_enable, self.bandB_start, self.bandB_stop),
            ("C", self.bandC_enable, self.bandC_start, self.bandC_stop),
        ]:
            start = start_spin.value()
            stop = stop_spin.value()
            enabled = enabled_cb.isChecked() and (stop > start)
            status = "enabled" if enabled else "disabled"
            bands_info.append(f"{label}: {start:.3f}-{stop:.3f} MHz ({status})")

        bands_text = "; ".join(bands_info) if bands_info else "None"

        self.debug_label.setText(
            f"State: {state}\n"
            f"Bin width: {bin_width} Hz ({bin_mode})\n"
            f"Interval: {interval_ms} ms\n"
            f"Power cal: net {net:+.1f} dB (gain {self.cal_gain_spin.value():.1f} − loss {self.cal_loss_spin.value():.1f})\n"
            f"Freq corr: {ppm:+.1f} ppm\n"
            f"Bias-T: {bias}\n"
            f"Bands: {bands_text}"
        )

    # ---------------- Sound helper ----------------

    def play_alarm_sound(self):
        if not self.beep_checkbox.isChecked():
            return

        mode = self.beep_sound_combo.currentData()
        if mode == "system" or mode is None:
            QtWidgets.QApplication.beep()
            return

        filename_map = {
            "soft_ding": "soft_ding.wav",
            "short_chirp": "short_chirp.wav",
            "alarm": "alarm.wav",
        }
        fname = filename_map.get(mode)
        if not fname:
            QtWidgets.QApplication.beep()
            return

        base_dir = os.path.dirname(os.path.abspath(__file__))
        sound_path = os.path.join(base_dir, "sounds", fname)
        if not os.path.exists(sound_path):
            QtWidgets.QApplication.beep()
            return

        effect = self.sound_effects.get(mode)
        if effect is None:
            effect = QSoundEffect(self)
            effect.setSource(QtCore.QUrl.fromLocalFile(sound_path))
            effect.setVolume(0.9)
            self.sound_effects[mode] = effect

        effect.play()

    # ---------------- Worker control ----------------

    def start_watchdog(self):
        if self.worker is not None:
            return

        bands = []
        for name, enabled_cb, start_spin, stop_spin in [
            ("A", self.bandA_enable, self.bandA_start, self.bandA_stop),
            ("B", self.bandB_enable, self.bandB_start, self.bandB_stop),
            ("C", self.bandC_enable, self.bandC_start, self.bandC_stop),
        ]:
            if not enabled_cb.isChecked():
                continue
            start_mhz = start_spin.value()
            stop_mhz = stop_spin.value()
            if stop_mhz <= start_mhz:
                continue
            bands.append(
                {
                    "name": name,
                    "enabled": True,
                    "start_mhz": start_mhz,
                    "stop_mhz": stop_mhz,
                    "start_hz": start_mhz * 1e6,
                    "stop_hz": stop_mhz * 1e6,
                }
            )

        if not bands:
            QtWidgets.QMessageBox.warning(
                self,
                "No bands",
                "Please enable at least one band with a valid start/stop range.",
            )
            return

        if self.auto_bin_checkbox.isChecked():
            bin_width = int(self.choose_auto_bin_width(bands))
            self.append_log(f"Auto bin width selected: {bin_width} Hz")
        else:
            bin_width = int(self.bin_width_spin.value())

        self.current_bin_width = bin_width

        threshold_db = float(self.threshold_spin.value())
        use_noise_floor = self.use_noise_floor_cb.isChecked()
        only_above = self.only_above_threshold_cb.isChecked()
        interval_ms = int(self.interval_spin.value())
        min_hold = float(self.persistence_spin.value())
        device_arg = self.device_combo.currentData()  # None or serial

        antenna_power = self.bias_tee_checkbox.isChecked()
        cal_gain = float(self.cal_gain_spin.value())
        cal_loss = float(self.cal_loss_spin.value())
        ppm = float(self.ppm_spin.value())

        self.detections.clear()
        self.status_label.setText("Sweeping...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # Best-effort Bias-T enable before starting sweeps
        self.bias_tee_requested = bool(antenna_power)
        self.bias_tee_engaged = False
        if antenna_power:
            # Try hackrf_biast first (if present). If it isn't present, we still pass -p 1 in sweeps.
            self.bias_tee_engaged = set_bias_tee(True, self.append_log, serial=device_arg)

        self.worker_thread = QtCore.QThread(self)
        self.worker = SweepWorker(
            bands=bands,
            bin_width_hz=bin_width,
            threshold_db=threshold_db,
            use_local_noise_floor=use_noise_floor,
            only_above_threshold=only_above,
            min_hold_time_s=min_hold,
            interval_ms=interval_ms,
            device_arg=device_arg,
            antenna_power=antenna_power,
            cal_gain_db=cal_gain,
            cal_loss_db=cal_loss,
            freq_ppm=ppm,
        )
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)

        self.worker.log_message.connect(self.append_log)
        self.worker.noise_floor_updated.connect(self.on_noise_floor_updated)
        self.worker.detections_found.connect(self.on_detections_found)

        self.worker_thread.start()
        self.append_log("Starting watchdog...")
        self.update_debug_info(True)

    def stop_watchdog(self):
        if self.worker is not None:
            self.append_log("Stopping watchdog...")
            self.status_label.setText("Stopping...")
            self.worker.stop()

        # Best-effort Bias-T off immediately (and also again on worker finished)
        if self.bias_tee_requested:
            device_arg = self.device_combo.currentData()
            set_bias_tee(False, self.append_log, serial=device_arg)
            self.bias_tee_engaged = False
            self.bias_tee_requested = False

        if self.worker is None:
            self.status_label.setText("Idle")

    def on_worker_finished(self):
        self.append_log("Worker finished.")

        # Best-effort Bias-T off on exit
        if self.bias_tee_requested or self.bias_tee_engaged:
            device_arg = self.device_combo.currentData()
            set_bias_tee(False, self.append_log, serial=device_arg)
            self.bias_tee_requested = False
            self.bias_tee_engaged = False

        self.status_label.setText("Idle")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None
        self.worker_thread = None
        self.update_debug_info(False)

    # ---------------- Slots / helpers ----------------

    @QtCore.pyqtSlot(float)
    def on_noise_floor_updated(self, value: float):
        self.current_noise_floor = value
        self.noise_floor_label.setText(f"Noise floor: {value:.1f} dB")
        self.update_effective_threshold_label()

    @QtCore.pyqtSlot(list)
    def on_detections_found(self, detections: List[Dict[str, Any]]):
        for d in detections:
            freq = round(float(d["freq_mhz"]), 6)
            existing = self.detections.get(freq)
            if existing is None or float(d["power_dbm"]) > float(existing["power_dbm"]):
                self.detections[freq] = d
            else:
                existing["timestamp"] = d["timestamp"]

        if detections:
            self.play_alarm_sound()

        for d in detections:
            try:
                send_cot_for_detection(d, noise_floor=self.current_noise_floor)
            except Exception as e:
                self.append_log(f"ATAK send error: {e}")

    def refresh_detection_table(self):
        now = time.time()

        items = sorted(
            self.detections.items(),
            key=lambda kv: kv[1]["timestamp"],
            reverse=True,
        )

        self.table.setRowCount(len(items))

        for row, (freq, d) in enumerate(items):
            age = now - float(d["timestamp"])

            freq_item = QtWidgets.QTableWidgetItem(f"{freq:.6f}")
            power_item = QtWidgets.QTableWidgetItem(f"{float(d['power_dbm']):.1f}")
            age_item = QtWidgets.QTableWidgetItem(f"{age:.1f}")

            self.table.setItem(row, 0, freq_item)
            self.table.setItem(row, 1, power_item)
            self.table.setItem(row, 2, age_item)

    def append_log(self, text: str):
        self.log_edit.append(text)
        self.log_edit.moveCursor(QtGui.QTextCursor.End)

    def clear_log(self):
        self.log_edit.clear()

    def on_apply_preset(self):
        preset = self.preset_combo.currentText()
        target = self.preset_target_combo.currentText()

        def apply_to_band(band: str, start_mhz: float, stop_mhz: float):
            if band == "Band A":
                self.bandA_start.setValue(start_mhz)
                self.bandA_stop.setValue(stop_mhz)
                self.bandA_enable.setChecked(True)
            elif band == "Band B":
                self.bandB_start.setValue(start_mhz)
                self.bandB_stop.setValue(stop_mhz)
                self.bandB_enable.setChecked(True)
            elif band == "Band C":
                self.bandC_start.setValue(start_mhz)
                self.bandC_stop.setValue(stop_mhz)
                self.bandC_enable.setChecked(True)

        if preset == "VHF Ham":
            start, stop = 144.0, 148.0
            if target == "All bands":
                for band in ("Band A", "Band B", "Band C"):
                    apply_to_band(band, start, stop)
            else:
                apply_to_band(target, start, stop)

        elif preset == "UHF Ham + GMRS/FRS":
            if target == "All bands":
                apply_to_band("Band A", 420.0, 450.0)
                apply_to_band("Band B", 462.0, 468.0)
                apply_to_band("Band C", 420.0, 470.0)
            else:
                apply_to_band(target, 420.0, 470.0)

        elif preset in ("915 MHz ISM", "2.4 GHz ISM", "5.8 GHz ISM"):
            if preset == "915 MHz ISM":
                start, stop = 902.0, 928.0
            elif preset == "2.4 GHz ISM":
                start, stop = 2400.0, 2483.5
            else:
                start, stop = 5650.0, 5850.0

            if target == "All bands":
                for band in ("Band A", "Band B", "Band C"):
                    apply_to_band(band, start, stop)
            else:
                apply_to_band(target, start, stop)

        self.update_debug_info(self.worker is not None)

    def apply_dark_mode(self, enabled: bool):
        if enabled:
            self.setStyleSheet(
                """
                QWidget { background-color: #222; color: #eee; }
                QGroupBox { border: 1px solid #444; margin-top: 6px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; }
                QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QTableWidget {
                    background-color: #333; color: #eee; border: 1px solid #555;
                }
                QHeaderView::section { background-color: #333; color: #eee; }
                QPushButton { background-color: #444; color: #eee; border: 1px solid #666; padding: 3px 8px; }
                QPushButton:disabled { background-color: #333; color: #777; }
                """
            )
        else:
            self.setStyleSheet("")


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.resize(1200, 800)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
