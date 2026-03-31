"""
Physics Data Recorder — PyQt6 main window.

Layout
------
  QToolBar (top):
      Title | spacer | Live Discovery toggle | Record/Stop | Download CSV

  QDockWidget (left, "Sensors & Data"):
      SensorPanel  — BLE discovery, connect/disconnect, sensor status list
      SessionPanel — CSV import, recorded sessions + CSV runs with visibility

  Central QWidget:
      PlotPanel    — PyQtGraph multi-subplot live + recorded plot

Architecture notes
------------------
  RecordingController — embedded in MainWindow; manages start/stop/flush
  BLEManager          — QObject with pyqtSignals; owned by MainWindow
  LiveStore           — shared data store; written by BLE threads, read by plot timer
  QTimer (300 ms)     — drives plot refresh, recording flush, and BLE poll_cleanup
"""

import csv
import datetime
import io
import re
from pathlib import Path

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDockWidget, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QMainWindow, QPushButton, QScrollArea,
    QSizePolicy, QToolBar, QVBoxLayout, QWidget, QComboBox,
)

from data_store import (
    LIVE_WINDOW_S, ImportedRun, LiveStore, RecordingSession, SensorMeta,
)
from ble_manager import PASCO_AVAILABLE, SCAN_INTERVAL_S

if PASCO_AVAILABLE:
    from ble_manager import BLEManager

# ── Colour palettes ────────────────────────────────────────────────────────────

# Vivid colours for recorded sessions and imported CSV runs.
FILE_COLORS: list[tuple[int, int, int]] = [
    (0x3b, 0x82, 0xf6),   # blue
    (0xf9, 0x73, 0x16),   # orange
    (0x10, 0xb9, 0x81),   # green
    (0xef, 0x44, 0x44),   # red
    (0x8b, 0x5c, 0xf6),   # violet
    (0x06, 0xb6, 0xd4),   # cyan
    (0xf5, 0x9e, 0x0b),   # amber
    (0x63, 0x66, 0xf1),   # indigo
    (0x84, 0xcc, 0x16),   # lime
    (0xec, 0x48, 0x99),   # pink
]

# Semi-opaque grey shades (RGBA) for live (unrecorded) sensor traces.
LIVE_GREY_COLORS: list[tuple[int, int, int, int]] = [
    (170, 170, 170, 165),
    (100, 100, 100, 165),
    (140, 140, 140, 140),
    ( 70,  70,  70, 165),
]

# Display preference for unit ordering in the plot.
_UNIT_ORDER = ["V", "A"]

# Regex patterns for Pasco/SPARKvue CSV column names.
_MEAS_RE = re.compile(r"^(.+?)\s*\((.+?)\)\s*Run\s*(\d+)$", re.IGNORECASE)
_TIME_RE = re.compile(r"^Time\s*\((.+?)\)\s*Run\s*(\d+)$",  re.IGNORECASE)


def _sorted_units(units: set[str]) -> list[str]:
    return ([u for u in _UNIT_ORDER if u in units] +
            [u for u in sorted(units) if u not in _UNIT_ORDER])


# ─────────────────────────────────────────────────────────────────────────────
# RecordingController
# ─────────────────────────────────────────────────────────────────────────────

class RecordingController:
    """Manages recording lifecycle: start, flush (per timer tick), stop.

    Timestamps in record_data are re-based so the first captured sample is
    always t = 0.  The absolute-time watermarks in _watermarks use the same
    time base as LiveBuffer (seconds since _global_t0).
    """

    def __init__(self, live_store: LiveStore) -> None:
        self._live_store   = live_store
        self.is_recording  = False
        self._record_data: dict[str, dict[str, list[tuple[float, float]]]] = {}
        self._watermarks:  dict[tuple[str, str], float] = {}
        self._sensor_labels: dict[str, str] = {}
        self._rec_start_abs: float | None = None   # abs-time of first captured sample
        self.sessions: list[RecordingSession] = []

    # ── Recording duration (rebased seconds, 0 when not recording) ────────────

    @property
    def rec_duration(self) -> float:
        if not self.is_recording or not self._record_data:
            return 0.0
        t_max = 0.0
        for sensor_map in self._record_data.values():
            for pts in sensor_map.values():
                if pts:
                    t_max = max(t_max, pts[-1][0])
        return t_max

    @property
    def rec_start_abs(self) -> float | None:
        return self._rec_start_abs

    @property
    def in_progress_data(self) -> dict[str, dict[str, list[tuple[float, float]]]] | None:
        """In-progress {unit: {addr: [(t_rebased, v)]}} or None when not recording."""
        return self._record_data if self.is_recording else None

    @property
    def in_progress_labels(self) -> dict[str, str]:
        """Sensor display labels accumulated so far during the active recording."""
        return dict(self._sensor_labels) if self.is_recording else {}

    def total_points(self) -> int:
        return sum(
            len(pts)
            for sm in self._record_data.values()
            for pts in sm.values()
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin a new recording session; set watermarks at current buffer tails."""
        self._record_data    = {}
        self._sensor_labels  = {}
        self._rec_start_abs  = None
        # Watermarks prevent old live-buffer data (before Record) from leaking in.
        snapshot = self._live_store.snapshot_all()
        self._watermarks = {}
        for unit, addr_map in snapshot.items():
            for addr, pts in addr_map.items():
                if pts:
                    self._watermarks[(unit, addr)] = pts[-1][0]
        self.is_recording = True

    def flush(self, managed_addrs: frozenset[str], label_fn) -> None:
        """Copy new live-buffer samples into record_data.

        Called unconditionally every ~300 ms while recording.
        label_fn(addr) -> str  — returns the sensor's display label.
        """
        if not self.is_recording:
            return
        snapshot = self._live_store.snapshot_all()
        for unit, addr_map in snapshot.items():
            for addr, pts in addr_map.items():
                last_t  = self._watermarks.get((unit, addr), -1.0)
                new_pts = [(t, v) for t, v in pts if t > last_t]
                if not new_pts:
                    continue
                # On first data point ever: fix the recording start.
                if self._rec_start_abs is None:
                    self._rec_start_abs = new_pts[0][0]
                rebased = [(t - self._rec_start_abs, v) for t, v in new_pts]
                (self._record_data
                    .setdefault(unit, {})
                    .setdefault(addr, [])
                    .extend(rebased))
                self._watermarks[(unit, addr)] = new_pts[-1][0]
                # Cache the label while the sensor is live.
                if addr not in self._sensor_labels:
                    lbl = label_fn(addr)
                    if lbl:
                        self._sensor_labels[addr] = lbl

    def stop(self) -> RecordingSession | None:
        """Finalise the session; return it (or None if no data was captured)."""
        self.is_recording = False
        if not self._record_data:
            return None
        label   = datetime.datetime.now().strftime("%H:%M:%S, %d-%m-%y")
        session = RecordingSession(
            label=label,
            data=dict(self._record_data),
            sensor_labels=dict(self._sensor_labels),
        )
        self.sessions.append(session)
        self._record_data   = {}
        self._sensor_labels = {}
        self._rec_start_abs = None
        return session

    def remove_session(self, session_id: str) -> None:
        self.sessions = [s for s in self.sessions if s.id != session_id]

    # ── CSV export ────────────────────────────────────────────────────────────

    def build_csv(self, imported_runs: list[ImportedRun]) -> bytes:
        """Return UTF-8 CSV bytes for all finalised sessions and imported runs.

        Format: long/tidy — columns: source, label, time_s, value, unit.
        Each row is one sample.
        """
        rows = []
        for session in self.sessions:
            for unit, sensor_map in session.data.items():
                for addr, pts in sensor_map.items():
                    lbl = session.sensor_labels.get(addr, addr[-6:])
                    for t, v in pts:
                        rows.append({
                            "source": session.label,
                            "label":  lbl,
                            "time_s": t,
                            "value":  v,
                            "unit":   unit,
                        })
        for run in imported_runs:
            for unit, series in run.data.items():
                times  = series.get("times",  [])
                values = series.get("values", [])
                for t, v in zip(times, values):
                    rows.append({
                        "source": run.label,
                        "label":  run.label,
                        "time_s": t,
                        "value":  v,
                        "unit":   unit,
                    })
        if not rows:
            return b"source,label,time_s,value,unit\n"
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["source", "label", "time_s", "value", "unit"])
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().encode()


# ─────────────────────────────────────────────────────────────────────────────
# PlotPanel
# ─────────────────────────────────────────────────────────────────────────────

class PlotPanel(QWidget):
    """PyQtGraph multi-subplot plot.

    One PlotItem per physical unit, stacked vertically.  All plots share the
    same x-axis via setXLink().  A vertical crosshair line follows the mouse.

    During live streaming the x-axis is controlled externally via set_x_range();
    the y-axis auto-ranges to the visible data.  When no live sensors are
    connected the user can pan/zoom freely.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        pg.setConfigOptions(antialias=True, background="w", foreground="k")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.pg_widget = pg.GraphicsLayoutWidget()
        layout.addWidget(self.pg_widget)

        self._plots:  dict[str, pg.PlotItem] = {}        # unit → PlotItem
        self._curves: dict[str, pg.PlotDataItem] = {}    # curve_key → curve
        self._vlines: list[pg.InfiniteLine] = []
        self._mouse_proxy = None

    # ── Layout management ─────────────────────────────────────────────────────

    def rebuild_for_units(self, unit_list: list[str]) -> None:
        """Rebuild the plot grid for the given ordered list of units."""
        self.pg_widget.clear()
        self._plots.clear()
        self._curves.clear()
        self._vlines.clear()
        self._mouse_proxy = None

        first_plot: pg.PlotItem | None = None
        for row, unit in enumerate(unit_list):
            p = self.pg_widget.addPlot(row=row, col=0)
            if first_plot is not None:
                p.setXLink(first_plot)
            else:
                first_plot = p

            p.setLabel("left",   "", units=unit)
            p.setLabel("bottom", "Time", units="s")
            p.showGrid(x=True, y=True, alpha=0.15)
            p.getAxis("left").setWidth(60)
            p.addLegend(offset=(5, 5))

            # Apply a consistent font to axis tick labels and axis title labels
            # so they match the legend typeface and weight.
            app_font = QApplication.font()
            for ax_name in ("left", "bottom"):
                ax = p.getAxis(ax_name)
                ax.setTickFont(app_font)
                ax.label.setFont(app_font)

            vl = pg.InfiniteLine(
                angle=90, movable=False,
                pen=pg.mkPen((160, 160, 160, 120), width=0.8,
                             style=Qt.PenStyle.DashLine))
            p.addItem(vl, ignoreBounds=True)
            self._vlines.append(vl)
            self._plots[unit] = p

        if first_plot is not None:
            self._mouse_proxy = pg.SignalProxy(
                self.pg_widget.scene().sigMouseMoved,
                rateLimit=60, slot=self._on_mouse_moved,
            )

    # ── Crosshair ─────────────────────────────────────────────────────────────

    def _on_mouse_moved(self, evt) -> None:
        pos = evt[0]
        for plot, vl in zip(self._plots.values(), self._vlines):
            if plot.sceneBoundingRect().contains(pos):
                x = plot.vb.mapSceneToView(pos).x()
                for v in self._vlines:
                    v.setPos(x)
                break

    # ── X-axis control ────────────────────────────────────────────────────────

    def set_x_range(self, x_start: float, x_end: float) -> None:
        """Force the x range on all plots (called during live streaming)."""
        first = next(iter(self._plots.values()), None)
        if first is None:
            return
        first.setXRange(x_start, x_end, padding=0)
        # Re-enable y auto-range so the y scale adapts as the x window scrolls.
        for p in self._plots.values():
            p.enableAutoRange(pg.ViewBox.YAxis, True)

    def get_x_range(self) -> tuple[float, float] | None:
        """Return (x_min, x_max) of the current viewport (reflects user zoom/pan)."""
        first = next(iter(self._plots.values()), None)
        if first is None:
            return None
        x_min, x_max = first.getViewBox().viewRange()[0]
        return (x_min, x_max)

    # ── Data update ───────────────────────────────────────────────────────────

    def update_curves(
        self,
        live_snapshot:        dict[str, dict[str, list[tuple[float, float]]]],
        sessions:             list[RecordingSession],
        imported_runs:        list[ImportedRun],
        managed_addrs:        frozenset[str],
        color_map:            dict[str, tuple[int, int, int]],
        is_recording:         bool,
        rec_start_abs:        float | None,
        live_window_s:        float = LIVE_WINDOW_S,
        in_progress_data:     dict | None = None,
        in_progress_labels:   dict[str, str] | None = None,
        in_progress_color_id: str | None = None,
        live_labels:          dict[str, str] | None = None,
    ) -> None:
        """Refresh all visible curves with current data.

        live_snapshot        — snapshot from LiveStore.snapshot_all()
        sessions             — RecordingSession list (all; visibility filtered here)
        imported_runs        — ImportedRun list (all; visibility filtered here)
        managed_addrs        — currently connected sensor addresses
        color_map            — id → (r,g,b) for sessions and runs
        is_recording         — True during an active recording
        rec_start_abs        — absolute t (LiveBuffer time base) of first recorded sample
        live_window_s        — current effective window width
        in_progress_data     — {unit: {addr: [(t_rebased, v)]}} for the active recording
        in_progress_labels   — {addr: label} for sensors in the active recording
        in_progress_color_id — color_map key for the in-progress session
        live_labels          — {addr: display_label} for live-trace legend entries
        """
        active_keys: set[str] = set()
        half = live_window_s / 2

        # ── Live (grey) traces — pre-recording history only ────────────────────
        for j, addr in enumerate(sorted(managed_addrs)):
            for unit, addr_map in live_snapshot.items():
                if unit not in self._plots:
                    continue
                pts = addr_map.get(addr, [])
                if not pts:
                    continue

                if is_recording and rec_start_abs is not None:
                    # Only show the pre-recording portion (x < 0).
                    # Recorded data (x ≥ 0) is drawn as the vivid in-progress trace.
                    pts = [(t - rec_start_abs, v)
                           for t, v in pts
                           if t >= rec_start_abs - half and t < rec_start_abs]
                else:
                    # Re-base so newest sample → 0 (centre of viewport).
                    t_newest = pts[-1][0]
                    pts = [(t - t_newest, v) for t, v in pts
                           if t >= t_newest - half]

                if not pts:
                    continue

                key = f"live|{unit}|{addr}"
                active_keys.add(key)
                gray = LIVE_GREY_COLORS[j % len(LIVE_GREY_COLORS)]
                pen  = pg.mkPen(color=gray, width=1.5)
                xs   = [p[0] for p in pts]
                ys   = [p[1] for p in pts]
                if key not in self._curves:
                    lv_lbl = (live_labels or {}).get(addr, addr[-6:])
                    self._curves[key] = self._plots[unit].plot(
                        xs, ys, pen=pen, name=lv_lbl)
                else:
                    self._curves[key].setData(xs, ys)
                    self._curves[key].setPen(pen)

        # ── In-progress recording trace (vivid, pre-assigned color) ──────────────
        if in_progress_data and in_progress_color_id:
            color = color_map.get(in_progress_color_id, FILE_COLORS[0])
            pen   = pg.mkPen(color=color, width=1.5)
            labels = in_progress_labels or {}
            for unit, sensor_map in in_progress_data.items():
                if unit not in self._plots:
                    continue
                for addr, pts in sensor_map.items():
                    if not pts:
                        continue
                    lbl = labels.get(addr, addr[-6:])
                    key = f"inprog|{unit}|{addr}"
                    active_keys.add(key)
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    if key not in self._curves:
                        self._curves[key] = self._plots[unit].plot(
                            xs, ys, pen=pen, name=lbl)
                    else:
                        self._curves[key].setData(xs, ys)
                        self._curves[key].setPen(pen)

        # ── Recorded session traces (vivid) ────────────────────────────────────
        for session in sessions:
            if not session.visible:
                continue
            color = color_map.get(session.id, FILE_COLORS[0])
            pen   = pg.mkPen(color=color, width=1.5)
            for unit, sensor_map in session.data.items():
                if unit not in self._plots:
                    continue
                for addr, pts in sensor_map.items():
                    lbl = session.sensor_labels.get(addr, addr[-6:])
                    trace_name = (f"{session.label} — {lbl}"
                                  if len(sensor_map) > 1 else session.label)
                    key = f"rec|{unit}|{session.id}|{addr}"
                    active_keys.add(key)
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    if key not in self._curves:
                        self._curves[key] = self._plots[unit].plot(
                            xs, ys, pen=pen, name=trace_name)
                    else:
                        self._curves[key].setData(xs, ys)
                        self._curves[key].setPen(pen)

        # ── Imported CSV traces (vivid) ────────────────────────────────────────
        for run in imported_runs:
            if not run.visible:
                continue
            color = color_map.get(run.id, FILE_COLORS[0])
            pen   = pg.mkPen(color=color, width=1.5)
            for unit, series in run.data.items():
                if unit not in self._plots:
                    continue
                key = f"csv|{unit}|{run.id}"
                active_keys.add(key)
                xs = series.get("times",  [])
                ys = series.get("values", [])
                if key not in self._curves:
                    self._curves[key] = self._plots[unit].plot(
                        xs, ys, pen=pen, name=run.label)
                else:
                    self._curves[key].setData(xs, ys)
                    self._curves[key].setPen(pen)

        # ── Remove stale curves ────────────────────────────────────────────────
        for key in list(self._curves):
            if key not in active_keys:
                unit = key.split("|")[1]
                if unit in self._plots:
                    self._plots[unit].removeItem(self._curves[key])
                del self._curves[key]


# ─────────────────────────────────────────────────────────────────────────────
# SensorPanel
# ─────────────────────────────────────────────────────────────────────────────

class SensorPanel(QWidget):
    """BLE sensor discovery, connection, and status list."""

    connect_requested    = pyqtSignal(object)  # SensorMeta
    disconnect_requested = pyqtSignal(str)     # addr

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        # Section title
        title = QLabel("LIVE SENSORS")
        title.setStyleSheet("font-size:10px; font-weight:600; color:#9ca3af;"
                            " letter-spacing:1px;")
        layout.addWidget(title)

        # Scan status + refresh row
        scan_row = QHBoxLayout()
        self._scan_label = QLabel("No scan yet")
        self._scan_label.setStyleSheet("font-size:11px; color:#6b7280;")
        scan_row.addWidget(self._scan_label, 1)
        self._refresh_btn = QPushButton("↺")
        self._refresh_btn.setFixedWidth(30)
        self._refresh_btn.setToolTip("Scan for sensors now")
        self._refresh_btn.setStyleSheet("QPushButton { border: 1px solid #d1d5db;"
                                        " border-radius:4px; padding:2px; }"
                                        " QPushButton:hover { background:#f3f4f6; }")
        scan_row.addWidget(self._refresh_btn)
        layout.addLayout(scan_row)

        # Sensor dropdown + connect row
        dropdown_row = QHBoxLayout()
        self._sensor_combo = QComboBox()
        self._sensor_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                          QSizePolicy.Policy.Fixed)
        dropdown_row.addWidget(self._sensor_combo, 1)
        self._connect_btn = QPushButton("＋")
        self._connect_btn.setFixedWidth(30)
        self._connect_btn.setToolTip("Connect selected sensor")
        self._connect_btn.setStyleSheet("QPushButton { border: 1px solid #d1d5db;"
                                        " border-radius:4px; padding:2px; }"
                                        " QPushButton:hover { background:#f3f4f6; }")
        dropdown_row.addWidget(self._connect_btn)
        layout.addLayout(dropdown_row)

        # Divider
        self._divider = QFrame()
        self._divider.setFrameShape(QFrame.Shape.HLine)
        self._divider.setStyleSheet("color:#e5e7eb;")
        self._divider.hide()
        layout.addWidget(self._divider)

        # Connected sensor rows (dynamically populated)
        self._sensor_list_layout = QVBoxLayout()
        self._sensor_list_layout.setSpacing(2)
        layout.addLayout(self._sensor_list_layout)
        self._sensor_rows: dict[str, QWidget] = {}

        layout.addStretch()

        # Internal state
        self._available: list[SensorMeta] = []

        # Wire up buttons
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._connect_btn.clicked.connect(self._on_connect)

        if not PASCO_AVAILABLE:
            self._scan_label.setText("pasco not installed")
            self._sensor_combo.setEnabled(False)
            self._connect_btn.setEnabled(False)
            self._refresh_btn.setEnabled(False)

    # ── External API ──────────────────────────────────────────────────────────

    def set_scan_status(self, in_progress: bool, last_t: float) -> None:
        import time
        if in_progress:
            self._scan_label.setText("Scanning…")
        elif last_t > 0:
            ts = datetime.datetime.fromtimestamp(last_t).strftime("%H:%M:%S")
            self._scan_label.setText(f"Last scan: {ts}")
        else:
            self._scan_label.setText("No scan yet")

    def set_available_sensors(self, available: list[SensorMeta],
                               managed: frozenset[str]) -> None:
        """Update the dropdown with sensors that are not currently managed."""
        self._available = [s for s in available if s.address not in managed]
        self._sensor_combo.blockSignals(True)
        self._sensor_combo.clear()
        for meta in self._available:
            self._sensor_combo.addItem(meta.display_label)
        self._connect_btn.setEnabled(bool(self._available))
        self._sensor_combo.setEnabled(bool(self._available))
        self._sensor_combo.blockSignals(False)

    def update_sensor_row(self, addr: str, status: str, label: str) -> None:
        """Add or refresh one connected-sensor row."""
        if status == "removed":
            self._remove_sensor_row(addr)
            return

        if addr not in self._sensor_rows:
            self._add_sensor_row(addr, label)
        row_widget = self._sensor_rows.get(addr)
        if row_widget is None:
            return

        icon_lbl   = row_widget.findChild(QLabel, f"icon_{addr}")
        status_lbl = row_widget.findChild(QLabel, f"status_{addr}")
        disc_btn   = row_widget.findChild(QPushButton, f"disc_{addr}")

        if icon_lbl:
            icon = ("🟢" if status == "connected" else
                    "🔴" if status.startswith("error") else "🟡")
            icon_lbl.setText(icon)
        if status_lbl:
            short = "" if status == "connected" else status
            status_lbl.setText(short)
        if disc_btn:
            disc_btn.setEnabled(status != "disconnecting…")
        self._divider.show()

    def _add_sensor_row(self, addr: str, label: str) -> None:
        row = QWidget()
        row.setObjectName(f"row_{addr}")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        icon_lbl = QLabel("🟡")
        icon_lbl.setObjectName(f"icon_{addr}")
        icon_lbl.setFixedWidth(18)
        h.addWidget(icon_lbl)

        name_lbl = QLabel(f"<b>{label}</b>")
        name_lbl.setStyleSheet("font-size:11px;")
        h.addWidget(name_lbl, 1)

        status_lbl = QLabel("connecting…")
        status_lbl.setObjectName(f"status_{addr}")
        status_lbl.setStyleSheet("font-size:10px; color:#6b7280;")
        h.addWidget(status_lbl)

        disc_btn = QPushButton("－")
        disc_btn.setObjectName(f"disc_{addr}")
        disc_btn.setFixedWidth(26)
        disc_btn.setToolTip("Disconnect")
        disc_btn.setStyleSheet("QPushButton { border: 1px solid #d1d5db;"
                               " border-radius:4px; padding:2px; }"
                               " QPushButton:hover { background:#fee2e2; }")
        disc_btn.clicked.connect(lambda _, a=addr: self.disconnect_requested.emit(a))
        h.addWidget(disc_btn)

        self._sensor_list_layout.addWidget(row)
        self._sensor_rows[addr] = row
        self._divider.show()

    def _remove_sensor_row(self, addr: str) -> None:
        row = self._sensor_rows.pop(addr, None)
        if row:
            self._sensor_list_layout.removeWidget(row)
            row.deleteLater()
        if not self._sensor_rows:
            self._divider.hide()

    # ── Signal forwarding ─────────────────────────────────────────────────────

    def _on_refresh(self) -> None:
        self._refresh_btn.clicked.emit()   # handled by MainWindow

    def _on_connect(self) -> None:
        idx = self._sensor_combo.currentIndex()
        if 0 <= idx < len(self._available):
            self.connect_requested.emit(self._available[idx])

    def connect_refresh_to(self, slot) -> None:
        self._refresh_btn.clicked.disconnect()
        self._refresh_btn.clicked.connect(slot)


# ─────────────────────────────────────────────────────────────────────────────
# SessionPanel
# ─────────────────────────────────────────────────────────────────────────────

class SessionPanel(QWidget):
    """CSV import, recorded sessions, and imported-run visibility toggles."""

    remove_session_requested = pyqtSignal(str)   # session_id
    remove_run_requested     = pyqtSignal(str)   # run_id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        # Section title + import button row
        top_row = QHBoxLayout()
        title = QLabel("MEASUREMENTS")
        title.setStyleSheet("font-size:10px; font-weight:600; color:#9ca3af;"
                            " letter-spacing:1px;")
        top_row.addWidget(title, 1)
        self._import_btn = QPushButton("Load CSV…")
        self._import_btn.setStyleSheet("QPushButton { font-size:10px; border:1px solid #d1d5db;"
                                       " border-radius:4px; padding:2px 6px; }"
                                       " QPushButton:hover { background:#f3f4f6; }")
        top_row.addWidget(self._import_btn)
        layout.addLayout(top_row)

        # Scroll area for entries
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._entries_widget = QWidget()
        self._entries_layout = QVBoxLayout(self._entries_widget)
        self._entries_layout.setContentsMargins(0, 0, 0, 0)
        self._entries_layout.setSpacing(2)
        self._entries_layout.addStretch()
        scroll.setWidget(self._entries_widget)
        layout.addWidget(scroll, 1)

        self._entry_rows: dict[str, QWidget] = {}

        self._import_btn.clicked.connect(self._on_import)

    # ── External API ──────────────────────────────────────────────────────────

    def add_entry(self, entry_id: str, label: str, on_toggle,
                  visible: bool = True) -> None:
        if entry_id in self._entry_rows:
            return
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)

        cb = QCheckBox(label)
        cb.setChecked(visible)
        cb.setStyleSheet("font-size:11px;")
        cb.stateChanged.connect(lambda state, eid=entry_id: on_toggle(eid, bool(state)))
        h.addWidget(cb, 1)

        remove_btn = QPushButton("－")
        remove_btn.setFixedWidth(22)
        remove_btn.setStyleSheet("QPushButton { border:1px solid #d1d5db; border-radius:4px;"
                                 " padding:1px; font-size:10px; }"
                                 " QPushButton:hover { background:#fee2e2; }")
        remove_btn.clicked.connect(lambda _, eid=entry_id: self._on_remove(eid))
        h.addWidget(remove_btn)

        # Insert before the trailing stretch.
        insert_idx = self._entries_layout.count() - 1
        self._entries_layout.insertWidget(insert_idx, row)
        self._entry_rows[entry_id] = row

    def remove_entry(self, entry_id: str) -> None:
        row = self._entry_rows.pop(entry_id, None)
        if row:
            self._entries_layout.removeWidget(row)
            row.deleteLater()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_remove(self, entry_id: str) -> None:
        if entry_id.startswith("csv_"):
            self.remove_run_requested.emit(entry_id)
        else:
            self.remove_session_requested.emit(entry_id)
        self.remove_entry(entry_id)

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load CSV", "", "CSV files (*.csv);;All files (*)")
        if path:
            self._import_btn.setProperty("pending_path", path)
            self._import_btn.clicked.emit()  # MainWindow intercepts

    def connect_import_to(self, slot) -> None:
        self._import_btn.clicked.disconnect()
        self._import_btn.clicked.connect(slot)

    def pending_import_path(self) -> str | None:
        return self._import_btn.property("pending_path")

    def clear_pending_import(self) -> None:
        self._import_btn.setProperty("pending_path", None)


# ─────────────────────────────────────────────────────────────────────────────
# MainWindow
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    """Top-level window; owns BLEManager, RecordingController, and all UI."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Physics Data Recorder")
        self.resize(1280, 800)

        # ── Core objects ──────────────────────────────────────────────────────
        self._live_store = LiveStore()
        self._rec_ctrl   = RecordingController(self._live_store)

        if PASCO_AVAILABLE:
            self._ble = BLEManager(self._live_store, parent=self)
            self._ble.scan_started.connect(self._on_scan_started)
            self._ble.scan_complete.connect(self._on_scan_complete)
            self._ble.status_changed.connect(self._on_status_changed)
            self._ble.unit_discovered.connect(self._on_unit_discovered)
        else:
            self._ble = None

        # ── Sensor metadata cache (for display after scan results arrive) ─────
        self._sensor_meta: dict[str, SensorMeta] = {}   # addr → SensorMeta
        self._scan_results: list[SensorMeta] = []

        # ── Imported CSV runs ─────────────────────────────────────────────────
        self._imported_runs: list[ImportedRun] = []

        # ── Stable colour assignment for sessions and runs ─────────────────────
        self._color_idx: int = 0
        self._color_map: dict[str, tuple[int, int, int]] = {}

        # ── Live Discovery state ───────────────────────────────────────────────
        self._live_discovery: bool = True

        # ── In-progress recording color ───────────────────────────────────────
        # Set to "rec_in_progress" when recording starts; transferred to the
        # session id on Stop so the color stays stable across the transition.
        self._in_progress_color_id: str | None = None

        # ── Effective live window width ────────────────────────────────────────
        # Initialised from the module constant; updated when:
        #   • a recording longer than the current window finishes (grows to fit)
        #   • the user zooms the plot viewport (tracks user preference)
        self._live_window_s: float = LIVE_WINDOW_S

        # User pan/zoom state:
        #   _center_offset — viewport centre relative to the natural anchor
        #     (anchor = 0 when not recording; anchor = rec_duration when recording).
        #     Updated whenever the user pans or zooms the plot.
        #   _expected_x_range — the (x_start, x_end) we last programmatically set.
        #     On the next tick we compare the actual viewport against this value.
        #     If they differ the user interacted, and we update _live_window_s and
        #     _center_offset accordingly.  Set to None after a plot rebuild so that
        #     the fresh default ViewBox range is not mistaken for user interaction.
        self._center_offset:    float = 0.0
        self._expected_x_range: tuple[float, float] | None = None

        # ── Build UI ──────────────────────────────────────────────────────────
        self._build_toolbar()
        self._build_sidebar()
        self._build_central()

        # ── Timers ────────────────────────────────────────────────────────────
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(333)   # ~3 fps
        self._plot_timer.timeout.connect(self._on_tick)
        self._plot_timer.start()

        # ── Initial scan ──────────────────────────────────────────────────────
        if self._ble and self._live_discovery:
            self._ble.start_scan()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main toolbar")
        tb.setMovable(False)
        tb.setStyleSheet("QToolBar { border-bottom: 1px solid #e5e7eb; spacing: 6px; }")
        self.addToolBar(tb)

        title = QLabel("  Physics Data Recorder")
        title.setStyleSheet("font-size:14px; font-weight:600;")
        tb.addWidget(title)

        # Spacer
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        # Live Discovery toggle
        self._live_btn = QPushButton("● Live Discovery")
        self._live_btn.setCheckable(True)
        self._live_btn.setChecked(True)
        self._live_btn.setToolTip("Toggle automatic BLE sensor discovery")
        self._live_btn.toggled.connect(self._on_live_discovery_toggled)
        self._live_btn.setStyleSheet(self._live_btn_style(True))
        tb.addWidget(self._live_btn)

        tb.addSeparator()

        # Record / Stop button
        self._record_btn = QPushButton("● Record")
        self._record_btn.setToolTip("Start a recording session")
        self._record_btn.clicked.connect(self._on_record_stop)
        self._record_btn.setEnabled(False)
        tb.addWidget(self._record_btn)

        tb.addSeparator()

        # Download CSV
        self._download_btn = QPushButton("↓ Download CSV")
        self._download_btn.setEnabled(False)
        self._download_btn.setToolTip("Save all recorded and imported data as CSV")
        self._download_btn.clicked.connect(self._on_download)
        tb.addWidget(self._download_btn)

        tb.addWidget(QWidget())   # right padding

    def _live_btn_style(self, active: bool) -> str:
        if active:
            return ("QPushButton { border:1px solid #10b981; border-radius:4px;"
                    " color:#10b981; background:rgba(16,185,129,0.07); padding:4px 10px; }"
                    " QPushButton:hover { background:rgba(16,185,129,0.15); }")
        return ("QPushButton { border:1px solid #d1d5db; border-radius:4px;"
                " color:#9ca3af; background:transparent; padding:4px 10px; }"
                " QPushButton:hover { background:#f3f4f6; }")

    def _build_sidebar(self) -> None:
        sidebar = QWidget()
        sidebar.setFixedWidth(240)
        v = QVBoxLayout(sidebar)
        v.setContentsMargins(0, 4, 0, 4)
        v.setSpacing(0)

        # Sensor panel
        self._sensor_panel = SensorPanel()
        self._sensor_panel.connect_requested.connect(self._on_connect_sensor)
        self._sensor_panel.disconnect_requested.connect(self._on_disconnect_sensor)
        self._sensor_panel.connect_refresh_to(self._on_manual_scan)
        v.addWidget(self._sensor_panel)

        # Divider between panels
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#e5e7eb; margin:4px 8px;")
        v.addWidget(sep)

        # Session panel
        self._session_panel = SessionPanel()
        self._session_panel.remove_session_requested.connect(self._on_remove_session)
        self._session_panel.remove_run_requested.connect(self._on_remove_run)
        self._session_panel.connect_import_to(self._on_import_csv)
        v.addWidget(self._session_panel, 1)

        dock = QDockWidget("Sensors & Data", self)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        dock.setWidget(sidebar)
        dock.setTitleBarWidget(QWidget())  # hide default title bar
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _build_central(self) -> None:
        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        self._plot_panel = PlotPanel()
        v.addWidget(self._plot_panel)

        self.setCentralWidget(central)

    # ── Timer tick ────────────────────────────────────────────────────────────

    def _on_tick(self) -> None:
        """Main update loop: BLE cleanup → flush recording → update plot."""
        if self._ble:
            self._ble.poll_cleanup()

        managed = self._ble.managed_addrs if self._ble else frozenset()
        self._rec_ctrl.flush(managed, lambda a: self._ble.get_label(a) if self._ble else a[-6:])

        self._update_record_btn(managed)
        self._update_download_btn()
        self._maybe_auto_scan()
        self._refresh_plot(managed)

    def _refresh_plot(self, managed: frozenset[str]) -> None:
        # Determine visible units from all data sources.
        units: set[str] = set()
        snapshot = self._live_store.snapshot_all()
        units.update(snapshot.keys())
        for s in self._rec_ctrl.sessions:
            if s.visible:
                units.update(s.data.keys())
        for r in self._imported_runs:
            if r.visible:
                units.update(r.data.keys())

        unit_list = _sorted_units(units)

        # Rebuild subplot grid if units changed.
        current_unit_list = list(self._plot_panel._plots.keys())
        if unit_list != current_unit_list:
            self._plot_panel.rebuild_for_units(unit_list)
            # Nullify expected range so the fresh default ViewBox range is not
            # mistaken for user interaction on this tick.
            self._expected_x_range = None

        if not unit_list:
            return

        # Detect user zoom/pan: compare actual viewport against the range we last
        # set programmatically.  Skip when _expected_x_range is None (just rebuilt
        # or first tick after recording stop).
        if self._expected_x_range is not None:
            current = self._plot_panel.get_x_range()
            if current is not None:
                exp = self._expected_x_range
                if abs(current[0] - exp[0]) > 0.01 or abs(current[1] - exp[1]) > 0.01:
                    # User panned or zoomed: update window width and centre offset.
                    self._live_window_s  = current[1] - current[0]
                    anchor = (self._rec_ctrl.rec_duration
                              if self._rec_ctrl.is_recording else 0.0)
                    self._center_offset  = (current[0] + current[1]) / 2.0 - anchor

        # Build live-trace display labels (addr → "Type ᛒ id") for the legend.
        live_labels: dict[str, str] = {}
        if self._ble:
            for addr in managed:
                meta = self._ble.get_meta(addr)
                if meta:
                    live_labels[addr] = meta.display_label

        # Update curves.
        self._plot_panel.update_curves(
            live_snapshot        = snapshot,
            sessions             = self._rec_ctrl.sessions,
            imported_runs        = self._imported_runs,
            managed_addrs        = managed,
            color_map            = self._color_map,
            is_recording         = self._rec_ctrl.is_recording,
            rec_start_abs        = self._rec_ctrl.rec_start_abs,
            live_window_s        = self._live_window_s,
            in_progress_data     = self._rec_ctrl.in_progress_data,
            in_progress_labels   = self._rec_ctrl.in_progress_labels,
            in_progress_color_id = self._in_progress_color_id,
            live_labels          = live_labels,
        )

        # Set x-axis range whenever there are plots.
        # Formula: [anchor + O − half, anchor + O + half]
        # where anchor = rec_duration while recording, 0 otherwise.
        if self._plot_panel._plots:
            half   = self._live_window_s / 2
            anchor = (self._rec_ctrl.rec_duration
                      if self._rec_ctrl.is_recording else 0.0)
            x_start = anchor + self._center_offset - half
            x_end   = anchor + self._center_offset + half
            self._plot_panel.set_x_range(x_start, x_end)
            self._expected_x_range = (x_start, x_end)

    # ── Toolbar helpers ───────────────────────────────────────────────────────

    def _update_record_btn(self, managed: frozenset[str]) -> None:
        any_connected = any(
            self._ble and self._ble.get_status(a) == "connected"
            for a in managed
        ) if managed else False

        if self._rec_ctrl.is_recording:
            pts = self._rec_ctrl.total_points()
            self._record_btn.setText(f"■  Stop  ({pts} pts)")
            self._record_btn.setStyleSheet(
                "QPushButton { background:#ef4444; color:white; border:none;"
                " border-radius:4px; padding:4px 10px; font-weight:600; }"
                " QPushButton:hover { background:#dc2626; }")
            self._record_btn.setEnabled(True)
        else:
            self._record_btn.setText("● Record")
            self._record_btn.setStyleSheet(
                "QPushButton { border:1px solid #d1d5db; border-radius:4px;"
                " padding:4px 10px; }"
                " QPushButton:hover { background:#f3f4f6; }")
            self._record_btn.setEnabled(any_connected)

    def _update_download_btn(self) -> None:
        has_data = bool(self._rec_ctrl.sessions) or bool(self._imported_runs)
        self._download_btn.setEnabled(has_data)

    # ── Live Discovery auto-scan ───────────────────────────────────────────────

    def _maybe_auto_scan(self) -> None:
        if not self._ble or not self._live_discovery:
            return
        if self._ble.scan_in_progress:
            return
        import time
        if (self._ble.scan_last_t == 0.0 or
                time.time() - self._ble.scan_last_t >= SCAN_INTERVAL_S):
            self._ble.start_scan()

    # ── BLE slots ─────────────────────────────────────────────────────────────

    def _on_scan_started(self) -> None:
        self._sensor_panel.set_scan_status(True, self._ble.scan_last_t)

    def _on_scan_complete(self, results: list) -> None:
        import time
        now = time.time()
        managed = self._ble.managed_addrs if self._ble else frozenset()

        # Merge scan results into metadata cache.
        for meta in results:
            if meta.address not in managed:
                self._sensor_meta[meta.address] = meta

        # Expire sensors not in managed set and not seen recently.
        for addr in list(self._sensor_meta):
            if addr in managed:
                continue
            if not any(m.address == addr for m in results):
                # Remove after missing from two consecutive scan intervals.
                meta = self._sensor_meta[addr]
                last = getattr(meta, "_last_seen", now)
                if now - last > SCAN_INTERVAL_S * 2:
                    del self._sensor_meta[addr]
                    continue
            else:
                self._sensor_meta[addr]._last_seen = now

        self._sensor_panel.set_scan_status(False, self._ble.scan_last_t)
        available = [m for m in self._sensor_meta.values()
                     if m.address not in managed]
        self._sensor_panel.set_available_sensors(available, managed)

    def _on_status_changed(self, addr: str, status: str) -> None:
        meta  = self._ble.get_meta(addr) if self._ble else None
        label = (meta.display_label if meta else
                 self._sensor_meta.get(addr, SensorMeta("?","","",addr)).display_label)
        self._sensor_panel.update_sensor_row(addr, status, label)

        if status in ("connecting…", "removed"):
            # Refresh the dropdown whenever a sensor enters or leaves the managed
            # set so it never appears in both the dropdown and the connected list.
            managed  = self._ble.managed_addrs if self._ble else frozenset()
            available = [m for m in self._sensor_meta.values()
                         if m.address not in managed]
            self._sensor_panel.set_available_sensors(available, managed)

    def _on_unit_discovered(self, addr: str, unit: str) -> None:
        pass  # plot rebuilds automatically on next tick

    # ── Sensor connect / disconnect ────────────────────────────────────────────

    def _on_connect_sensor(self, meta: SensorMeta) -> None:
        if self._ble:
            self._ble.connect_sensor(meta)

    def _on_disconnect_sensor(self, addr: str) -> None:
        if self._ble:
            self._ble.disconnect_sensor(addr)

    def _on_manual_scan(self) -> None:
        if self._ble:
            self._ble.start_scan()

    # ── Live Discovery toggle ─────────────────────────────────────────────────

    def _on_live_discovery_toggled(self, checked: bool) -> None:
        self._live_discovery = checked
        self._live_btn.setStyleSheet(self._live_btn_style(checked))
        if checked and self._ble:
            self._ble.start_scan()

    # ── Record / Stop ─────────────────────────────────────────────────────────

    def _on_record_stop(self) -> None:
        if self._rec_ctrl.is_recording:
            # Capture duration before stop() clears the in-progress buffers.
            final_dur = self._rec_ctrl.rec_duration
            session   = self._rec_ctrl.stop()
            if session:
                # Transfer the pre-assigned color to the finalised session id.
                cid = self._in_progress_color_id or "rec_in_progress"
                self._color_map[session.id] = self._color_map.pop(cid, FILE_COLORS[0])
                self._in_progress_color_id  = None
                self._session_panel.add_entry(
                    session.id, session.label,
                    on_toggle=self._on_toggle_session,
                )
                # Zoom to show the full recording plus live-data space.
                # left_part = space for the recording (right of x=0)
                # right_part = space for new live data (left of x=0)
                left_part  = max(final_dur, LIVE_WINDOW_S / 2.0)
                right_part = max(final_dur / 4.0, LIVE_WINDOW_S / 2.0)
                self._live_window_s    = left_part + right_part
                self._center_offset    = (left_part - right_part) / 2.0
                # Skip interaction detection on the next tick; the viewport still
                # shows the old recording range which must not override the zoom.
                self._expected_x_range = None
            else:
                # No data captured — discard the reserved color slot.
                self._color_map.pop(self._in_progress_color_id or "", None)
                self._in_progress_color_id = None
            self._update_download_btn()
        else:
            # Pre-assign the color this session will use when finalised.
            self._in_progress_color_id = "rec_in_progress"
            self._assign_color(self._in_progress_color_id)
            # Reset offset so recording starts with the anchor at viewport centre.
            self._center_offset    = 0.0
            self._expected_x_range = None
            self._rec_ctrl.start()

    def _on_toggle_session(self, session_id: str, visible: bool) -> None:
        for s in self._rec_ctrl.sessions:
            if s.id == session_id:
                s.visible = visible
                break

    def _on_remove_session(self, session_id: str) -> None:
        self._rec_ctrl.remove_session(session_id)
        self._color_map.pop(session_id, None)
        self._update_download_btn()

    # ── CSV import ────────────────────────────────────────────────────────────

    def _on_import_csv(self) -> None:
        path = self._session_panel.pending_import_path()
        self._session_panel.clear_pending_import()
        if not path:
            return
        try:
            runs = _parse_pasco_csv(path)
        except Exception as exc:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "CSV import failed", str(exc))
            return
        for run in runs:
            self._imported_runs.append(run)
            self._assign_color(run.id)
            self._session_panel.add_entry(
                run.id, run.label,
                on_toggle=self._on_toggle_run,
            )
        self._update_download_btn()

    def _on_toggle_run(self, run_id: str, visible: bool) -> None:
        for r in self._imported_runs:
            if r.id == run_id:
                r.visible = visible
                break

    def _on_remove_run(self, run_id: str) -> None:
        self._imported_runs = [r for r in self._imported_runs if r.id != run_id]
        self._color_map.pop(run_id, None)
        self._update_download_btn()

    # ── CSV download ──────────────────────────────────────────────────────────

    def _on_download(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV", "recording.csv", "CSV files (*.csv)")
        if not path:
            return
        data = self._rec_ctrl.build_csv(self._imported_runs)
        Path(path).write_bytes(data)

    # ── Color assignment ──────────────────────────────────────────────────────

    def _assign_color(self, entry_id: str) -> None:
        if entry_id not in self._color_map:
            self._color_map[entry_id] = FILE_COLORS[
                self._color_idx % len(FILE_COLORS)]
            self._color_idx += 1


# ─────────────────────────────────────────────────────────────────────────────
# CSV parsing helper
# ─────────────────────────────────────────────────────────────────────────────

def _parse_pasco_csv(path: str) -> list[ImportedRun]:
    """Parse a Pasco/SPARKvue CSV file and return one ImportedRun per run."""
    import pandas as pd

    df = pd.read_csv(path, low_memory=False)

    series_meta: dict[int, dict] = {}   # run_num → {unit, name, time_col, value_col}
    for col in df.columns:
        col = col.strip()
        if _TIME_RE.match(col):
            continue
        m = _MEAS_RE.match(col)
        if not m:
            continue
        name, unit, run_str = m.group(1).strip(), m.group(2).strip(), int(m.group(3))
        time_col = next(
            (c.strip() for c in df.columns
             if _TIME_RE.match(c.strip()) and
             _TIME_RE.match(c.strip()).group(2) == str(run_str)),
            None,
        )
        series_meta[run_str] = {
            "name":      name,
            "unit":      unit,
            "time_col":  time_col,
            "value_col": col,
        }

    runs: list[ImportedRun] = []
    for run_num in sorted(series_meta):
        meta      = series_meta[run_num]
        time_col  = meta["time_col"]
        value_col = meta["value_col"]
        unit      = meta["unit"]

        if time_col is None:
            continue

        t = pd.to_numeric(df[time_col],  errors="coerce")
        v = pd.to_numeric(df[value_col], errors="coerce")
        mask = t.notna() & v.notna()

        run = ImportedRun(
            label   = f"Run {run_num}",
            run_num = run_num,
            data    = {unit: {"times": t[mask].tolist(), "values": v[mask].tolist()}},
        )
        runs.append(run)
    return runs
