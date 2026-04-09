"""
Data layer: thread-safe buffers and in-memory stores.

LIVE_WINDOW_S defines both the ring-buffer display window and the initial
plot x-axis width.  Change this constant (or expose it as a UI slider later)
to adjust how much live history is visible.
"""

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# Width of the live-sensor scrolling window in seconds.
LIVE_WINDOW_S: float = 20.0

# Hard cap on live ring-buffer size (samples).  At 20 kHz this covers 5 s of
# history, which is the maximum live look-back at high sample rates.
LIVE_BUFFER_MAXLEN: int = 100_000


# ── Sensor metadata ────────────────────────────────────────────────────────────

@dataclass
class SensorMeta:
    quantity:   str
    model:      str
    sensor_id:  str
    address:    str
    ble_device: object = field(default=None, repr=False)

    @property
    def display_label(self) -> str:
        return f"{self.quantity} ᛒ {self.sensor_id}"


# ── Thread-safe per-sensor ring buffer ────────────────────────────────────────

class LiveBuffer:
    """Lock-protected ring buffer for a single sensor's time-series samples.

    Background streaming threads call append(); the UI thread calls snapshot()
    or last_t() for rendering.  The lock is held for the minimum duration.

    The buffer length is dynamic: call resize() when the streaming rate changes
    so the live window always holds at least ``rate * LIVE_WINDOW_S * 1.5``
    samples regardless of rate.  The default is sufficient for polling rates
    up to 100 Hz.
    """

    MAXLEN = 20_000   # default; sufficient for ≤ 667 Hz × 20 s × 1.5

    def __init__(self) -> None:
        self._buf: deque[tuple[float, float]] = deque(maxlen=self.MAXLEN)
        self._lock = threading.Lock()

    def append(self, t: float, v: float) -> None:
        with self._lock:
            self._buf.append((t, v))

    def snapshot(self) -> list[tuple[float, float]]:
        """Return a list copy; safe to call from any thread."""
        with self._lock:
            return list(self._buf)

    def last_t(self) -> Optional[float]:
        with self._lock:
            return self._buf[-1][0] if self._buf else None

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def resize(self, maxlen: int) -> None:
        """Change the ring-buffer capacity, preserving the most recent samples."""
        with self._lock:
            if self._buf.maxlen != maxlen:
                self._buf = deque(self._buf, maxlen=maxlen)


# ── Container for all active live buffers ─────────────────────────────────────

class LiveStore:
    """Holds all active live buffers keyed by (unit, address).

    Background threads call get_or_create() once (during sensor setup) and
    then call buf.append() directly.  The UI thread calls snapshot_all() or
    max_t() from the plot-update timer — never holding the store lock during
    a full render pass.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buffers: dict[tuple[str, str], LiveBuffer] = {}

    def get_or_create(self, unit: str, addr: str) -> LiveBuffer:
        key = (unit, addr)
        with self._lock:
            if key not in self._buffers:
                self._buffers[key] = LiveBuffer()
            return self._buffers[key]

    def remove_addr(self, addr: str) -> None:
        with self._lock:
            for key in [k for k in self._buffers if k[1] == addr]:
                del self._buffers[key]

    def units_for_addr(self, addr: str) -> list[str]:
        with self._lock:
            return [k[0] for k in self._buffers if k[1] == addr]

    def active_units(self) -> set[str]:
        with self._lock:
            return {k[0] for k in self._buffers}

    def active_addrs(self) -> set[str]:
        with self._lock:
            return {k[1] for k in self._buffers}

    def snapshot_all(self) -> dict[str, dict[str, list[tuple[float, float]]]]:
        """Return {unit: {addr: [(t, v)]}} for all buffers that have data."""
        with self._lock:
            keys = list(self._buffers.keys())
        result: dict[str, dict[str, list]] = {}
        for key in keys:
            buf = self._buffers.get(key)
            if buf is None:
                continue
            pts = buf.snapshot()
            if pts:
                unit, addr = key
                result.setdefault(unit, {})[addr] = pts
        return result

    def max_t(self) -> Optional[float]:
        """Latest timestamp across all buffers."""
        with self._lock:
            keys = list(self._buffers.keys())
        t_max: Optional[float] = None
        for key in keys:
            buf = self._buffers.get(key)
            if buf:
                t = buf.last_t()
                if t is not None and (t_max is None or t > t_max):
                    t_max = t
        return t_max


# ── Finalised recording session ────────────────────────────────────────────────

class RecordingSession:
    """One completed recording session.

    data          — {unit: {addr: [(t_rebased, v)]}}
                    timestamps are re-based so the first sample is t=0
    sensor_labels — {addr: display_label} cached at recording time so labels
                    survive a disconnect before Stop is pressed
    """

    def __init__(
        self,
        label: str,
        data: dict[str, dict[str, list[tuple[float, float]]]],
        sensor_labels: dict[str, str],
    ) -> None:
        self.id            = str(id(self))
        self.label         = label           # "HH:MM:SS, DD-MM-YY"
        self.data          = data
        self.sensor_labels = sensor_labels
        self.visible: bool = True


# ── Imported CSV run ───────────────────────────────────────────────────────────

class ImportedRun:
    """One run from an imported Pasco/SPARKvue CSV file.

    data — {unit: {"times": [float], "values": [float]}}
    """

    def __init__(
        self,
        label: str,
        run_num: int,
        data: dict[str, dict[str, list]],
    ) -> None:
        self.id            = f"csv_{id(self)}"
        self.label         = label       # e.g. "Run 1"
        self.run_num       = run_num
        self.data          = data
        self.visible: bool = True


# ── Curve fit result ───────────────────────────────────────────────────────────

@dataclass
class FitResult:
    """One fitted parametric curve overlaid on a recorded or imported trace.

    t_array / v_array are a dense 300-point grid computed once at fit time.
    They are stored so that PlotPanel can render without re-evaluating the
    expression on every tick.

    params / param_errors hold the best-fit values and 1σ uncertainties.
    The initial-guess strategy is kept outside this class so callers can
    extend it later without changing the data model.
    """

    id:           str
    label:        str                  # "Fit 1", "Fit 2", …
    unit:         str                  # subplot unit matched at fit time
    t_min:        float                # selection window start (seconds)
    t_max:        float                # selection window end (seconds)
    expr_str:     str                  # raw RHS formula as the user typed it
    source_id:    str                  # id of the RecordingSession / ImportedRun
    params:       dict                 # {name: best-fit value}
    param_errors: dict                 # {name: 1σ std-error}
    t_array:      list                 # time coordinates for the fit curve
    v_array:      list                 # value coordinates for the fit curve
    visible:      bool = True
