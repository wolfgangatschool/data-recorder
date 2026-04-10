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
from PyQt6.QtGui import QColor, QFont, QPalette
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDockWidget, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QToolBar, QVBoxLayout, QWidget, QComboBox,
)

from data_store import (
    LIVE_BUFFER_MAXLEN, LIVE_WINDOW_S,
    FitResult, ImportedRun, LiveStore, RecordingSession, SensorMeta,
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
# Theme / palette helpers
# ─────────────────────────────────────────────────────────────────────────────

class _Palette:
    """Semantic color tokens derived from the current Qt palette.

    Call _Palette.refresh() whenever the system theme may have changed.
    All stylesheet helpers read from the singleton so the rest of the code
    never has to branch on dark vs. light.
    """

    # Accent / status colors are the same in both themes.
    BLUE        = "#3b82f6"
    GREEN       = "#10b981"
    GREEN_DARK  = "#059669"
    RED         = "#ef4444"
    RED_DARK    = "#dc2626"
    VIOLET      = "#8b5cf6"

    # Semantic tokens — set by refresh()
    is_dark         : bool  = True

    # Text
    text_primary    : str = "#e5e7eb"
    text_secondary  : str = "#9ca3af"
    text_muted      : str = "#6b7280"
    text_on_accent  : str = "#ffffff"

    # Surfaces
    bg_window       : str = "#1e1e1e"
    bg_base         : str = "#171717"
    bg_input        : str = "#262626"

    # Borders / dividers
    border          : str = "#374151"
    divider         : str = "#374151"

    # Interactive elements (neutral / unfocused)
    btn_border      : str = "#4b5563"
    btn_text        : str = "#9ca3af"
    btn_hover_bg    : str = "#2d2d2d"

    # Hover tints for danger/safe actions
    hover_danger    : str = "#450a0a"
    hover_safe      : str = "#052e16"

    @classmethod
    def refresh(cls) -> None:
        """Read the live Qt palette and update all tokens."""
        app = QApplication.instance()
        if app is None:
            return
        win_color = app.palette().color(QPalette.ColorRole.Window)
        cls.is_dark = win_color.lightness() < 128

        if cls.is_dark:
            cls.text_primary   = "#e5e7eb"
            cls.text_secondary = "#9ca3af"
            cls.text_muted     = "#6b7280"
            cls.text_on_accent = "#ffffff"
            cls.bg_window      = win_color.name()
            cls.bg_base        = app.palette().color(QPalette.ColorRole.Base).name()
            cls.bg_input       = "#262626"
            cls.border         = "#374151"
            cls.divider        = "#374151"
            cls.btn_border     = "#4b5563"
            cls.btn_text       = "#9ca3af"
            cls.btn_hover_bg   = "#2d2d2d"
            cls.hover_danger   = "#450a0a"
            cls.hover_safe     = "#052e16"
        else:
            cls.text_primary   = "#111827"
            cls.text_secondary = "#374151"
            cls.text_muted     = "#6b7280"
            cls.text_on_accent = "#ffffff"
            cls.bg_window      = win_color.name()
            cls.bg_base        = app.palette().color(QPalette.ColorRole.Base).name()
            cls.bg_input       = "#f9fafb"
            cls.border         = "#e5e7eb"
            cls.divider        = "#e5e7eb"
            cls.btn_border     = "#d1d5db"
            cls.btn_text       = "#374151"
            cls.btn_hover_bg   = "#f3f4f6"
            cls.hover_danger   = "#fee2e2"
            cls.hover_safe     = "#d1fae5"

    # ── Stylesheet fragments ──────────────────────────────────────────────────

    @classmethod
    def section_title(cls) -> str:
        return (f"font-size:10px; font-weight:600; color:{cls.text_muted};"
                " letter-spacing:1px;")

    @classmethod
    def label_secondary(cls) -> str:
        return f"font-size:10px; color:{cls.text_muted};"

    @classmethod
    def label_primary(cls) -> str:
        return f"font-size:10px; color:{cls.text_primary};"

    @classmethod
    def label_bold(cls) -> str:
        return f"font-size:10px; font-weight:600; color:{cls.text_primary};"

    @classmethod
    def label_error(cls) -> str:
        return f"font-size:10px; color:{cls.RED};"

    @classmethod
    def label_monospace(cls) -> str:
        return (f"font-size:10px; font-family:Menlo,Monaco,'Courier New';"
                f" color:{cls.text_primary};")

    @classmethod
    def divider_style(cls) -> str:
        return f"color:{cls.divider};"

    @classmethod
    def divider_margin(cls) -> str:
        return f"color:{cls.divider}; margin:4px 8px;"

    @classmethod
    def toolbar_style(cls) -> str:
        return f"QToolBar {{ border-bottom:1px solid {cls.border}; spacing:6px; }}"

    @classmethod
    def scroll_area(cls) -> str:
        return "QScrollArea { border: none; }"

    @classmethod
    def neutral_btn(cls) -> str:
        return (f"QPushButton {{ border:1px solid {cls.btn_border}; border-radius:4px;"
                f" color:{cls.btn_text}; background:transparent; padding:4px 10px; }}"
                f" QPushButton:hover {{ background:{cls.btn_hover_bg}; }}")

    @classmethod
    def neutral_btn_small(cls) -> str:
        return (f"QPushButton {{ border:1px solid {cls.btn_border}; border-radius:4px;"
                f" color:{cls.btn_text}; background:transparent; padding:2px 6px;"
                f" font-size:10px; }}"
                f" QPushButton:hover {{ background:{cls.btn_hover_bg}; }}")

    @classmethod
    def danger_btn_small(cls) -> str:
        return (f"QPushButton {{ border:1px solid {cls.btn_border}; border-radius:4px;"
                f" color:{cls.RED}; background:transparent; padding:2px 6px;"
                f" font-size:10px; }}"
                f" QPushButton:hover {{ background:{cls.hover_danger}; }}")

    @classmethod
    def accent_btn_small(cls) -> str:
        return (f"QPushButton {{ border:1px solid {cls.BLUE}; border-radius:4px;"
                f" color:{cls.BLUE}; background:transparent; padding:2px 6px;"
                f" font-size:10px; }}"
                f" QPushButton:hover {{ background:rgba(59,130,246,0.12); }}")

    @classmethod
    def active_live_btn(cls) -> str:
        return (f"QPushButton {{ border:1px solid {cls.GREEN}; border-radius:4px;"
                f" color:{cls.GREEN}; background:rgba(16,185,129,0.07); padding:4px 10px; }}"
                f" QPushButton:hover {{ background:rgba(16,185,129,0.15); }}")

    @classmethod
    def active_fit_btn(cls) -> str:
        return (f"QPushButton {{ border:1px solid {cls.VIOLET}; border-radius:4px;"
                f" color:{cls.VIOLET}; background:rgba(139,92,246,0.07); padding:4px 10px; }}"
                f" QPushButton:hover {{ background:rgba(139,92,246,0.15); }}")

    @classmethod
    def fit_btn_neutral(cls) -> str:
        return (f"QPushButton {{ border:1px solid {cls.BLUE}; border-radius:4px;"
                f" color:{cls.BLUE}; background:transparent; padding:3px 10px; }}"
                f" QPushButton:hover {{ background:rgba(59,130,246,0.12); }}")

    @classmethod
    def fit_btn_ok(cls) -> str:
        return (f"QPushButton {{ border:none; border-radius:4px;"
                f" color:{cls.text_on_accent}; background:{cls.GREEN}; padding:3px 10px; }}"
                f" QPushButton:hover {{ background:{cls.GREEN_DARK}; }}")

    @classmethod
    def fit_btn_err(cls) -> str:
        return (f"QPushButton {{ border:none; border-radius:4px;"
                f" color:{cls.text_on_accent}; background:{cls.RED}; padding:3px 10px; }}"
                f" QPushButton:hover {{ background:{cls.RED_DARK}; }}")

    @classmethod
    def input_field(cls) -> str:
        return (f"font-size:11px; font-family:Menlo,Monaco,'Courier New';"
                f" padding:2px 4px;"
                f" background:{cls.bg_input}; color:{cls.text_primary};"
                f" border:1px solid {cls.border}; border-radius:3px;")

    @classmethod
    def small_field(cls) -> str:
        return (f"font-size:9px; padding:1px 2px;"
                f" background:{cls.bg_input}; color:{cls.text_primary};"
                f" border:1px solid {cls.border}; border-radius:2px;")


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

    fit_region_changed = pyqtSignal(float, float)   # t_min, t_max

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

        # Fit-region state
        self._fit_active: bool = False
        self._fit_bounds: tuple[float, float] = (0.0, 10.0)
        self._fit_regions: list[tuple[pg.PlotItem, object]] = []  # (plot, region)

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

        if self._fit_active:
            self._rebuild_fit_regions()

    # ── Crosshair ─────────────────────────────────────────────────────────────

    def _on_mouse_moved(self, evt) -> None:
        pos = evt[0]
        for plot, vl in zip(self._plots.values(), self._vlines):
            if plot.sceneBoundingRect().contains(pos):
                x = plot.vb.mapSceneToView(pos).x()
                for v in self._vlines:
                    v.setPos(x)
                break

    # ── Fit region ────────────────────────────────────────────────────────────

    def show_fit_region(self, t_min: float, t_max: float) -> None:
        """Show (or move) the selection band on all subplots."""
        self._fit_active = True
        self._fit_bounds = (t_min, t_max)
        self._rebuild_fit_regions()

    def hide_fit_region(self) -> None:
        """Remove the selection band from all subplots."""
        self._fit_active = False
        self._remove_fit_regions()

    def _rebuild_fit_regions(self) -> None:
        self._remove_fit_regions()
        t_min, t_max = self._fit_bounds
        for p in self._plots.values():
            region = pg.LinearRegionItem(
                values=[t_min, t_max],
                brush=pg.mkBrush(100, 150, 255, 25),
                pen=pg.mkPen((100, 150, 255, 180), width=1),
            )
            region.sigRegionChanged.connect(
                lambda _, r=region: self._on_region_moved(r))
            p.addItem(region)
            self._fit_regions.append((p, region))

    def _remove_fit_regions(self) -> None:
        for plot, region in self._fit_regions:
            try:
                plot.removeItem(region)
            except Exception:
                pass
        self._fit_regions.clear()

    def _on_region_moved(self, source: object) -> None:
        t_min, t_max = source.getRegion()
        self._fit_bounds = (t_min, t_max)
        for _, region in self._fit_regions:
            if region is not source:
                region.blockSignals(True)
                region.setRegion([t_min, t_max])
                region.blockSignals(False)
        self.fit_region_changed.emit(t_min, t_max)

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
        live_left_s:          float | None = None,
        in_progress_data:     dict | None = None,
        in_progress_labels:   dict[str, str] | None = None,
        in_progress_color_id: str | None = None,
        live_labels:          dict[str, str] | None = None,
        fit_results:          list | None = None,
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
        live_left_s          — max seconds of live history shown left of anchor;
                               defaults to live_window_s/2 when None
        in_progress_data     — {unit: {addr: [(t_rebased, v)]}} for the active recording
        in_progress_labels   — {addr: label} for sensors in the active recording
        in_progress_color_id — color_map key for the in-progress session
        live_labels          — {addr: display_label} for live-trace legend entries
        """
        active_keys: set[str] = set()
        half = live_window_s / 2
        left_s = live_left_s if live_left_s is not None else half

        def _symbol_brush(color):
            """Filled circle brush matching the line color, no outline."""
            return pg.mkBrush(color)

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
                           if t >= rec_start_abs - left_s and t < rec_start_abs]
                else:
                    # Re-base so newest sample → 0 (centre of viewport).
                    t_newest = pts[-1][0]
                    pts = [(t - t_newest, v) for t, v in pts
                           if t >= t_newest - left_s]

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
                        xs, ys, pen=pen, name=lv_lbl,
                        symbol='o', symbolSize=4,
                        symbolBrush=_symbol_brush(gray),
                        symbolPen=pg.mkPen(None))
                    self._curves[key].setDownsampling(auto=True)
                    self._curves[key].setClipToView(True)
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
                            xs, ys, pen=pen, name=lbl,
                            symbol='o', symbolSize=4,
                            symbolBrush=_symbol_brush(color),
                            symbolPen=pg.mkPen(None))
                        self._curves[key].setDownsampling(auto=True)
                        self._curves[key].setClipToView(True)
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
                            xs, ys, pen=pen, name=trace_name,
                            symbol='o', symbolSize=4,
                            symbolBrush=_symbol_brush(color),
                            symbolPen=pg.mkPen(None))
                        self._curves[key].setDownsampling(auto=True)
                        self._curves[key].setClipToView(True)
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
                        xs, ys, pen=pen, name=run.label,
                        symbol='o', symbolSize=4,
                        symbolBrush=_symbol_brush(color),
                        symbolPen=pg.mkPen(None))
                    self._curves[key].setDownsampling(auto=True)
                    self._curves[key].setClipToView(True)
                else:
                    self._curves[key].setData(xs, ys)
                    self._curves[key].setPen(pen)

        # ── Fit result curves (dashed vivid lines) ────────────────────────────
        for fit in (fit_results or []):
            if not fit.visible:
                continue
            if fit.unit not in self._plots:
                continue
            color = color_map.get(fit.id, FILE_COLORS[0])
            pen   = pg.mkPen(color=color, width=2,
                             style=Qt.PenStyle.DashLine)
            key   = f"fit|{fit.unit}|{fit.id}"
            active_keys.add(key)
            xs, ys = fit.t_array, fit.v_array
            if key not in self._curves:
                self._curves[key] = self._plots[fit.unit].plot(
                    xs, ys, pen=pen, name=fit.label)
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
        title.setStyleSheet(_Palette.section_title())
        layout.addWidget(title)

        # Scan status + refresh row
        scan_row = QHBoxLayout()
        self._scan_label = QLabel("No scan yet")
        self._scan_label.setStyleSheet(_Palette.label_secondary())
        scan_row.addWidget(self._scan_label, 1)
        self._refresh_btn = QPushButton("↺")
        self._refresh_btn.setFixedWidth(30)
        self._refresh_btn.setToolTip("Scan for sensors now")
        self._refresh_btn.setStyleSheet(_Palette.neutral_btn_small())
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
        self._connect_btn.setStyleSheet(_Palette.neutral_btn_small())
        dropdown_row.addWidget(self._connect_btn)
        layout.addLayout(dropdown_row)

        # Divider
        self._divider = QFrame()
        self._divider.setFrameShape(QFrame.Shape.HLine)
        self._divider.setStyleSheet(_Palette.divider_style())
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
        status_lbl.setStyleSheet(_Palette.label_secondary())
        h.addWidget(status_lbl)

        disc_btn = QPushButton("－")
        disc_btn.setObjectName(f"disc_{addr}")
        disc_btn.setFixedWidth(26)
        disc_btn.setToolTip("Disconnect")
        disc_btn.setStyleSheet(_Palette.danger_btn_small())
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
# RatePanel
# ─────────────────────────────────────────────────────────────────────────────

# Full rate step list: polling (≤ 20 Hz via one-shot) + push (> 20 Hz via
# continuous notifications).  All steps are now active; the strategy is
# selected automatically in sensor_stream.py based on the configured rate.
_RATE_STEPS: list[float] = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000]
_RATE_DEFAULT: float = 20.0

def _fmt_rate(hz: float) -> str:
    return f"{int(hz / 1000)}k Hz" if hz >= 1000 else f"{int(hz)} Hz"


class RatePanel(QWidget):
    """Sampling-rate step control.

    Steps ≤ 20 Hz use one-shot BLE polling (PollStream).
    Steps > 20 Hz use continuous push notifications (PushStream).
    All steps are active; the strategy is selected automatically.

    Emits rate_changed(float) whenever the user changes the step.
    """

    rate_changed = pyqtSignal(float)   # new rate in Hz

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        title = QLabel("SAMPLE RATE")
        title.setStyleSheet(_Palette.section_title())
        layout.addWidget(title)

        # ◀ label ▶ row
        row = QHBoxLayout()
        row.setSpacing(4)

        self._prev_btn = QPushButton("◀")
        self._prev_btn.setFixedWidth(28)
        self._prev_btn.setStyleSheet(_Palette.neutral_btn_small())
        row.addWidget(self._prev_btn)

        self._rate_lbl = QLabel(_fmt_rate(_RATE_DEFAULT))
        self._rate_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rate_lbl.setStyleSheet(f"font-size:12px; font-weight:600; color:{_Palette.text_primary};")
        row.addWidget(self._rate_lbl, 1)

        self._next_btn = QPushButton("▶")
        self._next_btn.setFixedWidth(28)
        self._next_btn.setStyleSheet(_Palette.neutral_btn_small())
        row.addWidget(self._next_btn)

        layout.addLayout(row)

        self._idx = _RATE_STEPS.index(_RATE_DEFAULT)
        self._update_buttons()

        self._prev_btn.clicked.connect(self._on_prev)
        self._next_btn.clicked.connect(self._on_next)

    @property
    def current_rate(self) -> float:
        return _RATE_STEPS[self._idx]

    def _on_prev(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._apply()

    def _on_next(self) -> None:
        if self._idx < len(_RATE_STEPS) - 1:
            self._idx += 1
            self._apply()

    def _apply(self) -> None:
        self._update_buttons()
        self.rate_changed.emit(_RATE_STEPS[self._idx])

    def _update_buttons(self) -> None:
        self._rate_lbl.setText(_fmt_rate(_RATE_STEPS[self._idx]))
        self._prev_btn.setEnabled(self._idx > 0)
        self._next_btn.setEnabled(self._idx < len(_RATE_STEPS) - 1)


# ─────────────────────────────────────────────────────────────────────────────
# SessionPanel
# ─────────────────────────────────────────────────────────────────────────────

class SessionPanel(QWidget):
    """CSV import, recorded sessions, and imported-run visibility toggles."""

    remove_session_requested = pyqtSignal(str)   # session_id
    remove_run_requested     = pyqtSignal(str)   # run_id
    remove_fit_requested     = pyqtSignal(str)   # fit_id

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        # Section title + import button row
        top_row = QHBoxLayout()
        title = QLabel("MEASUREMENTS")
        title.setStyleSheet(_Palette.section_title())
        top_row.addWidget(title, 1)
        self._import_btn = QPushButton("Load CSV…")
        self._import_btn.setStyleSheet(_Palette.neutral_btn_small())
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
        self._external_import_slot = None

        self._import_btn.clicked.connect(self._on_import)

    # ── External API ──────────────────────────────────────────────────────────

    def add_entry(self, entry_id: str, label: str, on_toggle,
                  visible: bool = True, on_edit=None) -> None:
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

        if on_edit is not None:
            edit_btn = QPushButton("✏")
            edit_btn.setFixedWidth(22)
            edit_btn.setToolTip("Edit fit")
            edit_btn.setStyleSheet(_Palette.accent_btn_small())
            edit_btn.clicked.connect(lambda _, eid=entry_id: on_edit(eid))
            h.addWidget(edit_btn)

        remove_btn = QPushButton("－")
        remove_btn.setFixedWidth(22)
        remove_btn.setStyleSheet(_Palette.danger_btn_small())
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
        if entry_id.startswith("fit_"):
            self.remove_fit_requested.emit(entry_id)
        elif entry_id.startswith("csv_"):
            self.remove_run_requested.emit(entry_id)
        else:
            self.remove_session_requested.emit(entry_id)
        self.remove_entry(entry_id)

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load CSV", "", "CSV files (*.csv);;All files (*)")
        if path:
            self._import_btn.setProperty("pending_path", path)
            if self._external_import_slot:
                self._external_import_slot()

    def connect_import_to(self, slot) -> None:
        self._external_import_slot = slot

    def pending_import_path(self) -> str | None:
        return self._import_btn.property("pending_path")

    def clear_pending_import(self) -> None:
        self._import_btn.setProperty("pending_path", None)


# ─────────────────────────────────────────────────────────────────────────────
# FitPanel
# ─────────────────────────────────────────────────────────────────────────────

class ParamSliderRow(QWidget):
    """One row in the FitPanel parameter display: name, value, ±error, and a slider.

    The slider range defaults to ±10× the fitted value magnitude.  The user
    can narrow or widen the range by editing the min/max fields; changes are
    applied immediately when the field loses focus or Enter is pressed.
    """

    value_changed = pyqtSignal(str, float)   # param_name, new_value

    def __init__(self, name: str, value: float, error: float, parent=None) -> None:
        super().__init__(parent)
        self._name  = name
        self._value = value

        mag = max(abs(value), 1e-6)
        self._min_val = value - 10.0 * mag
        self._max_val = value + 10.0 * mag

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 3, 0, 1)
        outer.setSpacing(1)

        # ── Top row: name (left) + value (right) ─────────────────────────────
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(_Palette.label_bold())
        self._val_lbl = QLabel(f"{value:.5g}")
        self._val_lbl.setStyleSheet(_Palette.label_monospace())
        self._val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(name_lbl)
        top.addWidget(self._val_lbl)
        outer.addLayout(top)

        # ── Error label ───────────────────────────────────────────────────────
        import math
        if not math.isnan(error):
            err_lbl = QLabel(f"± {error:.2g}")
            err_lbl.setStyleSheet(_Palette.label_secondary())
            outer.addWidget(err_lbl)

        # ── Slider row: [min] ────slider──── [max] ────────────────────────────
        slider_row = QHBoxLayout()
        slider_row.setContentsMargins(0, 0, 0, 0)
        slider_row.setSpacing(3)

        self._min_edit = QLineEdit(f"{self._min_val:.3g}")
        self._min_edit.setFixedWidth(52)
        self._min_edit.setStyleSheet(_Palette.small_field())
        self._min_edit.editingFinished.connect(self._on_range_changed)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 1000)
        self._slider.setValue(self._to_pos(value))
        self._slider.valueChanged.connect(self._on_slider_moved)

        self._max_edit = QLineEdit(f"{self._max_val:.3g}")
        self._max_edit.setFixedWidth(52)
        self._max_edit.setStyleSheet(_Palette.small_field())
        self._max_edit.editingFinished.connect(self._on_range_changed)

        slider_row.addWidget(self._min_edit)
        slider_row.addWidget(self._slider)
        slider_row.addWidget(self._max_edit)
        outer.addLayout(slider_row)

    # ── Public ────────────────────────────────────────────────────────────────

    @property
    def param_name(self) -> str:
        return self._name

    def current_value(self) -> float:
        return self._value

    # ── Internal ──────────────────────────────────────────────────────────────

    def _to_pos(self, v: float) -> int:
        rng = self._max_val - self._min_val
        if rng == 0.0:
            return 500
        return max(0, min(1000, int((v - self._min_val) / rng * 1000)))

    def _to_value(self, pos: int) -> float:
        return self._min_val + pos / 1000.0 * (self._max_val - self._min_val)

    def _on_slider_moved(self, pos: int) -> None:
        self._value = self._to_value(pos)
        self._val_lbl.setText(f"{self._value:.5g}")
        self.value_changed.emit(self._name, self._value)

    def _on_range_changed(self) -> None:
        try:
            self._min_val = float(self._min_edit.text())
        except ValueError:
            self._min_edit.setText(f"{self._min_val:.3g}")
        try:
            self._max_val = float(self._max_edit.text())
        except ValueError:
            self._max_edit.setText(f"{self._max_val:.3g}")
        # Reposition slider for current value without emitting
        self._slider.blockSignals(True)
        self._slider.setValue(self._to_pos(self._value))
        self._slider.blockSignals(False)


# ─────────────────────────────────────────────────────────────────────────────
# FitPanel
# ─────────────────────────────────────────────────────────────────────────────

class FitPanel(QWidget):
    """Curve-fitting controls: trace selector, formula field, fit button, results.

    Emits fit_requested(source_id, unit, expr_str) when the user confirms.
    The caller is responsible for reading current_region() to get the time
    window and for calling set_region() whenever the plot region changes.
    """

    fit_requested = pyqtSignal(str, str, str)   # source_id, unit, expr_str

    @property
    def _BTN_NEUTRAL(self): return _Palette.fit_btn_neutral()
    @property
    def _BTN_OK(self):      return _Palette.fit_btn_ok()
    @property
    def _BTN_ERR(self):     return _Palette.fit_btn_err()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 4, 8, 4)
        self._layout.setSpacing(4)

        title = QLabel("CURVE FIT")
        title.setStyleSheet(_Palette.section_title())
        self._layout.addWidget(title)

        # Trace selector
        self._trace_combo = QComboBox()
        self._trace_combo.setSizePolicy(QSizePolicy.Policy.Expanding,
                                        QSizePolicy.Policy.Fixed)
        self._layout.addWidget(self._trace_combo)

        # Formula input — resets button to neutral on every edit
        self._formula_edit = QLineEdit()
        self._formula_edit.setPlaceholderText("e.g. A*(1-exp(-k*t))")
        self._formula_edit.setStyleSheet(_Palette.input_field())
        self._formula_edit.returnPressed.connect(self._on_fit)
        self._formula_edit.textChanged.connect(self._on_formula_changed)
        self._layout.addWidget(self._formula_edit)

        # Fit button (doubles as status indicator)
        self._fit_btn = QPushButton("Fit")
        self._fit_btn.setStyleSheet(self._BTN_NEUTRAL)
        self._fit_btn.clicked.connect(self._on_fit)
        self._layout.addWidget(self._fit_btn)

        # Separator shown when results or error are displayed
        self._status_sep = QFrame()
        self._status_sep.setFrameShape(QFrame.Shape.HLine)
        self._status_sep.setStyleSheet(_Palette.divider_style())
        self._status_sep.hide()
        self._layout.addWidget(self._status_sep)

        # Error label (hidden while showing param sliders)
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._status_lbl.hide()
        self._layout.addWidget(self._status_lbl)

        # Internal state
        self._traces: list[tuple[str, str, str]] = []
        self._t_min: float = 0.0
        self._t_max: float = 10.0
        self._editing_fit_id: str | None = None
        self._param_rows: list[ParamSliderRow] = []
        self._params_changed_cb = None   # callable(params: dict) | None

    # ── External API ──────────────────────────────────────────────────────────

    def set_traces(self, traces: list[tuple[str, str, str]]) -> None:
        """Populate the trace dropdown: [(source_id, unit, display_name)]."""
        prev_key = self._current_key()
        self._traces = traces
        self._trace_combo.blockSignals(True)
        self._trace_combo.clear()
        restore_idx = 0
        for i, (sid, unit, name) in enumerate(traces):
            self._trace_combo.addItem(name)
            if prev_key and (sid, unit) == prev_key:
                restore_idx = i
        if traces:
            self._trace_combo.setCurrentIndex(restore_idx)
        self._trace_combo.blockSignals(False)

    def set_region(self, t_min: float, t_max: float) -> None:
        self._t_min = t_min
        self._t_max = t_max

    def current_region(self) -> tuple[float, float]:
        return (self._t_min, self._t_max)

    def set_params_changed_cb(self, cb) -> None:
        """Set a callable(params: dict) called whenever a slider moves."""
        self._params_changed_cb = cb

    def set_result(self, params: dict, errors: dict) -> None:
        self._fit_btn.setText("✓ Fitted")
        self._fit_btn.setStyleSheet(self._BTN_OK)
        self._status_lbl.hide()
        self._clear_param_rows()
        if params:
            self._status_sep.show()
            for name, val in params.items():
                err = errors.get(name, float('nan'))
                row = ParamSliderRow(name, val, err)
                row.value_changed.connect(self._on_param_value_changed)
                self._layout.addWidget(row)
                self._param_rows.append(row)
        else:
            self._status_sep.hide()

    def set_error(self, msg: str) -> None:
        self._fit_btn.setText("✗ Error")
        self._fit_btn.setStyleSheet(self._BTN_ERR)
        self._clear_param_rows()
        self._status_lbl.setStyleSheet(_Palette.label_error())
        self._status_lbl.setText(msg)
        self._status_sep.show()
        self._status_lbl.show()

    def _clear_param_rows(self) -> None:
        for row in self._param_rows:
            self._layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._param_rows.clear()

    def _on_param_value_changed(self, _name: str, _value: float) -> None:
        if self._params_changed_cb:
            params = {row.param_name: row.current_value() for row in self._param_rows}
            self._params_changed_cb(params)

    def _on_formula_changed(self) -> None:
        """Reset button to neutral and clear param rows whenever the formula changes."""
        self._fit_btn.setText("Fit")
        self._fit_btn.setStyleSheet(self._BTN_NEUTRAL)
        self._clear_param_rows()
        self._status_sep.hide()
        self._status_lbl.hide()

    def populate_for_edit(self, fit: FitResult) -> None:
        """Pre-fill the panel to edit an existing fit."""
        self._editing_fit_id = fit.id
        # blockSignals so textChanged doesn't reset the button while populating
        self._formula_edit.blockSignals(True)
        self._formula_edit.setText(fit.expr_str)
        self._formula_edit.blockSignals(False)
        self.set_region(fit.t_min, fit.t_max)
        for i, (sid, unit, _) in enumerate(self._traces):
            if sid == fit.source_id and unit == fit.unit:
                self._trace_combo.setCurrentIndex(i)
                break
        self.set_result(fit.params, fit.param_errors)

    def clear_edit_state(self) -> None:
        self._editing_fit_id = None

    @property
    def editing_fit_id(self) -> str | None:
        return self._editing_fit_id

    # ── Internal ──────────────────────────────────────────────────────────────

    def _current_key(self) -> tuple[str, str] | None:
        idx = self._trace_combo.currentIndex()
        if 0 <= idx < len(self._traces):
            sid, unit, _ = self._traces[idx]
            return (sid, unit)
        return None

    def _on_fit(self) -> None:
        key = self._current_key()
        if key is None:
            self.set_error("No trace selected.")
            return
        expr = self._formula_edit.text().strip()
        if not expr:
            self.set_error("Enter a formula.")
            return
        self._fit_btn.setText("Fitting…")
        self._fit_btn.setStyleSheet(self._BTN_NEUTRAL)
        source_id, unit = key
        self.fit_requested.emit(source_id, unit, expr)


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

        # ── Curve fit results ─────────────────────────────────────────────────
        self._fit_results:  list[FitResult] = []
        self._fit_counter:  int  = 0
        self._fit_mode:     bool = False
        # Active fit function — updated in _do_fit / _rebuild_active_fit_fn
        # so slider changes can recompute the curve without re-running scipy.
        self._active_fit_fn          = None    # callable(t, *params) → np.array
        self._active_fit_param_names: list[str] = []
        self._active_fit_id:          str | None = None

        # ── Stable colour assignment for sessions and runs ─────────────────────
        self._color_idx: int = 0
        self._color_map: dict[str, tuple[int, int, int]] = {}

        # ── Live Discovery state ───────────────────────────────────────────────
        self._live_discovery: bool = True

        # ── In-progress recording color ───────────────────────────────────────
        # Set to "rec_in_progress" when recording starts; transferred to the
        # session id on Stop so the color stays stable across the transition.
        self._in_progress_color_id: str | None = None

        # ── Current sample rate ────────────────────────────────────────────────
        # Kept in sync with RatePanel; used to cap the live look-back window.
        self._sample_rate_hz: float = _RATE_DEFAULT

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
        _Palette.refresh()          # read current system palette before building
        self._build_toolbar()
        self._build_sidebar()
        self._build_central()

        # Re-apply theme when system switches dark ↔ light.
        QApplication.instance().paletteChanged.connect(self._on_palette_changed)

        # ── Timers ────────────────────────────────────────────────────────────
        self._plot_timer = QTimer(self)
        self._plot_timer.setInterval(333)   # ~3 fps
        self._plot_timer.timeout.connect(self._on_tick)
        self._plot_timer.start()

        # ── Initial scan ──────────────────────────────────────────────────────
        if self._ble and self._live_discovery:
            self._ble.start_scan()

    # ── Theme ──────────────────────────────────────────────────────────────────

    def _on_palette_changed(self) -> None:
        """Called by Qt when the system switches dark ↔ light mode."""
        _Palette.refresh()
        self._apply_theme()

    def _apply_theme(self) -> None:
        """Re-apply all palette-derived stylesheets after a theme change."""
        # Toolbar
        for tb in self.findChildren(QToolBar):
            tb.setStyleSheet(_Palette.toolbar_style())

        # Live / Fit buttons in toolbar
        self._live_btn.setStyleSheet(
            _Palette.active_live_btn() if self._live_btn.isChecked()
            else _Palette.neutral_btn())
        self._fit_btn.setStyleSheet(
            _Palette.active_fit_btn() if self._fit_btn.isChecked()
            else _Palette.neutral_btn())

        # Record button (preserves text/enabled state — just re-style)
        if self._rec_ctrl.is_recording:
            self._record_btn.setStyleSheet(
                f"QPushButton {{ background:{_Palette.RED}; color:{_Palette.text_on_accent};"
                f" border:none; border-radius:4px; padding:4px 10px; font-weight:600; }}"
                f" QPushButton:hover {{ background:{_Palette.RED_DARK}; }}")
        else:
            self._record_btn.setStyleSheet(_Palette.neutral_btn())

        # Sensor panel
        sp = self._sensor_panel
        if hasattr(sp, '_scan_label'):
            sp._scan_label.setStyleSheet(_Palette.label_secondary())
        if hasattr(sp, '_refresh_btn'):
            sp._refresh_btn.setStyleSheet(_Palette.neutral_btn_small())
        if hasattr(sp, '_connect_btn'):
            sp._connect_btn.setStyleSheet(_Palette.neutral_btn_small())
        if hasattr(sp, '_divider'):
            sp._divider.setStyleSheet(_Palette.divider_style())
        # Connected sensor rows
        for addr, row in sp._sensor_rows.items():
            lbl = row.findChild(QLabel, f"status_{addr}")
            btn = row.findChild(QPushButton, f"disc_{addr}")
            if lbl: lbl.setStyleSheet(_Palette.label_secondary())
            if btn: btn.setStyleSheet(_Palette.danger_btn_small())

        # Rate panel
        rp = self._rate_panel
        if hasattr(rp, '_prev_btn'):
            rp._prev_btn.setStyleSheet(_Palette.neutral_btn_small())
            rp._next_btn.setStyleSheet(_Palette.neutral_btn_small())
            rp._rate_lbl.setStyleSheet(
                f"font-size:12px; font-weight:600; color:{_Palette.text_primary};")

        # Session panel
        self._import_btn_style()

        # Sidebar separators — re-create styles by iterating named children
        for child in self.findChildren(QFrame):
            if child.frameShape() == QFrame.Shape.HLine:
                ss = child.styleSheet()
                if "margin" in ss:
                    child.setStyleSheet(_Palette.divider_margin())
                else:
                    child.setStyleSheet(_Palette.divider_style())

        # Fit panel
        self._fit_panel._fit_btn.setStyleSheet(
            self._fit_panel._BTN_NEUTRAL
            if self._fit_panel._fit_btn.text() == "Fit"
            else (self._fit_panel._BTN_OK
                  if "✓" in self._fit_panel._fit_btn.text()
                  else self._fit_panel._BTN_ERR))
        self._fit_panel._formula_edit.setStyleSheet(_Palette.input_field())
        self._fit_panel._status_sep.setStyleSheet(_Palette.divider_style())
        if self._fit_panel._status_lbl.isVisible():
            self._fit_panel._status_lbl.setStyleSheet(_Palette.label_error())
        for row in self._fit_panel._param_rows:
            for child in row.findChildren(QLabel):
                if child.styleSheet() == row._val_lbl.styleSheet() or child == row._val_lbl:
                    child.setStyleSheet(_Palette.label_monospace())
                else:
                    child.setStyleSheet(_Palette.label_bold())
            for child in row.findChildren(QLineEdit):
                child.setStyleSheet(_Palette.small_field())

    def _import_btn_style(self) -> None:
        if hasattr(self, '_session_panel'):
            self._session_panel._import_btn.setStyleSheet(_Palette.neutral_btn_small())

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main toolbar")
        tb.setMovable(False)
        tb.setStyleSheet(_Palette.toolbar_style())
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

        tb.addSeparator()

        # Curve fit toggle
        self._fit_btn = QPushButton("∿ Fit")
        self._fit_btn.setCheckable(True)
        self._fit_btn.setToolTip("Enter curve fitting mode")
        self._fit_btn.toggled.connect(self._on_fit_toggled)
        self._fit_btn.setStyleSheet(self._fit_btn_style(False))
        tb.addWidget(self._fit_btn)

        tb.addWidget(QWidget())   # right padding

    def _live_btn_style(self, active: bool) -> str:
        return _Palette.active_live_btn() if active else _Palette.neutral_btn()

    def _fit_btn_style(self, active: bool) -> str:
        return _Palette.active_fit_btn() if active else _Palette.neutral_btn()

    def _build_sidebar(self) -> None:
        # All sidebar content lives in a scrollable inner widget so that the
        # FitPanel (with dynamic param rows) is always reachable even when the
        # session list is long.
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 4, 0, 4)
        v.setSpacing(0)

        # Sensor panel
        self._sensor_panel = SensorPanel()
        self._sensor_panel.connect_requested.connect(self._on_connect_sensor)
        self._sensor_panel.disconnect_requested.connect(self._on_disconnect_sensor)
        self._sensor_panel.connect_refresh_to(self._on_manual_scan)
        v.addWidget(self._sensor_panel)

        # Rate panel
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet(_Palette.divider_margin())
        v.addWidget(sep1)

        self._rate_panel = RatePanel()
        self._rate_panel.rate_changed.connect(self._on_rate_changed)
        v.addWidget(self._rate_panel)

        # Divider between panels
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(_Palette.divider_margin())
        v.addWidget(sep)

        # Session panel — no stretch so it takes its natural height and doesn't
        # squeeze the FitPanel below it.
        self._session_panel = SessionPanel()
        self._session_panel.remove_session_requested.connect(self._on_remove_session)
        self._session_panel.remove_run_requested.connect(self._on_remove_run)
        self._session_panel.remove_fit_requested.connect(self._on_remove_fit)
        self._session_panel.connect_import_to(self._on_import_csv)
        v.addWidget(self._session_panel)

        # Fit panel (hidden until fit mode is activated)
        self._fit_sep = QFrame()
        self._fit_sep.setFrameShape(QFrame.Shape.HLine)
        self._fit_sep.setStyleSheet(_Palette.divider_margin())
        self._fit_sep.hide()
        v.addWidget(self._fit_sep)

        self._fit_panel = FitPanel()
        self._fit_panel.fit_requested.connect(self._on_fit_requested)
        self._fit_panel.hide()
        v.addWidget(self._fit_panel)

        # Push everything to the top so empty space accumulates at the bottom.
        v.addStretch(1)

        # Wrap in a scroll area so dynamic content (param rows) is always reachable.
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(284)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(_Palette.scroll_area())

        dock = QDockWidget("Sensors & Data", self)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable |
                         QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        dock.setWidget(scroll)
        dock.setTitleBarWidget(QWidget())  # hide default title bar
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _build_central(self) -> None:
        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        self._plot_panel = PlotPanel()
        self._plot_panel.fit_region_changed.connect(self._on_fit_region_changed)
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

        for r in self._fit_results:
            if r.visible:
                units.add(r.unit)

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

        # Compute effective left look-back: cap at buffer capacity / sample rate.
        half         = self._live_window_s / 2
        max_left_s   = LIVE_BUFFER_MAXLEN / self._sample_rate_hz
        half_left    = min(half, max_left_s)

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
            live_left_s          = half_left,
            in_progress_data     = self._rec_ctrl.in_progress_data,
            in_progress_labels   = self._rec_ctrl.in_progress_labels,
            in_progress_color_id = self._in_progress_color_id,
            live_labels          = live_labels,
            fit_results          = self._fit_results,
        )

        # Set x-axis range whenever there are plots.
        # Left edge is capped at max_left_s to match buffer capacity.
        if self._plot_panel._plots:
            anchor  = (self._rec_ctrl.rec_duration
                       if self._rec_ctrl.is_recording else 0.0)
            x_start = anchor + self._center_offset - half_left
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
                f"QPushButton {{ background:{_Palette.RED}; color:{_Palette.text_on_accent};"
                f" border:none; border-radius:4px; padding:4px 10px; font-weight:600; }}"
                f" QPushButton:hover {{ background:{_Palette.RED_DARK}; }}")
            self._record_btn.setEnabled(True)
        else:
            self._record_btn.setText("● Record")
            self._record_btn.setStyleSheet(_Palette.neutral_btn())
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

    def _on_rate_changed(self, rate_hz: float) -> None:
        self._sample_rate_hz = rate_hz
        if self._ble:
            self._ble.set_sample_rate(rate_hz)

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

    # ── Curve fitting ─────────────────────────────────────────────────────────

    def _on_fit_toggled(self, checked: bool) -> None:
        self._fit_mode = checked
        self._fit_btn.setStyleSheet(self._fit_btn_style(checked))
        if checked:
            self._fit_sep.show()
            self._fit_panel.show()
            self._update_fit_traces()
            # Initialise region to the centre quarter of the current viewport.
            rng = self._plot_panel.get_x_range()
            if rng:
                cx = (rng[0] + rng[1]) / 2.0
                w  = (rng[1] - rng[0]) / 4.0
                t_min, t_max = cx - w, cx + w
            else:
                t_min, t_max = 0.0, 5.0
            self._fit_panel.set_region(t_min, t_max)
            self._plot_panel.show_fit_region(t_min, t_max)
        else:
            self._fit_sep.hide()
            self._fit_panel.hide()
            self._plot_panel.hide_fit_region()
            self._fit_panel.clear_edit_state()

    def _on_fit_region_changed(self, t_min: float, t_max: float) -> None:
        self._fit_panel.set_region(t_min, t_max)

    def _update_fit_traces(self) -> None:
        """Rebuild the FitPanel trace dropdown from all recorded/imported data."""
        traces: list[tuple[str, str, str]] = []
        for session in self._rec_ctrl.sessions:
            for unit in session.data:
                traces.append((session.id, unit,
                               f"{session.label} ({unit})"))
        for run in self._imported_runs:
            for unit in run.data:
                traces.append((run.id, unit,
                               f"{run.label} ({unit})"))
        self._fit_panel.set_traces(traces)

    def _get_trace_data(
        self, source_id: str, unit: str
    ) -> tuple[list[float], list[float]] | None:
        """Return (times, values) for the given trace id and unit."""
        for session in self._rec_ctrl.sessions:
            if session.id == source_id and unit in session.data:
                sensor_map = session.data[unit]
                pts_all = [(t, v) for pts in sensor_map.values() for t, v in pts]
                pts_all.sort()
                return [p[0] for p in pts_all], [p[1] for p in pts_all]
        for run in self._imported_runs:
            if run.id == source_id and unit in run.data:
                series = run.data[unit]
                return series["times"], series["values"]
        return None

    def _on_fit_requested(self, source_id: str, unit: str, expr_str: str) -> None:
        try:
            self._do_fit(source_id, unit, expr_str)
        except Exception as exc:
            self._fit_panel.set_error(f"Unexpected error: {exc}")

    def _do_fit(self, source_id: str, unit: str, expr_str: str) -> None:
        import numpy as np
        from sympy import Symbol, lambdify
        from sympy.parsing.sympy_parser import parse_expr, standard_transformations
        from scipy.optimize import curve_fit

        # Strip optional "LHS =" prefix so users can type "I(t) = ..." naturally.
        if "=" in expr_str:
            expr_str = expr_str.split("=", 1)[1].strip()

        t_min, t_max = self._fit_panel.current_region()

        # 1. Get and filter data
        data = self._get_trace_data(source_id, unit)
        if data is None:
            self._fit_panel.set_error("Trace not found.")
            return
        t_all, v_all = data
        ts = [t for t in t_all if t_min <= t <= t_max]
        vs = [v for t, v in zip(t_all, v_all) if t_min <= t <= t_max]
        if len(ts) < 2:
            self._fit_panel.set_error("Not enough data points in selection.")
            return

        # 2. Parse expression
        # Note: implicit_multiplication_application is intentionally NOT used because
        # it breaks variable names containing digits (e.g. U0 → U*0 = 0).
        # Users must write explicit '*' for multiplication.
        # convert_xor lets users write '^' for exponentiation.
        try:
            from sympy.parsing.sympy_parser import convert_xor
            transforms = standard_transformations + (convert_xor,)
            t_sym = Symbol("t")
            expr  = parse_expr(expr_str, transformations=transforms)
            free  = sorted(expr.free_symbols - {t_sym}, key=lambda s: s.name)
            param_names = [str(s) for s in free]
            f_np = lambdify([t_sym] + free, expr, "numpy")
        except Exception as exc:
            self._fit_panel.set_error(f"Parse error: {exc}")
            return

        # 3. Fit (or evaluate directly if no free parameters)
        ts_arr = np.asarray(ts, dtype=float)
        vs_arr = np.asarray(vs, dtype=float)
        params: dict = {}
        param_errors: dict = {}
        popt: list = []

        def _safe_eval(f_np, t, *args):
            """Call lambdified function and guarantee a same-shape numpy array."""
            result = f_np(t, *args)
            return np.broadcast_to(np.asarray(result, dtype=float), np.shape(t)).copy()

        if param_names:
            def fit_fn(t, *args):
                return _safe_eval(f_np, t, *args)

            def residual_cost(p):
                try:
                    r = fit_fn(ts_arr, *p) - vs_arr
                    c = float(np.sum(r * r))
                    return c if np.isfinite(c) else 1e30
                except Exception:
                    return 1e30

            # Phase 1 — multi-start Levenberg-Marquardt over ~50 starting points.
            # Each LM run is cheap; keeping the best residual avoids local minima.
            p0_candidates = self._starting_points(param_names, ts_arr, vs_arr)
            best_cost = np.inf
            best_popt = None
            best_pcov = None
            for p0 in p0_candidates:
                try:
                    po, pc = curve_fit(
                        fit_fn, ts_arr, vs_arr, p0=p0, maxfev=5_000,
                        ftol=1e-10, xtol=1e-10)
                    res = residual_cost(po)
                    if res < best_cost:
                        best_cost, best_popt, best_pcov = res, po, pc
                except Exception:
                    continue

            # Phase 2 — Nelder-Mead (derivative-free) starting from the best LM
            # result or the best single starting point if LM found nothing.  This
            # catches cases where LM is trapped by a poorly-conditioned Jacobian.
            from scipy.optimize import minimize
            nm_seed = (best_popt if best_popt is not None
                       else min(p0_candidates, key=residual_cost))
            try:
                nm = minimize(residual_cost, nm_seed, method='Nelder-Mead',
                              options={'maxiter': 20_000, 'xatol': 1e-9, 'fatol': 1e-9,
                                       'adaptive': True})
                if nm.fun < best_cost:
                    # Nelder-Mead found something better — run one LM pass from
                    # that point to get a proper covariance estimate.
                    try:
                        po, pc = curve_fit(
                            fit_fn, ts_arr, vs_arr, p0=nm.x, maxfev=5_000,
                            ftol=1e-10, xtol=1e-10)
                        res = residual_cost(po)
                        if res <= nm.fun * 1.01:   # LM didn't diverge
                            best_cost, best_popt, best_pcov = res, po, pc
                        else:
                            # Keep NM result; approximate covariance from finite diff
                            best_cost = nm.fun
                            best_popt = nm.x
                            best_pcov = np.diag(np.abs(nm.x) * 0.1 + 1e-8)
                    except Exception:
                        best_cost = nm.fun
                        best_popt = nm.x
                        best_pcov = np.diag(np.abs(nm.x) * 0.1 + 1e-8)
            except Exception:
                pass

            if best_popt is None:
                self._fit_panel.set_error(
                    "Fit did not converge from any starting point.\n"
                    "Try a different formula or a wider time window.")
                return

            perr = np.sqrt(np.abs(np.diag(best_pcov))).tolist()
            popt         = best_popt.tolist()
            params       = dict(zip(param_names, popt))
            param_errors = dict(zip(param_names, perr))
        else:
            # No free parameters — overlay the function as-is.
            def fit_fn(t):
                return _safe_eval(f_np, t)

        # 4. Compute dense fit curve (always an array)
        t_dense = np.linspace(t_min, t_max, 300)
        v_dense = np.asarray(
            fit_fn(t_dense, *popt) if param_names else fit_fn(t_dense),
            dtype=float,
        )

        # 5. Create or replace FitResult
        editing_id = self._fit_panel.editing_fit_id
        if editing_id:
            old = next((r for r in self._fit_results if r.id == editing_id), None)
            fit_id    = editing_id
            fit_label = old.label if old else f"Fit {self._fit_counter}"
            self._fit_results = [r for r in self._fit_results if r.id != editing_id]
            self._session_panel.remove_entry(editing_id)
            # Keep existing color — do not pop from _color_map.
        else:
            self._fit_counter += 1
            fit_id    = f"fit_{self._fit_counter}"
            fit_label = f"Fit {self._fit_counter}"

        fit = FitResult(
            id=fit_id, label=fit_label, unit=unit,
            t_min=t_min, t_max=t_max, expr_str=expr_str,
            source_id=source_id, params=params, param_errors=param_errors,
            t_array=t_dense.tolist(), v_array=v_dense.tolist(),
        )
        self._fit_results.append(fit)
        self._assign_color(fit_id)
        self._session_panel.add_entry(
            fit_id, f"ƒ {fit_label}",
            on_toggle=self._on_toggle_fit,
            on_edit=self._on_edit_fit,
        )

        # Store the fit function so slider changes can update the curve live.
        if param_names:
            self._active_fit_fn = fit_fn
        else:
            self._active_fit_fn = None
        self._active_fit_param_names = param_names
        self._active_fit_id = fit_id
        self._fit_panel.set_params_changed_cb(self._on_fit_params_adjusted)

        self._fit_panel.set_result(params, param_errors)
        self._fit_panel.clear_edit_state()

    def _starting_points(
        self,
        param_names: list[str],
        ts_arr,
        vs_arr,
    ) -> list:
        """Return starting-point arrays for multi-start optimisation.

        Builds data-informed seeds (amplitude, time-constant, their ratios)
        plus a large grid of log-uniform random draws so that the optimizer
        can explore many scales without any domain knowledge.
        """
        import numpy as np
        n = len(param_names)
        v_amp   = max(float(np.max(np.abs(vs_arr))), 1e-6)
        v_std   = max(float(np.std(vs_arr)), 1e-6)
        v_mean  = float(np.mean(vs_arr))
        t_range = max(float(ts_arr[-1] - ts_arr[0]), 1e-6)
        t_mid   = float(ts_arr[len(ts_arr)//2])
        k_est   = 3.0 / t_range     # time-constant guess (e-fold in ~1/3 window)

        # Data-informed seeds — cover amplitude-like and rate-like scales
        deterministic = [
            np.ones(n),
            np.full(n, v_amp),
            np.full(n, v_std),
            np.full(n, k_est),
            np.full(n, v_amp * k_est),
            np.full(n, 1.0 / t_range),
            np.full(n, v_mean) if abs(v_mean) > 1e-9 else np.full(n, v_amp),
            # mixed: alternate amplitude / rate per parameter
            np.array([v_amp if i % 2 == 0 else k_est for i in range(n)]),
            np.array([k_est if i % 2 == 0 else v_amp for i in range(n)]),
        ]

        # Log-uniform random draws with both signs (deterministic seed → reproducible)
        rng = np.random.RandomState(42)
        random_pts = []
        for _ in range(50):
            signs = rng.choice([-1.0, 1.0], size=n)
            mags  = 10.0 ** rng.uniform(-4.0, 4.0, size=n)
            random_pts.append(signs * mags)

        return deterministic + random_pts

    def _on_edit_fit(self, fit_id: str) -> None:
        fit = next((r for r in self._fit_results if r.id == fit_id), None)
        if fit is None:
            return
        if not self._fit_mode:
            self._fit_btn.setChecked(True)   # triggers _on_fit_toggled
        self._update_fit_traces()
        self._fit_panel.populate_for_edit(fit)
        self._plot_panel.show_fit_region(fit.t_min, fit.t_max)
        # Rebuild active fit function so sliders update the curve immediately.
        self._rebuild_active_fit_fn(fit)
        self._fit_panel.set_params_changed_cb(self._on_fit_params_adjusted)

    def _rebuild_active_fit_fn(self, fit: FitResult) -> None:
        """Reconstruct the numpy fit function from a stored FitResult's expr_str."""
        try:
            import numpy as np
            from sympy import Symbol, lambdify
            from sympy.parsing.sympy_parser import (
                parse_expr, standard_transformations, convert_xor,
            )
            expr_str = fit.expr_str
            if "=" in expr_str:
                expr_str = expr_str.split("=", 1)[1].strip()
            transforms = standard_transformations + (convert_xor,)
            t_sym  = Symbol("t")
            expr   = parse_expr(expr_str, transformations=transforms)
            free   = sorted(expr.free_symbols - {t_sym}, key=lambda s: s.name)
            f_np   = lambdify([t_sym] + free, expr, "numpy")
            param_names = [str(s) for s in free]

            def _fn(t, *args):
                result = f_np(t, *args)
                return np.broadcast_to(np.asarray(result, dtype=float), np.shape(t)).copy()

            self._active_fit_fn          = _fn
            self._active_fit_param_names = param_names
            self._active_fit_id          = fit.id
        except Exception:
            self._active_fit_fn = None

    def _on_fit_params_adjusted(self, params: dict) -> None:
        """Called by FitPanel when the user moves a parameter slider."""
        import numpy as np
        fit = next((r for r in self._fit_results if r.id == self._active_fit_id), None)
        if fit is None or self._active_fit_fn is None:
            return
        try:
            popt    = [params[n] for n in self._active_fit_param_names]
            t_dense = np.linspace(fit.t_min, fit.t_max, 300)
            v_dense = self._active_fit_fn(t_dense, *popt)
            fit.t_array = t_dense.tolist()
            fit.v_array = np.asarray(v_dense, dtype=float).tolist()
            # Also update stored params so they survive a re-edit.
            fit.params = {n: float(v) for n, v in params.items()}
            managed = self._ble.managed_addrs if self._ble else frozenset()
            self._refresh_plot(managed)
        except Exception:
            pass

    def _on_toggle_fit(self, fit_id: str, visible: bool) -> None:
        for r in self._fit_results:
            if r.id == fit_id:
                r.visible = visible
                break

    def _on_remove_fit(self, fit_id: str) -> None:
        self._fit_results = [r for r in self._fit_results if r.id != fit_id]
        self._color_map.pop(fit_id, None)

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
