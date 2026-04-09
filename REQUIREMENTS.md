# Physics Data Recorder — Requirements

## Functional Requirements

### BLE Sensor Discovery
- On startup, automatically scan for PASCO BLE sensors.
- In "Live Discovery" mode, re-scan periodically with a target interval short enough
  that a nearby sensor appears in the dropdown within ~5 s of being powered on.
- A manual refresh button triggers an immediate scan.
- A "Live Discovery" toggle in the toolbar enables/disables auto-scanning.

### Sensor Connection
- Sidebar shows a dropdown of discovered, unmanaged sensors.
- User clicks ＋ (mouse over: Connect) to connect a sensor; connection runs on a background thread.
- Multiple sensors can be connected simultaneously.
- A sensor must **never** appear in both the dropdown and the connected list at the same time.
- Immediately after the start of the connection process, the sensor is removed from the dropdown and added to the connected list. The status of the sensor in the connected list is 🟡 (transitioning) with the message "connecting...".
- While disconnecting, the sensor stays in the connected list (showing 🟡 transitioning) with the status message "disconnecting…"). 
- The sensor is only added back to the dropdown when the disconnection process is complete and the sensor has been removed from the connected list.

### Sensor Status Display
- Each connected sensor shows a status indicator: 🟢 connected / 🟡 transitioning / 🔴 error.
- The sensor name in the connected list uses the format `<type> ᛒ <id>` (e.g., "Voltage ᛒ 390-900"),
  where `ᛒ` is a Bluetooth symbol, `<type>` is the physical quantity (Voltage, Current, …),
  and `<id>` is the sensor's hardware identifier (e.g., 390-900).
- The disconnect button (－) is disabled while the sensor is disconnecting.
- After a sensor disconnects (or errors), it is removed from the list and reappears in the dropdown.

### Live Data Streaming
- Connected sensors stream data continuously; only the most recent samples are retained.
- Live (unrecorded) signal is shown in the plot as **grey, semi-opaque** lines.
- The live ring buffer is capped at `LIVE_BUFFER_MAXLEN = 100 000` samples.
  At high sample rates this limits the available history to `100 000 / sample_rate`
  seconds (e.g. 5 s at 20 kHz).
- The plot window right half is `LIVE_WINDOW_S/2` wide (future / empty space).
  The left half (live history) is `min(LIVE_WINDOW_S/2, 100 000 / sample_rate)`,
  making the visible window asymmetric at high sample rates.
- The newest sample is always at the **centre** anchor of the plot.
- When **not recording**, the time axis shows `(−left, +LIVE_WINDOW_S/2)`,
  with `0s` (latest sample) fixed at the centre anchor.  `left` is the capped
  look-back duration defined above.

### Recording
- "Record" button starts a recording session; "Stop" finalises it.
- Only samples arriving after Record is pressed are captured.
- The timestamp of the first recorded sample is **0**; all subsequent timestamps are in seconds relative to that first sample.
- During recording the plot x-axis represents recording time (0 = recording start).
  The newest recorded sample is always at the **centre** of the viewport, mirroring
  live mode. The viewport is `LIVE_WINDOW_S` wide: left half shows recent history,
  right half shows empty future space.  While the recording duration is shorter than
  `LIVE_WINDOW_S/2` the left edge is negative, showing pre-recording live data there —
  creating a smooth transition from live to recording mode.  Once `dur > LIVE_WINDOW_S/2`
  the window scrolls normally with the newest sample at the centre.
- When the recording stops, the x-axis is immediately zoomed so that:
  - The full recording `[0, recording_duration]` is always visible.
  - Space for newly incoming (non-recorded) live data is allocated to the left of
    `x = 0`, between **½** of the total viewport (short recordings) and **⅕**
    (long recordings).
  - Concretely:
    ```
    left_part  = max(recording_duration, LIVE_WINDOW_S / 2)   # right edge of viewport
    right_part = max(recording_duration / 4, LIVE_WINDOW_S / 2)   # space left of x=0
    new_window = left_part + right_part
    x_range    = [−right_part, left_part]
    ```
    where `LIVE_WINDOW_S` is the compile-time default (20 s).
    This formula ensures the viewport is at least `LIVE_WINDOW_S` wide and that
    the live-data fraction transitions smoothly from ½ to ⅕ as recordings grow
    beyond `2 × LIVE_WINDOW_S`.
- Every session is uniquely identified by the date-time stamp of its start.
- A session captures one signal per sensor that was active at any point during the
  recording interval (sensors that disconnect mid-recording are captured up to the point of disconnection, sensors that are connected mid-recording are included in the recording with the first sample they send).
- Multiple sessions can be recorded sequentially.
- Each session appears in the sidebar with a `HH:MM:SS, DD-MM-YY` label indicating the session start, including a visibility checkbox and a remove (－) button aligned to the right.
- Recorded traces appear as **vivid coloured** solid lines in the plot (one colour per session).

### CSV Import
- User can upload a Pasco/SPARKvue CSV file via the sidebar.
- Each run in the CSV appears as a separate entry with a `HH:MM:SS, DD-MM-YY` label, with a visibility checkbox and a remove (－) button aligned to the right of the label.
- CSV runs are plotted as vivid coloured solid lines; colours are assigned so that they are distinct from all existing signals (recorded or loaded).

### Plot Layout
- One subplot per physical unit (e.g. V, A); subplots share a common x-axis.
- Subplots stacked vertically; each has its own legend and y-axis label.
- Crosshair spike lines shown on hover across all subplots.
- The user can pan/zoom freely and the view is preserved across plot refreshes. Implementation Hint: This implies that `LIVE_WINDOW_S` must be adjusted according to pan/zoom actions by the user.
- **Legend ordering**: the live (grey) signal entry for a unit appears first in that unit's legend,
  before any recorded or imported traces.  If no sensor is currently connected for a unit, no
  live-signal entry is shown in that unit's legend.
- **Legend and axis font consistency**: axis tick labels and axis title labels use the same font
  style (typeface and weight) as the legend text.  Font sizes of tick labels and axis titles are
  unchanged.
- **Live legend label format**: the live signal legend entry uses the same `<type> ᛒ <id>` format
  as the connected-sensors list.

### Data Export
- "Download CSV" link in the toolbar; active only when at least one recorded session exists or imported data is present.
- Exported columns: `session_start`, `sensor_id_1`, `time_s_1`, `value_1`, `unit_1`, ... , `sensor_id_n`, `time_s_n`, `value_n`, `unit_n`. If no sample exists for a given sensor at a particular row, `time_s_i` and `value_i` are `nan`.
- Future: the CSV export format will be replaced by a more efficient time-series format (e.g. Parquet or HDF5).

---

## Non-Functional Requirements

### UI Responsiveness
- **No flickering** of any UI element (toolbar, sidebar, plot toolbar) during live data updates or live sensor discovery.
- Plot scrolls smoothly at approximately 3 fps during live streaming.
- Sensor connection/disconnection status updates in the sidebar promptly (within one UI refresh cycle after the state change occurs in the background thread).

### Thread Safety
- All BLE I/O is serialised (only one CoreBluetooth manager active at a time on macOS).
- Background streaming threads communicate with the UI thread only through thread-safe shared data structures (ring buffers); they never directly modify UI state.
- Sensor management state (which sensors are managed, their connection status) is written only by the UI thread; background threads write only to data buffers.

---

## Architecture

### Why the current stack needs rethinking

The existing Streamlit-based prototype demonstrated all required functionality but
hits a structural ceiling: Streamlit re-executes the entire script on every user
interaction *and* on every 0.3 s live-data refresh, replacing all DOM nodes each
time. This causes unavoidable flickering of toolbar, modebar, and sidebar elements —
not a configuration issue, but a fundamental consequence of the rendering model.

A suitable framework must support **targeted, partial UI updates**: refresh only the
plot canvas without touching the rest of the window.

### Recommended stack: PyQt6 + PyQtGraph

| Concern | Solution |
|---|---|
| UI toolkit | **PyQt6** — mature, native macOS desktop, fine-grained widget updates |
| Real-time plots | **PyQtGraph** — purpose-built for scientific live data, renders only the changed canvas |
| BLE thread communication | Qt **signals/slots** — queued connections are inherently thread-safe |
| Data manipulation | **pandas** — unchanged from current code |
| Future analysis | **scipy** (curve fitting) + dock-widget panels |

PyQtGraph updates only the plot canvas on each data tick — not the toolbar, sidebar,
or any other widget — eliminating flickering by design. Qt's signal/slot system
replaces the current ad-hoc dict-passing pattern with a well-defined, typed,
thread-safe event bus.

**Alternative:** **NiceGUI** (web-based, Python-only, WebSocket partial updates,
`ui.run(native=True)` for standalone mode). Suitable if a browser-rendered UI is
preferred; slightly less mature ecosystem and higher communication overhead for
high-frequency data.

### Layered architecture

```
┌────────────────────────────────────────────────────────┐
│                       UI Layer                         │
│  MainWindow (QMainWindow)                              │
│  ├── Toolbar  (Record/Stop, Download, Live Discovery)  │
│  ├── Sidebar (QDockWidget)                             │
│  │   ├── SensorPanel   (dropdown + connected list)     │
│  │   └── SessionPanel  (recorded runs + CSV imports)   │
│  └── PlotPanel (PyQtGraph, one PlotItem per unit)      │
├────────────────────────────────────────────────────────┤
│                  Application Layer                     │
│  AppController                                         │
│  ├── SensorController  (connect / disconnect / stream) │
│  ├── RecordingController (start / stop / flush)        │
│  └── DataController    (CSV import, export, sessions)  │
├────────────────────────────────────────────────────────┤
│                    Data Layer                          │
│  LiveBuffer    (per-sensor ring buffer, thread-safe)   │
│  SessionStore  (finalised recordings)                  │
│  ImportedRuns  (CSV-imported data)                     │
├────────────────────────────────────────────────────────┤
│                    BLE Layer                           │
│  BLEManager    (discovery, connect, stream loop)       │
│  PASCOAdapter  (wraps pasco library)                   │
└────────────────────────────────────────────────────────┘
```

### Thread model

```
Main thread (Qt event loop)
├── All UI rendering and user-event handling
└── Slot handlers update widgets in response to BLE signals

BLE worker threads (one per active sensor + one for scanning)
├── Serialised through a single lock (one CoreBluetooth manager at a time)
├── Write only to LiveBuffer ring buffers
└── Emit Qt signals → queued delivery to main thread
      scan_started()
      scan_complete(list[SensorMeta])
      status_changed(addr, status)
      unit_discovered(addr, unit)

Plot timer (QTimer, 333 ms ≈ 3 fps)
├── Calls BLEManager.poll_cleanup()  — Phase 2 sensor removal
├── Calls RecordingController.flush() — copy live → record buffers
└── Reads LiveStore snapshots and updates only the PlotPanel canvas
```

**No `DataReady` signal**: streaming threads write directly to `LiveBuffer`
ring buffers; the plot timer pulls a snapshot on each tick. This avoids
high-frequency signal emission (20 Hz × n sensors) and keeps inter-thread
communication minimal.

### Extensibility points

- **Data analysis panel**: add a `AnalysisPanel` dock widget (QDockWidget); reads
  `SessionStore` and `ImportedRuns`; uses `scipy.optimize.curve_fit` for model
  fitting. No changes to other layers needed.
- **Export formats**: `DataController.export()` dispatches to a small set of
  pluggable exporter classes (CSV, HDF5, Parquet). Add a new exporter without
  touching UI or BLE code.
- **Additional sensor types**: `PASCOAdapter` implements a `SensorAdapter` abstract
  base class. USB, serial, or network sensors plug in as new adapters without
  modifying the application or UI layers.
- **Multiple plot styles**: `PlotPanel` can be swapped for a Matplotlib-embedded
  canvas (FigureCanvasQTAgg) if publication-quality static figures are needed
  alongside the live PyQtGraph view.

---

## Implementation notes (deviations from the architecture plan)

### StreamStrategy pattern (sensor streaming)

Polling and push-notification streaming differ fundamentally in protocol,
timing, error handling, and cleanup.  Spreading these differences across
`ble_manager.py` and `main_window.py` would entangle BLE transport logic with
connection management and UI concerns.

Instead, the variance is fully encapsulated in **`sensor_stream.py`**:

```
StreamStrategy (ABC)
├── PollStream   — read_data() one-shot polling, ≤ 20 Hz
└── PushStream   — svc1 push notifications, any rate > 20 Hz
```

`BLEManager._stream_sensor` calls `make_strategy(rate_hz)` to obtain the right
implementation, then calls `strategy.stream(device, ...)`.  `stream()` blocks
until `stop_event` or `restart_event` fires; `BLEManager` restarts with a new
strategy when the rate changes (e.g. polling → push boundary crossing).

**Why this matters:** adding a third transport (e.g. USB or Ethernet) means
adding a new `StreamStrategy` subclass — nothing else changes.  The rest of the
app (connection management, data buffering, UI) never needs to know which
transport is active.

---

### File structure

The implemented layer structure maps to four files:

| File | Role |
|---|---|
| `data_store.py` | Data layer — `LiveBuffer`, `LiveStore`, `SensorMeta`, `RecordingSession`, `ImportedRun` |
| `ble_manager.py` | BLE layer — `BLEManager` (QObject with signals) |
| `sensor_stream.py` | Streaming strategies — `StreamStrategy` ABC, `PollStream`, `PushStream` |
| `main_window.py` | Application + UI layers — `RecordingController`, `PlotPanel`, `SensorPanel`, `SessionPanel`, `MainWindow` |
| `main.py` | Entry point |
| `ble_patches.py` | macOS CoreBluetooth/pasco compatibility patches (unchanged) |

The `AppController` / `SensorController` / `DataController` split from the
architecture diagram was flattened: `RecordingController` is a standalone
class in `main_window.py`; sensor connect/disconnect and CSV import/export
are handled directly by `MainWindow` slots. For this application size this is
cleaner than three thin controller classes with no meaningful state boundary.

### PASCOAdapter

Not a separate class. `ble_patches.py` continues to serve this role
(monkey-patching the pasco library on import). A formal `SensorAdapter`
abstract base class can be introduced when a second sensor type is needed.

### CSV export format

The export uses a **long/tidy** format (`source`, `label`, `time_s`, `value`,
`unit`) rather than the wide per-sensor-column format described in the
requirements. Reason: for sensors with different sampling rates, aligning rows
by index into a wide format produces misleading implicit alignment. The tidy
format is unambiguous and directly importable by pandas/R. The wide format
can be added as a second export option later.

### Scan interval

Reduced from 15 s to **5 s** to meet the "sensors appear within ~5 s"
discovery UX target.

### x-axis rules, `_live_window_s`, and `_center_offset` (implemented)

`LIVE_WINDOW_S` in `data_store.py` is the compile-time default (20 s).

**Runtime state in `MainWindow`:**

| Field | Meaning | Initial value |
|---|---|---|
| `_live_window_s` | Current viewport width in seconds | `LIVE_WINDOW_S` |
| `_center_offset` | User-chosen offset of the viewport centre from the natural anchor | `0.0` |

**Natural anchor:** `0.0` when not recording; `rec_duration` when recording.
**Viewport centre** = `anchor + _center_offset`.
**Programmatic range** = `[anchor + _center_offset − W/2, anchor + _center_offset + W/2]`.

**User pan/zoom detection:** each tick the programmatically set range is stored
in `_expected_x_range`.  On the next tick the actual viewport is compared.  If
they differ by more than 0.01 s, `_live_window_s` and `_center_offset` are
updated from the actual viewport.  A plot rebuild sets `_expected_x_range = None`
to avoid the fresh default ViewBox range being mistaken for user interaction.

| Mode | x range | Anchor | `_center_offset` change |
|---|---|---|---|
| Live, not recording | `[−W/2+O, W/2+O]` | 0 | updated on user interaction |
| Recording | `[dur−W/2+O, dur+W/2+O]` | `dur` | reset to 0 on record start; updated on user interaction |
| Just stopped | `[−R, L]` one tick (see below) | 0 | set to `(L−R)/2` |

`W = _live_window_s`, `O = _center_offset`, `dur = rec_duration`.

`L = max(final_dur, LIVE_WINDOW_S/2)`, `R = max(final_dur/4, LIVE_WINDOW_S/2)`.
`W_new = L + R`, `O_new = (L−R)/2`.

**Recording start:** `_center_offset` is reset to `0` so the recording begins with the
natural anchor-at-centre view, regardless of any earlier user pan.

**Recording stop:** the anchor switches from `rec_duration` to `0`.  `_center_offset`
and `_live_window_s` are set from the formula above, and `_expected_x_range` is set to
`None` so that the stale recording-time viewport is not mistaken for user interaction.

**Range-setting in `_refresh_plot`:** a single formula `[anchor + O − W/2, anchor + O + W/2]`
is used whenever there are plots to show.  No separate `_post_stop_duration` one-shot
branch is needed.

During recording, when `dur < W/2 − O`, the left edge is negative, showing
pre-recording grey data (smooth visual transition from live to recording mode).

### Sensor label format and live-trace legend (implemented)

**`SensorMeta.display_label`** (in `data_store.py`) returns `"{quantity} ᛒ {sensor_id}"`.
This is used everywhere a human-readable sensor name is needed:
- Connected-sensors list in `SensorPanel` (via `_on_status_changed` → `meta.display_label`)
- Live (grey) trace legend entries in `PlotPanel.update_curves` (via `live_labels` dict built
  in `_refresh_plot` and passed to `update_curves`)
- Sensor dropdown (via `SensorMeta.display_label` directly)

**Legend ordering:** `update_curves` adds curves in this order: live (grey) → in-progress
(vivid) → recorded sessions → imported CSV.  Because PyQtGraph legend entries are appended
on first `plot()` call and preserved on subsequent `setData()` calls, the order is set once
per PlotItem lifetime (i.e., after every `rebuild_for_units`) and stays stable.

**Legend cleanup:** stale curves (including their legend entries) are removed at the end
of every `update_curves` call via `plot.removeItem()`, which also removes the legend entry
automatically (PyQtGraph 0.13+).

**Font consistency:** `rebuild_for_units` calls `QApplication.font()` to get the application
default font and applies it to tick labels (`axis.setTickFont(font)`) and axis title labels
(`axis.label.setFont(font)`) for both axes of every PlotItem.  The legend uses the same
application default font by default, so all three text elements stay in sync.

---

## Sampling Rate Control

### Functional requirements

- A **sampling rate control** is visible in the sidebar, below the sensor
  connection panel.  Always visible; the setting is preserved when no sensors
  are connected.
- Discrete steps on a logarithmic scale:
  `1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1k, 2k, 2.5k, 5k, 10k, 20k Hz`.
  Default: **20 Hz**.
- UI: ◀ / ▶ step buttons with a centred rate label (e.g. `"20 Hz"`).
- A rate change takes effect immediately for all connected sensors.
  No reconnect required.
- Rate changes are allowed before, during, and after a recording.

### Behaviour on rate change

- Changing rate triggers an immediate restart of the streaming strategy for
  each connected sensor.
- At the transition there is a **brief gap in the live plot** while the
  sensor settles at the new rate.  This is expected and preferable to
  corrupt data near the boundary.
- All ADC values including boundary values (0x0000 = −full-scale, 0xFFFF ≈
  +full-scale) are valid measurements and are never filtered out.
- Recordings use absolute timestamps; non-uniform sample spacing from a
  mid-recording rate change is handled automatically by the `(t, v)` plot.

### Data volume

- Live buffer capacity scales automatically with rate.
- Long high-rate recordings accumulate significant data.  No warning is
  required for typical lab durations (< 5 min).

### Architecture

Two transport strategies are encapsulated in `sensor_stream.py` behind a
`StreamStrategy` ABC.  `BLEManager` selects the strategy, runs it, and
restarts it on rate changes.  Nothing else in the app needs to know which
transport is active.

- **`PollStream`** (≤ 20 Hz): one-shot request/response per sample.
  Reliable, no sensor state changes.  Adapts to rate changes within the
  current run — no restart needed.
- **`PushStream`** (> 20 Hz): sensor streams samples autonomously at the
  configured rate.  Requires an init sequence, explicit per-packet
  acknowledgement, and a stop command on exit.  Restarts on rate change;
  discards a brief transition window of data after each init to flush
  residual old-rate packets.

### PushStream protocol (confirmed from PacketLogger capture)

GATT handles used (PS-3211 voltage / PS-3212 current):

```
svc0 SEND_CMD  4a5c0000-0002-...  polling commands, stop, keepalive
svc1 SEND_CMD  4a5c0001-0002-...  streaming init commands
svc1 RECV_CMD  4a5c0001-0003-...  init ACK responses from sensor
svc1 DATA      4a5c0001-0004-...  push-notification stream
svc1 SEND_ACK  4a5c0001-0005-...  per-packet ACK writes
```

**Init sequence** (both sensors, same sequence per sensor):
```
Write svc1 SEND_CMD  [0x01, p0, p1, p2, p3, 0x02, 0x00]  # period µs LE
Write svc1 SEND_CMD  [0x28, 0x00, 0x00, N]                # N: 0x04 voltage / 0x02 current
sleep ~55 ms
Write svc1 SEND_CMD  [0x29, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00]
Write svc0 SEND_CMD  svc0_cmd                              # sensor-specific (fixed, see below)
sleep ~10 ms
Write svc0 SEND_CMD  [0x00]                                # keepalive
```

**Sensor-specific svc0_cmd values** (fixed, rate-independent):
```
Voltage  PS-3211:  [0x06, 0x7c, 0x06]
Current  PS-3212:  [0x06, 0x0b, 0x03]
```

**Stop command** (after each streaming run, before next init or on disconnect):
```
Write svc0 SEND_CMD  [0x07]
```

**Notification format** (rate-dependent packet size):
```
byte 0      : sequence counter 0–31
bytes 1–end : uint16-LE ADC samples

Samples per packet = rate_hz / 10  (sensor groups 100 ms of data per packet)
Exceptions:
  ≤ 500 Hz  : payload always an even number of bytes → no leftover
  2000 Hz   : BLE MTU limits payload to 139 bytes → 69 samples + 1 leftover byte
              The leftover byte is the LOW byte of the next uint16; prepend it
              to the following packet's payload before unpacking.
```

**ACK protocol** (every 8 packets):
```
Write svc1 SEND_ACK  [0x00, 0x00, 0x00, 0x00, last_seq_in_batch % 32]
```
ACK byte cycles: 7 → 15 → 23 → 31 → 7 → …

`BLEManager.set_sample_rate()` propagates a rate change to all active
connections and triggers strategy restarts.  New connections pick up the
current rate automatically.

## Active Workarounds — Must Be Removed

### Corrupted development sensors (temporary, remove before production)

Two sensors (model 390-900 and 910-042) had their BLE advertising names
permanently corrupted during BLE protocol reverse-engineering (probe scripts
sent an unintended `[0x08, 0x02]` command that overwrote a persistent firmware
byte, changing the advertisement from e.g. `"Voltage 390-900>78"` to
`"\x02 390-900>78"`).

These sensors are kept in use **only to avoid corrupting additional units**
during continued development.  The workaround must be removed once the sensors
are retired or reflashed by PASCO.

**Location of workaround code** in `ble_manager.py`:
- `import re`, `from bleak import BleakScanner`, `from bleak.backends.device import BLEDevice` (top of file).
- `_PASCO_MODEL_RE` constant and `_reconstruct_pasco_name()` helper.
- `[WORKAROUND] … [END WORKAROUND]` block in `_do_scan()` (raw bleak fallback scan).
- `[WORKAROUND] … [END WORKAROUND]` block in `_connect_thread_fn()` (name reconstruction on connect).

**How to remove when ready:** delete all four marked sections.  No other
file is affected.

**Impact on intact sensors:** none.  Intact sensors flow through
`pasco.scan()` unchanged.  The workaround is a fallback-only path.

---

## Curve Fitting

### Overview

A curve-fitting mode lets the user overlay a parametric model on any recorded
or imported trace.  The fit is added to the MEASUREMENTS list and rendered on
the plot like any other trace.

### Activation

A **"∿ Fit"** toggle button in the toolbar (right of Download CSV).  When
active:
- A `LinearRegionItem` appears on all subplots (spanning roughly ¼ of the
  current viewport).  The user drags its edges to select the time window.
- A **Fit Panel** appears in the sidebar, below the MEASUREMENTS section.

### Fit Panel layout

```
CURVE FIT
[Trace dropdown               ▼]
[f(t) = ________________       ]   ← QLineEdit, monospace
[error message if any          ]   ← hidden unless error
[ Fit ]
──────────────────────────────
  param = value ± error          ← results table, shown after fit
```

- **Trace dropdown** lists every visible recorded session and imported run,
  one entry per unit (e.g. `"14:30:00 (A)"`, `"Run 1 (V)"`).
- **Formula field** accepts a Python/sympy expression of `t`, e.g.
  `U0/RD * (1 - exp(-RD/L*t))`.  The independent variable is always `t`.
  All other free symbols are treated as free parameters.
- Supported functions: `exp`, `sin`, `cos`, `sqrt`, `log`, `pi`, `e`, and
  standard arithmetic.  Implicit multiplication (e.g. `2t`) is supported.

### Fitting algorithm

- Data within the selected time window is extracted from the chosen trace.
- Expression is parsed with **sympy** (`parse_expr` + `implicit_multiplication`),
  then compiled to a numpy callable via `lambdify`.
- Fitted via `scipy.optimize.curve_fit` (Levenberg-Marquardt, equivalent to
  MLE under Gaussian noise).
- Initial parameter guesses: all **1.0** (architecture is open to extension via
  `_initial_guesses(param_names)`).
- If the expression has no free parameters, it is evaluated directly (no
  fitting step) and overlaid as-is.
- Fit result + 1σ parameter uncertainties are shown in the panel.

### Fit result as a run entry

On success, a `FitResult` is appended to the MEASUREMENTS list:

```
  [☑ ƒ Fit 1   ] [✏] [－]
```

- **ƒ** prefix distinguishes fits from sessions and CSV imports.
- **✏ (pencil)** button re-opens the fit panel pre-filled with the existing
  fit's trace, formula, and time window for editing.  Re-fitting replaces the
  entry in-place (same label, same color).
- **－** removes the entry and its curve.
- Fit curve is drawn as a **dashed vivid line** within `[t_min, t_max]`.
- Color is assigned from the same pool as sessions and CSV imports.
- Visibility toggle works like any other entry.

### Data model (`FitResult` in `data_store.py`)

```python
@dataclass
class FitResult:
    id:           str                    # "fit_N"
    label:        str                    # "Fit 1", "Fit 2", …
    unit:         str                    # subplot unit (e.g. "A", "V")
    t_min:        float                  # selection window start
    t_max:        float                  # selection window end
    expr_str:     str                    # raw RHS formula text
    source_id:    str                    # id of the fitted trace
    params:       dict[str, float]       # best-fit parameter values
    param_errors: dict[str, float]       # 1σ standard errors
    t_array:      list[float]            # 300-point time grid for rendering
    v_array:      list[float]            # corresponding fitted values
    visible:      bool = True
```

`t_array` / `v_array` are computed once at fit time and stored; no re-eval
during rendering.

### Architecture

| File | Change |
|---|---|
| `data_store.py` | Add `FitResult` dataclass |
| `main_window.py` | Add `FitPanel` widget; `LinearRegionItem` management in `PlotPanel`; `_on_fit_requested` and related slots in `MainWindow`; extend `update_curves` to render fit curves; extend `SessionPanel.add_entry` with optional pencil button |

No changes to `ble_manager.py`, `sensor_stream.py`, or the BLE/recording stack.

---

## Open Questions / To-Do

1. **Faster sensor discovery**: investigate whether `pasco.scan()` accepts a
   timeout argument shorter than its default, enabling tighter scan cycles
   without the full ~5 s wait.

2. **y-axis zoom preservation during live streaming**: currently `set_x_range()`
   re-enables y auto-range on every plot tick so y adapts to the visible window.
   If the user manually zooms the y axis, that zoom is overridden on the next
   tick. A future improvement: detect a user y-zoom gesture and stop overriding.

3. **Analysis panel**: add a `QDockWidget` with fit controls (model selection,
   parameter display) reading `SessionStore` via `scipy.optimize.curve_fit`.

4. **High-rate implementation (Phase 2)**: implemented in `sensor_stream.py`.
   See the Phase 2 section above for the confirmed GATT protocol details.

5. **High-rate memory**: a 10-minute recording at 20 kHz uses ~768 MB for two
   sensors.  If memory pressure becomes an issue, consider streaming recorded
   data to a temporary file during the recording rather than keeping it fully
   in RAM.
