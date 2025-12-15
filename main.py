import sys
import time
import statistics
import subprocess
import os
import shutil
from typing import List, Dict, Any, Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtMultimedia import QSoundEffect

from hackrf_watchdog.sweep_backend import iter_sweep_frames, SweepBackendError
from hackrf_watchdog.atak_bridge import AtakBridge, AtakBridgeWindow


# ---------------------------------------------------------------------------
# Bias-T / antenna power control helper
# ---------------------------------------------------------------------------

def set_bias_tee(enable: bool, log_fn, serial: Optional[str] = None) -> bool:
    exe = shutil.which("hackrf_biast") or shutil.which("hackrf_biast.exe")
    if not exe:
        return False

    mode = "1" if enable else "0"
    cmd = [exe, "-b", mode, "-r", ("on" if enable else "off")]
    if serial:
        cmd += ["-d", str(serial)]

    try:
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
    detections_found = QtCore.pyqtSignal(list)
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
                        extra_args = ["-1"]

                        if self.device_arg:
                            extra_args += ["-d", self.device_arg]

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
        return 1.0 + (float(self.freq_ppm) / 1e6)

    def _handle_frame(self, band: Dict[str, Any], frame: Dict[str, Any]) -> None:
        powers_raw = frame["powers_dbm"]
        if not powers_raw:
            return

        cal_offset = self._net_cal_offset_db()
        powers = [p + cal_offset for p in powers_raw]

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

            if max_power is None or p_cal > max_power:
                max_power = p_cal
                max_freq_mhz = freq_mhz

        cleanup_limit = max(hold * 2.0, 10.0)
        stale_keys = []
        for k, st in self._hold_state.items():
            last_seen = st.get("last_seen")
            if last_seen is not None and (now - last_seen) > cleanup_limit:
                stale_keys.append(k)
        for k in stale_keys:
            del self._hold_state[k]

        span_txt = f"{band['start_mhz']:.3f}-{band['stop_mhz']:.3f} MHz"
        if max_power is not None and max_freq_mhz is not None:
            line = f"Max: {max_power:.1f} dB at {max_freq_mhz:.6f} MHz (span {span_txt})"
            if self.only_above_threshold:
                if max_power >= abs_threshold:
                    self.log_message.emit(line)
            else:
                self.log_message.emit(line)

        if detections:
            self.detections_found.emit(detections)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HackRF Watchdog")

        # ATAK bridge window
        self.atak_bridge = AtakBridge(self)
        self.atak_window = AtakBridgeWindow(self.atak_bridge, parent=self)
        self.atak_window.show()
        self.atak_bridge.status_changed.connect(lambda s: self.append_log(f"ATAK: {s}"))

        self.worker_thread: Optional[QtCore.QThread] = None
        self.worker: Optional[SweepWorker] = None

        self.detections: Dict[float, Dict[str, Any]] = {}
        self.current_noise_floor: Optional[float] = None
        self.sound_effects: Dict[str, QSoundEffect] = {}

        self.current_bin_width: int = 250_000

        self.bias_tee_requested: bool = False
        self.bias_tee_engaged: bool = False

        self._build_ui()
        self._create_timers()
        self.refresh_device_list()

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

        self.atak_btn = QtWidgets.QPushButton("ATAK Bridge")
        top_bar.addWidget(self.atak_btn)

        self.clear_log_btn = QtWidgets.QPushButton("Clear log")
        top_bar.addWidget(self.clear_log_btn)

        self.dark_mode_checkbox = QtWidgets.QCheckBox("Dark mode")
        top_bar.addWidget(self.dark_mode_checkbox)

        main_layout.addLayout(top_bar)

        # ---------------- Detection settings group (LEFT) ----------------
        det_group = QtWidgets.QGroupBox("Detection settings")
        det_layout = QtWidgets.QGridLayout(det_group)

        row = 0
        det_layout.addWidget(QtWidgets.QLabel("Threshold (dB)"), row, 0)
        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setRange(0.0, 120.0)
        self.threshold_spin.setSingleStep(0.5)
        self.threshold_spin.setValue(3.0)
        det_layout.addWidget(self.threshold_spin, row, 1)

        self.only_above_threshold_cb = QtWidgets.QCheckBox("Only show detections above threshold")
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
        self.persistence_spin.setValue(1.5)
        det_layout.addWidget(self.persistence_spin, row, 1)

        det_layout.addWidget(QtWidgets.QLabel("Interval (ms)"), row, 2)
        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(0, 60000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setValue(0)
        det_layout.addWidget(self.interval_spin, row, 3)

        row += 1
        self.beep_checkbox = QtWidgets.QCheckBox("Beep on detection")
        self.beep_checkbox.setChecked(False)
        det_layout.addWidget(self.beep_checkbox, row, 0, 1, 4)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Alarm sound"), row, 0)
        self.beep_sound_combo = QtWidgets.QComboBox()
        self.beep_sound_combo.addItem("System beep (default)", userData="system")
        self.beep_sound_combo.addItem("Soft ding", userData="soft_ding")
        self.beep_sound_combo.addItem("Short chirp", userData="short_chirp")
        self.beep_sound_combo.addItem("Alarm", userData="alarm")
        det_layout.addWidget(self.beep_sound_combo, row, 1, 1, 3)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Antenna/LNA gain (dB)"), row, 0)
        self.cal_gain_spin = QtWidgets.QDoubleSpinBox()
        self.cal_gain_spin.setDecimals(1)
        self.cal_gain_spin.setRange(-200.0, 200.0)
        self.cal_gain_spin.setSingleStep(0.5)
        self.cal_gain_spin.setValue(0.0)
        det_layout.addWidget(self.cal_gain_spin, row, 1)

        det_layout.addWidget(QtWidgets.QLabel("Feedline loss (dB)"), row, 2)
        self.cal_loss_spin = QtWidgets.QDoubleSpinBox()
        self.cal_loss_spin.setDecimals(1)
        self.cal_loss_spin.setRange(0.0, 200.0)
        self.cal_loss_spin.setSingleStep(0.5)
        self.cal_loss_spin.setValue(0.0)
        det_layout.addWidget(self.cal_loss_spin, row, 3)

        row += 1
        self.cal_net_label = QtWidgets.QLabel("Net power offset: +0.0 dB (gain − loss)")
        det_layout.addWidget(self.cal_net_label, row, 0, 1, 4)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Freq correction (ppm)"), row, 0)
        self.ppm_spin = QtWidgets.QDoubleSpinBox()
        self.ppm_spin.setDecimals(1)
        self.ppm_spin.setRange(-2000.0, 2000.0)
        self.ppm_spin.setSingleStep(0.5)
        self.ppm_spin.setValue(0.0)
        det_layout.addWidget(self.ppm_spin, row, 1)

        # ---------------- Device group (RIGHT) ----------------
        device_group = QtWidgets.QGroupBox("Device")
        dev_layout = QtWidgets.QGridLayout(device_group)

        dev_layout.addWidget(QtWidgets.QLabel("Type:"), 0, 0)
        self.device_type_combo = QtWidgets.QComboBox()
        self.device_type_combo.addItems(["HackRF (hackrf_sweep)"])
        dev_layout.addWidget(self.device_type_combo, 0, 1, 1, 2)

        dev_layout.addWidget(QtWidgets.QLabel("HackRF:"), 1, 0)
        self.device_combo = QtWidgets.QComboBox()
        dev_layout.addWidget(self.device_combo, 1, 1, 1, 2)

        self.refresh_devices_btn = QtWidgets.QPushButton("Refresh")
        dev_layout.addWidget(self.refresh_devices_btn, 2, 2)

        self.bias_tee_checkbox = QtWidgets.QCheckBox("Bias-T / antenna power")
        dev_layout.addWidget(self.bias_tee_checkbox, 3, 0, 1, 3)

        # Make the Device box a bit narrower so it doesn't steal width
        device_group.setMaximumWidth(420)

        # ---------------- Top row layout: Detection (left) + Device (right) ----------------
        top_row = QtWidgets.QHBoxLayout()
        det_group.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        device_group.setSizePolicy(QtWidgets.QSizePolicy.Maximum, QtWidgets.QSizePolicy.Preferred)

        top_row.addWidget(det_group, 1)
        top_row.addWidget(device_group, 0)

        main_layout.addLayout(top_row)

        # ---------------- Band configuration group ----------------
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

        row += 1
        bg_layout.addWidget(QtWidgets.QLabel("Bin width (Hz)"), row, 0)
        self.bin_width_spin = QtWidgets.QSpinBox()
        self.bin_width_spin.setRange(2445, 5_000_000)
        self.bin_width_spin.setSingleStep(1000)
        self.bin_width_spin.setValue(250_000)
        bg_layout.addWidget(self.bin_width_spin, row, 1)

        self.auto_bin_checkbox = QtWidgets.QCheckBox("Auto")
        self.auto_bin_checkbox.setChecked(True)
        bg_layout.addWidget(self.auto_bin_checkbox, row, 2)

        self.max_bins_spin = QtWidgets.QSpinBox()
        self.max_bins_spin.setRange(50, 2000)
        self.max_bins_spin.setSingleStep(50)
        self.max_bins_spin.setValue(400)
        bg_layout.addWidget(self.max_bins_spin, row, 3)

        main_layout.addWidget(band_group)

        # ---------------- Bottom splitter ----------------
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
        bottom_splitter.setStretchFactor(0, 3)
        bottom_splitter.setStretchFactor(1, 1)

        main_layout.addWidget(bottom_splitter, 1)

        # ---------------- Connections ----------------
        self.start_btn.clicked.connect(self.start_watchdog)
        self.stop_btn.clicked.connect(self.stop_watchdog)
        self.dark_mode_checkbox.toggled.connect(self.apply_dark_mode)
        self.clear_log_btn.clicked.connect(self.clear_log)
        self.refresh_devices_btn.clicked.connect(self.refresh_device_list)
        self.use_noise_floor_cb.toggled.connect(self.on_use_noise_floor_toggled)
        self.threshold_spin.valueChanged.connect(self.on_threshold_changed)
        self.auto_bin_checkbox.toggled.connect(self.on_auto_bin_toggled)

        self.cal_gain_spin.valueChanged.connect(self.on_cal_changed)
        self.cal_loss_spin.valueChanged.connect(self.on_cal_changed)
        self.ppm_spin.valueChanged.connect(self.on_ppm_changed)

        self.atak_btn.clicked.connect(self.show_atak_bridge)

        self.on_auto_bin_toggled(self.auto_bin_checkbox.isChecked())
        self.on_use_noise_floor_toggled(self.use_noise_floor_cb.isChecked())
        self.on_cal_changed()
        self.update_effective_threshold_label()

    def show_atak_bridge(self):
        self.atak_window.show()
        self.atak_window.raise_()
        self.atak_window.activateWindow()

    def _create_timers(self):
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.setInterval(500)
        self.update_timer.timeout.connect(self.refresh_detection_table)
        self.update_timer.start()

    def refresh_device_list(self):
        self.device_combo.clear()
        self.device_combo.addItem("Default (first HackRF)", userData=None)
        devices = list_hackrf_devices()
        for dev in devices:
            label = f"HackRF {dev['index']} – {dev['serial']}"
            self.device_combo.addItem(label, userData=dev["serial"])

    def net_cal_offset_db(self) -> float:
        return float(self.cal_gain_spin.value()) - float(self.cal_loss_spin.value())

    def on_cal_changed(self):
        net = self.net_cal_offset_db()
        self.cal_net_label.setText(f"Net power offset: {net:+.1f} dB (gain − loss)")
        if self.worker is not None:
            self.worker.cal_gain_db = float(self.cal_gain_spin.value())
            self.worker.cal_loss_db = float(self.cal_loss_spin.value())
        self.update_effective_threshold_label()

    def on_ppm_changed(self, value: float):
        if self.worker is not None:
            self.worker.freq_ppm = float(value)

    def on_use_noise_floor_toggled(self, checked: bool):
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
                    f"Effective threshold: (waiting for noise floor, offset {thr:.1f} dB; cal {net:+.1f} applied)"
                )
            else:
                abs_thr = float(self.current_noise_floor) + thr
                self.eff_threshold_label.setText(
                    f"Effective threshold: {abs_thr:.1f} dB (noise {self.current_noise_floor:.1f} + {thr:.1f}; cal {net:+.1f} applied)"
                )
        else:
            self.eff_threshold_label.setText(
                f"Effective threshold: {thr:.1f} dB (absolute; cal {net:+.1f} applied)"
            )

    def on_auto_bin_toggled(self, checked: bool):
        self.bin_width_spin.setEnabled(not checked)
        self.max_bins_spin.setEnabled(checked)

    def choose_auto_bin_width(self, bands: List[Dict[str, Any]]) -> int:
        max_bins = self.max_bins_spin.value() or 400
        max_span_hz = 0.0
        for b in bands:
            if not b.get("enabled", True):
                continue
            span_hz = (b["stop_mhz"] - b["start_mhz"]) * 1e6
            max_span_hz = max(max_span_hz, span_hz)

        if max_span_hz <= 0:
            return int(self.bin_width_spin.value()) or 250_000

        raw_bin = max_span_hz / float(max_bins)
        raw_bin = max(10_000, min(1_000_000, raw_bin))
        nice = int(round(raw_bin / 10_000.0)) * 10_000
        return nice if nice > 0 else 10_000

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
            QtWidgets.QMessageBox.warning(self, "No bands", "Enable at least one band.")
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
        device_arg = self.device_combo.currentData()

        antenna_power = self.bias_tee_checkbox.isChecked()
        cal_gain = float(self.cal_gain_spin.value())
        cal_loss = float(self.cal_loss_spin.value())
        ppm = float(self.ppm_spin.value())

        self.detections.clear()
        self.status_label.setText("Sweeping...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self.bias_tee_requested = bool(antenna_power)
        self.bias_tee_engaged = False
        if antenna_power:
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

    def stop_watchdog(self):
        if self.worker is not None:
            self.append_log("Stopping watchdog...")
            self.status_label.setText("Stopping...")
            self.worker.stop()

        if self.bias_tee_requested:
            device_arg = self.device_combo.currentData()
            set_bias_tee(False, self.append_log, serial=device_arg)
            self.bias_tee_engaged = False
            self.bias_tee_requested = False

        if self.worker is None:
            self.status_label.setText("Idle")

    def on_worker_finished(self):
        self.append_log("Worker finished.")

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
                self.atak_bridge.send_detection(d, noise_floor=self.current_noise_floor)
            except Exception as e:
                self.append_log(f"ATAK send error: {e}")

    def refresh_detection_table(self):
        now = time.time()
        items = sorted(self.detections.items(), key=lambda kv: kv[1]["timestamp"], reverse=True)
        self.table.setRowCount(len(items))

        for row, (freq, d) in enumerate(items):
            age = now - float(d["timestamp"])
            self.table.setItem(row, 0, QtWidgets.QTableWidgetItem(f"{freq:.6f}"))
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(f"{float(d['power_dbm']):.1f}"))
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(f"{age:.1f}"))

    def append_log(self, text: str):
        self.log_edit.append(text)
        self.log_edit.moveCursor(QtGui.QTextCursor.End)

    def clear_log(self):
        self.log_edit.clear()

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
    app.setOrganizationName("HackRF-Watchdog")
    app.setApplicationName("HackRF-Watchdog")

    win = MainWindow()
    win.resize(1200, 800)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
