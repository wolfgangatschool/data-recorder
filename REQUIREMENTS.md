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
- The disconnect button (－) is disabled while the sensor is disconnecting.
- After a sensor disconnects (or errors), it is removed from the list and reappears in the dropdown.

### Live Data Streaming
- Connected sensors stream data continuously; only the most recent `LIVE_WINDOW_S` seconds of live data are retained and displayed.
- Live (unrecorded) signal is shown in the plot as **grey, semi-opaque** lines.
- The plot window is `LIVE_WINDOW_S` wide (default 20 s). The newest sample is
  always at the **centre** of the plot. The right half `[0, LIVE_WINDOW_S/2]` shows
  empty future space; the left half `[-LIVE_WINDOW_S/2, 0]` shows recent history.
- When **not recording**, the time axis shows `(-LIVE_WINDOW_S/2, +LIVE_WINDOW_S/2)`,
  with `0s` (latest sample) fixed at the centre.

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
- When the recording stops, the x-axis is immediately zoomed to show the complete session if `LIVE_WINDOW_S`<`recording_duration`: `[0, recording_duration]` and `LIVE_WINDOW_S` is set to `recording_duration`. if `LIVE_WINDOW_S`>=`recording_duration` the x-axis zoom is not changed by stopping the recording.
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

### File structure

The implemented layer structure maps to four files:

| File | Role |
|---|---|
| `data_store.py` | Data layer — `LiveBuffer`, `LiveStore`, `SensorMeta`, `RecordingSession`, `ImportedRun` |
| `ble_manager.py` | BLE layer — `BLEManager` (QObject with signals) |
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
| Recording | `[dur−W/2+O, dur+W/2+O]` | `dur` | updated on user interaction |
| Just stopped, `final_dur > W` | `[0, final_dur]` one tick | 0 | set to `final_dur/2` |
| Just stopped, `final_dur ≤ W` | unchanged | 0 | set to `final_dur + O` (preserves x range) |
| No sensors | preserved (user pans freely) | — | not changed programmatically |

`W = _live_window_s`, `O = _center_offset`.

On recording stop the anchor switches from `rec_duration` to `0`.  `_center_offset`
is adjusted to keep the viewport numerically unchanged: `O_new = final_dur + O_old`.
When `final_dur > W`, the viewport zooms to `[0, final_dur]` and `O = final_dur/2`.

During recording, when `dur < W/2 − O`, the left edge is negative, showing
pre-recording grey data (smooth visual transition from live to recording mode).

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
