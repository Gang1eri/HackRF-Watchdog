import sys
import time
import statistics
from typing import List, Dict, Any

from PyQt5 import QtCore, QtGui, QtWidgets

from hackrf_watchdog.sweep_backend import iter_sweep_frames, SweepBackendError


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
        parent=None,
    ):
        super().__init__(parent)
        self.bands = bands
        self.bin_width_hz = bin_width_hz
        self.threshold_db = threshold_db
        self.use_local_noise_floor = use_local_noise_floor
        self.only_above_threshold = only_above_threshold
        self.min_hold_time_s = float(min_hold_time_s)  # detection persistence (hold time)
        self.interval_ms = interval_ms
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
                        for frame in iter_sweep_frames(
                            start_hz,
                            stop_hz,
                            self.bin_width_hz,
                            extra_args=["-1"],
                        ):
                            self._handle_frame(band, frame)
                            # iter_sweep_frames with -1 gives one frame; break after it
                            break
                    except SweepBackendError as e:
                        self.log_message.emit(f"Error from hackrf_sweep: {e}")
                        # If HackRF is missing or busy, don't spin like crazy
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

    def _handle_frame(self, band: Dict[str, Any], frame: Dict[str, Any]) -> None:
        powers = frame["powers_dbm"]
        if not powers:
            return

        # Estimate noise floor: median of lower 80% of samples
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
            abs_threshold = self._noise_floor + self.threshold_db
        else:
            abs_threshold = self.threshold_db

        low_hz = frame["low_hz"]
        bin_w = frame["bin_width_hz"]

        detections = []
        max_power = None
        max_freq_mhz = None

        now = time.time()
        hold = self.min_hold_time_s  # detection persistence / hold time

        for idx, p in enumerate(powers):
            center_hz = low_hz + (idx + 0.5) * bin_w
            freq_mhz = center_hz / 1e6
            key = round(freq_mhz, 6)

            st = self._hold_state.get(key)

            if p >= abs_threshold:
                if st is None or not st.get("above", False):
                    st = {"first_seen": now, "last_seen": now, "above": True}
                    self._hold_state[key] = st
                else:
                    st["last_seen"] = now
                    st["above"] = True

                dwell = 0.0 if st["first_seen"] is None else now - st["first_seen"]

                # Persistence / hold-time: only count as detection if it has lasted long enough
                if hold <= 0 or dwell >= hold:
                    detections.append(
                        {
                            "freq_mhz": freq_mhz,
                            "power_dbm": p,
                            "timestamp": now,
                        }
                    )
            else:
                if st is not None and st.get("above", False):
                    # Signal dropped below threshold; reset hold
                    st["above"] = False
                    st["first_seen"] = None
                    st["last_seen"] = now

            # Track max power regardless of hold time
            if max_power is None or p > max_power:
                max_power = p
                max_freq_mhz = freq_mhz

        # Cleanup stale hold-state entries
        cleanup_limit = max(hold * 2.0, 10.0)
        stale_keys = []
        for key, st in self._hold_state.items():
            last_seen = st.get("last_seen")
            if last_seen is not None and (now - last_seen) > cleanup_limit:
                stale_keys.append(key)
        for key in stale_keys:
            del self._hold_state[key]

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


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HackRF Watchdog")

        self.worker_thread: QtCore.QThread | None = None
        self.worker: SweepWorker | None = None

        # freq -> detection dict
        self.detections: Dict[float, Dict[str, Any]] = {}

        self._build_ui()
        self._create_timers()

    # ---------------- UI ----------------

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        main_layout = QtWidgets.QVBoxLayout(central)

        # Top bar: Start/Stop + status + clear log + dark mode
        top_bar = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.status_label = QtWidgets.QLabel("Idle")

        top_bar.addWidget(self.start_btn)
        top_bar.addWidget(self.stop_btn)
        top_bar.addWidget(self.status_label)
        top_bar.addStretch(1)

        # NEW: Clear log button
        self.clear_log_btn = QtWidgets.QPushButton("Clear log")
        self.clear_log_btn.setToolTip("Clear the text log below.")
        top_bar.addWidget(self.clear_log_btn)

        self.dark_mode_checkbox = QtWidgets.QCheckBox("Dark mode")
        self.dark_mode_checkbox.setToolTip(
            "Toggle a darker color scheme for the interface.\n"
            "This does not affect detection logic, only the look of the UI."
        )
        top_bar.addWidget(self.dark_mode_checkbox)

        main_layout.addLayout(top_bar)

        # Detection settings group
        det_group = QtWidgets.QGroupBox("Detection settings")
        det_layout = QtWidgets.QGridLayout(det_group)

        row = 0
        det_layout.addWidget(QtWidgets.QLabel("Threshold (dB)"), row, 0)
        self.threshold_spin = QtWidgets.QDoubleSpinBox()
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setRange(0.0, 50.0)
        self.threshold_spin.setSingleStep(0.5)
        self.threshold_spin.setValue(3.0)
        self.threshold_spin.setToolTip(
            "Detection threshold.\n"
            "If 'Use local noise floor' is checked, this is relative to the\n"
            "estimated noise floor (e.g. 3 dB above noise).\n"
            "If unchecked, this is an absolute power level in dB."
        )
        det_layout.addWidget(self.threshold_spin, row, 1)

        self.only_above_threshold_cb = QtWidgets.QCheckBox(
            "Only show detections above threshold"
        )
        self.only_above_threshold_cb.setChecked(True)
        self.only_above_threshold_cb.setToolTip(
            "When checked, only log and show sweeps where the maximum peak\n"
            "exceeds the detection threshold.\n"
            "When unchecked, log the max peak for every sweep even if it is below threshold."
        )
        det_layout.addWidget(self.only_above_threshold_cb, row, 2, 1, 2)

        row += 1
        self.use_noise_floor_cb = QtWidgets.QCheckBox("Use local noise floor")
        self.use_noise_floor_cb.setChecked(True)
        self.use_noise_floor_cb.setToolTip(
            "When enabled, the threshold is applied relative to the estimated\n"
            "noise floor: effective_threshold = noise_floor + Threshold.\n"
            "When disabled, Threshold is treated as an absolute dB level."
        )
        det_layout.addWidget(self.use_noise_floor_cb, row, 0, 1, 2)

        self.noise_floor_label = QtWidgets.QLabel("Noise floor: --.- dB")
        self.noise_floor_label.setToolTip(
            "Smoothed estimate of the noise floor based on recent sweeps."
        )
        det_layout.addWidget(self.noise_floor_label, row, 2, 1, 2)

        row += 1
        det_layout.addWidget(QtWidgets.QLabel("Persistence / hold time (s)"), row, 0)
        self.persistence_spin = QtWidgets.QDoubleSpinBox()
        self.persistence_spin.setDecimals(1)
        self.persistence_spin.setRange(0.0, 3600.0)
        self.persistence_spin.setSingleStep(0.1)
        self.persistence_spin.setValue(3.0)
        self.persistence_spin.setToolTip(
            "Minimum time a signal must stay above threshold before it is\n"
            "treated as a detection.\n"
            "0 = every above-threshold blip counts immediately.\n"
            "Larger values = require the signal to persist across multiple sweeps."
        )
        det_layout.addWidget(self.persistence_spin, row, 1)

        det_layout.addWidget(QtWidgets.QLabel("Interval (ms)"), row, 2)
        self.interval_spin = QtWidgets.QSpinBox()
        self.interval_spin.setRange(0, 60000)
        self.interval_spin.setSingleStep(50)
        self.interval_spin.setValue(0)
        self.interval_spin.setToolTip(
            "Extra delay between full band-sweep cycles in milliseconds.\n"
            "0 = run sweeps back-to-back as fast as possible."
        )
        det_layout.addWidget(self.interval_spin, row, 3)

        main_layout.addWidget(det_group)

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

        # Bin width
        row += 1
        bin_label = QtWidgets.QLabel("Bin width (Hz)")
        bin_label.setToolTip(
            "FFT bin width for hackrf_sweep:\n"
            "• Smaller = higher frequency resolution but slower/noisier sweeps.\n"
            "• Larger = lower resolution but faster, smoother sweeps.\n"
            "Valid range: 2445–5,000,000 Hz."
        )
        bg_layout.addWidget(bin_label, row, 0)

        self.bin_width_spin = QtWidgets.QSpinBox()
        self.bin_width_spin.setRange(2445, 5_000_000)
        self.bin_width_spin.setSingleStep(1000)
        self.bin_width_spin.setValue(250000)
        self.bin_width_spin.setToolTip(
            "Bin width for hackrf_sweep.\n"
            "Smaller values = finer frequency resolution but slower sweeps.\n"
            "Larger values = faster sweeps but with less frequency detail."
        )
        bg_layout.addWidget(self.bin_width_spin, row, 1)

        # Presets
        row += 1
        bg_layout.addWidget(QtWidgets.QLabel("Preset:"), row, 0)
        self.preset_combo = QtWidgets.QComboBox()
        self.preset_combo.addItems(
            [
                "Custom",
                "VHF+UHF Ham",
                "915 MHz ISM",
                "2.4 GHz ISM",
                "5.8 GHz ISM",
            ]
        )
        bg_layout.addWidget(self.preset_combo, row, 1)

        bg_layout.addWidget(QtWidgets.QLabel("Apply to:"), row, 2)
        self.preset_target_combo = QtWidgets.QComboBox()
        self.preset_target_combo.addItems(
            ["All bands", "Band A", "Band B", "Band C"]
        )
        bg_layout.addWidget(self.preset_target_combo, row, 3)

        row += 1
        self.apply_preset_btn = QtWidgets.QPushButton("Apply")
        bg_layout.addWidget(self.apply_preset_btn, row, 3)

        main_layout.addWidget(band_group)

        # Detection table
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Frequency (MHz)", "Power (dB)", "Age (s)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        main_layout.addWidget(self.table, 1)

        # Text log (font size 14)
        self.log_edit = QtWidgets.QTextEdit()
        self.log_edit.setReadOnly(True)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        font.setPointSize(14)
        self.log_edit.setFont(font)
        main_layout.addWidget(self.log_edit, 1)

        # Connections
        self.start_btn.clicked.connect(self.start_watchdog)
        self.stop_btn.clicked.connect(self.stop_watchdog)
        self.apply_preset_btn.clicked.connect(self.on_apply_preset)
        self.dark_mode_checkbox.toggled.connect(self.apply_dark_mode)
        self.clear_log_btn.clicked.connect(self.clear_log)

    def _create_timers(self):
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.setInterval(500)
        self.update_timer.timeout.connect(self.refresh_detection_table)
        self.update_timer.start()

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

        bin_width = int(self.bin_width_spin.value())
        threshold_db = self.threshold_spin.value()
        use_noise_floor = self.use_noise_floor_cb.isChecked()
        only_above = self.only_above_threshold_cb.isChecked()
        interval_ms = int(self.interval_spin.value())
        min_hold = float(self.persistence_spin.value())

        self.detections.clear()
        self.status_label.setText("Sweeping...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        self.worker_thread = QtCore.QThread(self)
        self.worker = SweepWorker(
            bands,
            bin_width,
            threshold_db,
            use_noise_floor,
            only_above,
            min_hold,
            interval_ms,
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
        else:
            self.status_label.setText("Idle")

    def on_worker_finished(self):
        self.append_log("Worker finished.")
        self.status_label.setText("Idle")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.worker = None
        self.worker_thread = None

    # ---------------- Slots / helpers ----------------

    @QtCore.pyqtSlot(float)
    def on_noise_floor_updated(self, value: float):
        self.noise_floor_label.setText(f"Noise floor: {value:.1f} dB")

    @QtCore.pyqtSlot(list)
    def on_detections_found(self, detections: List[Dict[str, Any]]):
        for d in detections:
            freq = round(d["freq_mhz"], 6)
            existing = self.detections.get(freq)
            if existing is None or d["power_dbm"] > existing["power_dbm"]:
                self.detections[freq] = d
            else:
                existing["timestamp"] = d["timestamp"]

    def refresh_detection_table(self):
        now = time.time()

        freqs = sorted(self.detections.keys())
        self.table.setRowCount(len(freqs))

        for row, freq in enumerate(freqs):
            d = self.detections[freq]
            age = now - d["timestamp"]

            freq_item = QtWidgets.QTableWidgetItem(f"{freq:.6f}")
            power_item = QtWidgets.QTableWidgetItem(f"{d['power_dbm']:.1f}")
            age_item = QtWidgets.QTableWidgetItem(f"{age:.1f}")

            self.table.setItem(row, 0, freq_item)
            self.table.setItem(row, 1, power_item)
            self.table.setItem(row, 2, age_item)

    def append_log(self, text: str):
        self.log_edit.append(text)
        self.log_edit.moveCursor(QtGui.QTextCursor.End)

    def clear_log(self):
        """Clear the text log area."""
        self.log_edit.clear()

    def on_apply_preset(self):
        preset = self.preset_combo.currentText()
        target = self.preset_target_combo.currentText()

        def apply_to_band(
            band: str,
            start_mhz: float,
            stop_mhz: float,
        ):
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

        if preset == "VHF+UHF Ham":
            if target == "All bands":
                apply_to_band("Band A", 144.0, 148.0)
                apply_to_band("Band B", 420.0, 450.0)
                apply_to_band("Band C", 902.0, 928.0)
            elif target in ("Band A", "Band B", "Band C"):
                mapping = {
                    "Band A": (144.0, 148.0),
                    "Band B": (420.0, 450.0),
                    "Band C": (902.0, 928.0),
                }
                start, stop = mapping[target]
                apply_to_band(target, start, stop)
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

        # "Custom" does nothing

    def apply_dark_mode(self, enabled: bool):
        if enabled:
            self.setStyleSheet(
                """
                QWidget {
                    background-color: #222;
                    color: #eee;
                }
                QGroupBox {
                    border: 1px solid #444;
                    margin-top: 6px;
                }
                QGroupBox::title {
                    subcontrol-origin: margin;
                    left: 10px;
                    padding: 0 3px 0 3px;
                }
                QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QTableWidget {
                    background-color: #333;
                    color: #eee;
                    border: 1px solid #555;
                }
                QHeaderView::section {
                    background-color: #333;
                    color: #eee;
                }
                QPushButton {
                    background-color: #444;
                    color: #eee;
                    border: 1px solid #666;
                    padding: 3px 8px;
                }
                QPushButton:disabled {
                    background-color: #333;
                    color: #777;
                }
                """
            )
        else:
            self.setStyleSheet("")


def main():
    app = QtWidgets.QApplication(sys.argv)
    win = MainWindow()
    win.resize(1200, 700)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
