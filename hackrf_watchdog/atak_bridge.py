import json
import os
import socket
import time
import uuid
import ipaddress
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, Tuple

from xml.sax.saxutils import escape
from PyQt5 import QtCore, QtWidgets


def _config_path() -> str:
    base = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppDataLocation)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "atak_bridge.json")


@dataclass
class AtakBridgeConfig:
    enabled: bool = False
    host: str = "239.2.3.1"
    port: int = 6969

    multicast_ttl: int = 1
    # If set, forces multicast outbound interface selection on Windows.
    bind_local_ip: str = ""

    lat: float = 46.84878
    lon: float = -114.03891

    cot_type: str = "a-f-G-U-C"
    stale_seconds: int = 300

    group_name: str = "Cyan"
    group_role: str = "Team"

    use_per_frequency_uid: bool = True
    callsign_prefix: str = "RF-"

    static_callsign: str = "HackRF-Watchdog"
    static_uid: str = ""


def load_config() -> AtakBridgeConfig:
    cfg = AtakBridgeConfig()
    try:
        path = _config_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
    except Exception:
        pass

    if not cfg.static_uid:
        cfg.static_uid = f"WD-{uuid.uuid4()}"
        save_config(cfg)

    return cfg


def save_config(cfg: AtakBridgeConfig) -> None:
    with open(_config_path(), "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, indent=2)


def detect_preferred_local_ipv4() -> Optional[str]:
    """
    Best-effort: ask the OS which local IP it would use for outbound traffic.
    Uses UDP connect() which does not send packets by itself.
    """
    # Try a couple of public resolvers in case one is blocked
    candidates = [("8.8.8.8", 53), ("1.1.1.1", 53)]
    for host, port in candidates:
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((host, port))
            local_ip = s.getsockname()[0]
            if local_ip and local_ip != "0.0.0.0":
                return local_ip
        except Exception:
            pass
        finally:
            try:
                if s:
                    s.close()
            except Exception:
                pass
    return None


class AtakBridge(QtCore.QObject):
    status_changed = QtCore.pyqtSignal(str)
    enabled_changed = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cfg = load_config()
        self._sock: Optional[socket.socket] = None
        self._last_sent_by_key: Dict[str, float] = {}
        self._auto_local_ip_cache: Optional[str] = None

    def set_enabled(self, enabled: bool) -> None:
        self.cfg.enabled = bool(enabled)
        save_config(self.cfg)
        self.enabled_changed.emit(self.cfg.enabled)
        self.status_changed.emit("Enabled" if self.cfg.enabled else "Disabled")

    def apply_config(self, new_cfg: AtakBridgeConfig) -> None:
        if not new_cfg.static_uid:
            new_cfg.static_uid = self.cfg.static_uid or f"WD-{uuid.uuid4()}"
        self.cfg = new_cfg
        save_config(self.cfg)
        self._auto_local_ip_cache = None
        self._reset_socket()
        self.status_changed.emit("Settings saved")

    def _reset_socket(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock = None

    def _is_multicast_host(self) -> bool:
        try:
            ip = ipaddress.ip_address(self.cfg.host)
            return ip.is_multicast
        except Exception:
            return False

    def resolve_local_ip_for_send(self) -> Optional[str]:
        """
        Returns the local IP that will be used (best-effort).
        Priority:
          1) cfg.bind_local_ip if provided
          2) auto-detect preferred outbound IP (cached)
        """
        if self.cfg.bind_local_ip:
            return self.cfg.bind_local_ip

        if self._auto_local_ip_cache:
            return self._auto_local_ip_cache

        auto_ip = detect_preferred_local_ipv4()
        self._auto_local_ip_cache = auto_ip
        return auto_ip

    def _get_socket(self) -> socket.socket:
        if self._sock:
            return self._sock

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

        # Only apply multicast options if destination is multicast
        if self._is_multicast_host():
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, int(self.cfg.multicast_ttl))
            except Exception:
                pass

            local_ip = self.resolve_local_ip_for_send()
            if local_ip:
                # Force outbound interface for multicast (critical on Windows with multiple NICs)
                try:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
                except Exception:
                    pass

        self._sock = s
        return s

    @staticmethod
    def _iso_z_ms(t: float) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))

    def _build_cot(self, uid: str, callsign: str, remarks: str) -> bytes:
        now = time.time()
        t = self._iso_z_ms(now)
        stale = self._iso_z_ms(now + max(5, int(self.cfg.stale_seconds)))

        uid_esc = escape(uid)
        callsign_esc = escape(callsign)
        remarks_esc = escape(remarks) if remarks else ""

        xml = f"""<event version="2.0"
    uid="{uid_esc}"
    type="{escape(self.cfg.cot_type)}"
    how="m-g"
    time="{t}"
    start="{t}"
    stale="{stale}">
  <point lat="{self.cfg.lat:.6f}" lon="{self.cfg.lon:.6f}" hae="0" ce="9999999" le="9999999" />
  <detail>
    <contact callsign="{callsign_esc}" />
    <__group name="{escape(self.cfg.group_name)}" role="{escape(self.cfg.group_role)}" />
    <remarks>{remarks_esc}</remarks>
  </detail>
</event>"""
        return xml.encode("utf-8")

    def _send_raw(self, payload: bytes, emit_success_status: bool = False, success_label: str = "") -> None:
        try:
            s = self._get_socket()
            s.sendto(payload, (self.cfg.host, int(self.cfg.port)))

            if emit_success_status:
                via = self.resolve_local_ip_for_send()
                via_txt = f" via {via}" if via else ""
                label = success_label or "Sent"
                self.status_changed.emit(f"{label} to {self.cfg.host}:{self.cfg.port}{via_txt}")
        except Exception as e:
            via = self.resolve_local_ip_for_send()
            via_txt = f" via {via}" if via else ""
            self.status_changed.emit(f"Send failed: {e} (to {self.cfg.host}:{self.cfg.port}{via_txt})")

    def _extract_freq_mhz(self, det: Dict[str, Any]) -> float:
        if "freq_mhz" in det:
            try:
                return float(det["freq_mhz"])
            except Exception:
                return 0.0

        for k in ("center_freq_hz", "freq_hz", "frequency_hz", "frequency"):
            if k in det:
                try:
                    hz = float(det[k])
                    return hz / 1e6 if hz > 1e5 else hz
                except Exception:
                    return 0.0
        return 0.0

    def preview_identity(self, sample_freq_mhz: float) -> Tuple[str, str]:
        fmhz = float(sample_freq_mhz)
        if self.cfg.use_per_frequency_uid and fmhz > 0:
            callsign = f"{self.cfg.callsign_prefix}{fmhz:.3f}MHz"
            uid = callsign
        else:
            callsign = self.cfg.static_callsign
            uid = self.cfg.static_uid
        return callsign, uid

    def send_test(self) -> None:
        if not self.cfg.enabled:
            self.status_changed.emit("Bridge is disabled (enable it to send)")
            return

        cot = self._build_cot(
            uid=self.cfg.static_uid,
            callsign=self.cfg.static_callsign,
            remarks="CoT test from HackRF-Watchdog",
        )
        self._send_raw(cot, emit_success_status=True, success_label="Test sent")

    def send_detection(self, det: Dict[str, Any], noise_floor: Optional[float] = None) -> None:
        if not self.cfg.enabled:
            return

        fmhz = self._extract_freq_mhz(det)
        callsign, uid = self.preview_identity(fmhz if fmhz > 0 else 0.0)
        key = uid

        now = time.time()
        last = self._last_sent_by_key.get(key, 0.0)
        if now - last < 1.0:
            return
        self._last_sent_by_key[key] = now

        parts = []
        if fmhz > 0:
            parts.append(f"Freq: {fmhz:.3f} MHz")

        power_dbm = det.get("power_dbm", None)
        if power_dbm is not None:
            try:
                parts.append(f"Power: {float(power_dbm):.1f} dB")
            except Exception:
                parts.append(f"Power: {power_dbm} dB")

        cal_offset = det.get("cal_offset_db", None)
        raw_power = det.get("power_dbm_raw", None)
        if cal_offset is not None:
            try:
                cal_offset_f = float(cal_offset)
                if abs(cal_offset_f) >= 0.05:
                    parts.append(f"Cal offset: {cal_offset_f:+.1f} dB")
            except Exception:
                pass

        if raw_power is not None and power_dbm is not None:
            try:
                raw_power_f = float(raw_power)
                power_f = float(power_dbm)
                if abs(raw_power_f - power_f) >= 0.05:
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

        if noise_floor is not None and power_dbm is not None:
            try:
                nf = float(noise_floor)
                p = float(power_dbm)
                parts.append(f"Noise floor: {nf:.1f} dB")
                parts.append(f"Above noise: {(p - nf):.1f} dB")
            except Exception:
                pass

        band = det.get("band", "")
        if band:
            parts.append(f"Band: {band}")

        remarks = " | ".join(parts) if parts else "HackRF-Watchdog detection"
        cot = self._build_cot(uid=uid, callsign=callsign, remarks=remarks)

        # Do NOT emit success status here (would spam), but we DO emit errors if they happen.
        self._send_raw(cot, emit_success_status=False)


ATAK_GROUP_COLORS = [
    "Cyan", "Blue", "Red", "Green", "Yellow", "Orange",
    "Purple", "Magenta", "White", "Black", "Gray", "Brown",
]


class AtakBridgeWindow(QtWidgets.QDialog):
    def __init__(self, bridge: AtakBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge

        self.setWindowTitle("ATAK Bridge")
        self.setModal(False)
        self.setMinimumWidth(500)

        self.enable_cb = QtWidgets.QCheckBox("Enable ATAK Bridge")

        self.host_edit = QtWidgets.QLineEdit()
        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1, 65535)

        self.ttl_spin = QtWidgets.QSpinBox()
        self.ttl_spin.setRange(0, 255)

        self.bind_ip_edit = QtWidgets.QLineEdit()
        self.bind_ip_edit.setPlaceholderText("Optional, but fixes Windows multicast routing issues")
        self.bind_ip_edit.setToolTip(
            "If ATAK/WinTAK doesn't receive markers (especially on Windows), set this\n"
            "to your PC's Wi-Fi IPv4 address (from ipconfig). This forces multicast\n"
            "to go out the correct network interface.\n\n"
            "Tip: use the Auto-detect button."
        )

        self.lat_spin = QtWidgets.QDoubleSpinBox()
        self.lat_spin.setDecimals(6)
        self.lat_spin.setRange(-90.0, 90.0)

        self.lon_spin = QtWidgets.QDoubleSpinBox()
        self.lon_spin.setDecimals(6)
        self.lon_spin.setRange(-180.0, 180.0)

        self.stale_spin = QtWidgets.QSpinBox()
        self.stale_spin.setRange(5, 3600)

        self.type_edit = QtWidgets.QLineEdit()

        self.group_color_combo = QtWidgets.QComboBox()
        self.group_color_combo.addItems(ATAK_GROUP_COLORS)
        self.group_color_combo.setEditable(True)
        self.group_color_combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)

        self.group_role_edit = QtWidgets.QLineEdit()

        self.per_freq_cb = QtWidgets.QCheckBox("Use per-frequency marker UID/callsign (RF-xxxMHz)")
        self.prefix_edit = QtWidgets.QLineEdit()

        self.static_callsign_edit = QtWidgets.QLineEdit()
        self.static_uid_edit = QtWidgets.QLineEdit()
        self.static_uid_edit.setPlaceholderText("Leave blank to keep existing")

        # Preview
        self.sample_freq_spin = QtWidgets.QDoubleSpinBox()
        self.sample_freq_spin.setDecimals(3)
        self.sample_freq_spin.setRange(0.0, 6000.0)
        self.sample_freq_spin.setValue(915.000)

        self.preview_lbl = QtWidgets.QLabel("")
        self.preview_lbl.setWordWrap(True)

        self.status_lbl = QtWidgets.QLabel("")
        self.status_lbl.setWordWrap(True)

        save_btn = QtWidgets.QPushButton("Save changes")
        autodetect_btn = QtWidgets.QPushButton("Auto-detect Bind IP")
        test_btn = QtWidgets.QPushButton("Send test")
        close_btn = QtWidgets.QPushButton("Close")

        form = QtWidgets.QFormLayout()
        form.addRow(self.enable_cb)
        form.addRow("Host", self.host_edit)
        form.addRow("Port", self.port_spin)
        form.addRow("Multicast TTL", self.ttl_spin)

        # Bind row + button
        bind_row = QtWidgets.QHBoxLayout()
        bind_row.addWidget(self.bind_ip_edit, 1)
        bind_row.addWidget(autodetect_btn)
        form.addRow("Bind local IP", bind_row)

        form.addRow("Latitude", self.lat_spin)
        form.addRow("Longitude", self.lon_spin)
        form.addRow("Stale (sec)", self.stale_spin)
        form.addRow("CoT type", self.type_edit)
        form.addRow("Group color/name", self.group_color_combo)
        form.addRow("Group role", self.group_role_edit)
        form.addRow(self.per_freq_cb)
        form.addRow("Callsign prefix", self.prefix_edit)
        form.addRow("Static callsign", self.static_callsign_edit)
        form.addRow("Static UID", self.static_uid_edit)
        form.addRow("Preview freq (MHz)", self.sample_freq_spin)
        form.addRow("Preview marker", self.preview_lbl)

        btns = QtWidgets.QHBoxLayout()
        btns.addWidget(save_btn)
        btns.addWidget(test_btn)
        btns.addStretch(1)
        btns.addWidget(close_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.status_lbl)
        layout.addLayout(btns)

        save_btn.clicked.connect(self.on_save)
        autodetect_btn.clicked.connect(self.on_autodetect_bind_ip)
        test_btn.clicked.connect(self.bridge.send_test)
        close_btn.clicked.connect(self.hide)
        self.enable_cb.toggled.connect(self.bridge.set_enabled)

        self.bridge.status_changed.connect(self.status_lbl.setText)
        self.bridge.enabled_changed.connect(self.enable_cb.setChecked)

        # live preview updates
        self.sample_freq_spin.valueChanged.connect(self.update_preview)
        self.per_freq_cb.toggled.connect(self.update_preview)
        self.prefix_edit.textChanged.connect(self.update_preview)
        self.static_callsign_edit.textChanged.connect(self.update_preview)

        self.load_into_ui()
        self.update_preview()

    def load_into_ui(self) -> None:
        cfg = self.bridge.cfg

        self.enable_cb.setChecked(cfg.enabled)
        self.host_edit.setText(cfg.host)
        self.port_spin.setValue(int(cfg.port))
        self.ttl_spin.setValue(int(cfg.multicast_ttl))
        self.bind_ip_edit.setText(cfg.bind_local_ip or "")

        self.lat_spin.setValue(float(cfg.lat))
        self.lon_spin.setValue(float(cfg.lon))

        self.stale_spin.setValue(int(cfg.stale_seconds))
        self.type_edit.setText(cfg.cot_type)

        idx = self.group_color_combo.findText(cfg.group_name)
        if idx >= 0:
            self.group_color_combo.setCurrentIndex(idx)
        else:
            self.group_color_combo.setCurrentText(cfg.group_name)

        self.group_role_edit.setText(cfg.group_role)

        self.per_freq_cb.setChecked(bool(cfg.use_per_frequency_uid))
        self.prefix_edit.setText(cfg.callsign_prefix)

        self.static_callsign_edit.setText(cfg.static_callsign)
        self.static_uid_edit.setText("")

    def update_preview(self) -> None:
        use_per_freq = self.per_freq_cb.isChecked()
        prefix = (self.prefix_edit.text().strip() or "RF-")
        static_callsign = (self.static_callsign_edit.text().strip() or "HackRF-Watchdog")
        sample = float(self.sample_freq_spin.value())

        if use_per_freq and sample > 0:
            callsign = f"{prefix}{sample:.3f}MHz"
            uid = callsign
        else:
            callsign = static_callsign
            uid = self.bridge.cfg.static_uid

        self.preview_lbl.setText(f"Callsign: {callsign}\nUID: {uid}")

    def on_autodetect_bind_ip(self) -> None:
        ip = detect_preferred_local_ipv4()
        if not ip:
            self.status_lbl.setText("Auto-detect failed. Use ipconfig and enter your Wi-Fi IPv4 manually.")
            return

        self.bind_ip_edit.setText(ip)
        # Save immediately so next run works without extra clicks
        self.on_save()
        self.status_lbl.setText(f"Bind local IP auto-set to {ip} (saved)")

    def on_save(self) -> None:
        new_cfg = AtakBridgeConfig(
            enabled=self.enable_cb.isChecked(),
            host=self.host_edit.text().strip() or "239.2.3.1",
            port=int(self.port_spin.value()),
            multicast_ttl=int(self.ttl_spin.value()),
            bind_local_ip=self.bind_ip_edit.text().strip(),

            lat=float(self.lat_spin.value()),
            lon=float(self.lon_spin.value()),

            stale_seconds=int(self.stale_spin.value()),
            cot_type=self.type_edit.text().strip() or "a-f-G-U-C",

            group_name=self.group_color_combo.currentText().strip() or "Cyan",
            group_role=self.group_role_edit.text().strip() or "Team",

            use_per_frequency_uid=self.per_freq_cb.isChecked(),
            callsign_prefix=self.prefix_edit.text().strip() or "RF-",

            static_callsign=self.static_callsign_edit.text().strip() or "HackRF-Watchdog",
            static_uid=self.static_uid_edit.text().strip(),
        )

        if not new_cfg.static_uid:
            new_cfg.static_uid = self.bridge.cfg.static_uid

        self.bridge.apply_config(new_cfg)
        self.update_preview()
