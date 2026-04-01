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

Two transport strategies are used depending on the configured rate:

- **Polling (≤ 20 Hz):** `PollStream` — one-shot `GCMD_READ_ONE_SAMPLE` per
  sample.  Reliable with no changes to sensor firmware state.
- **Push (> 20 Hz):** `PushStream` — continuous push notifications up to
  the sensor's hardware limit (~2 kHz for PS-3211/PS-3212).

Both are encapsulated in `sensor_stream.py` behind the `StreamStrategy` ABC.
`BLEManager` selects the right strategy based on the configured rate and
restarts it transparently on rate changes.

---

### Phase 1 — Adjustable Polling Rate (1–100 Hz)

#### Functional requirements

- A **sampling rate control** is visible in the sidebar, below the sensor
  connection panel and above the session panel.  Always visible and editable;
  the setting is preserved when no sensors are connected.
- The control presents **two groups** of discrete steps on a **logarithmic
  scale**, separated by a visual divider:
  - **Polling group** (≤ 20 Hz, one-shot BLE polling):
    `1, 2, 5, 10, 20 Hz` — default **20 Hz**
  - **Push group** (> 20 Hz, continuous push notifications):
    `25, 50, 100, 200, 250, 500, 1k, 2k Hz` — all active.
- The UI control is a row of **◀ / ▶ step buttons** with a centred label
  (e.g. `"20 Hz"`), snapping through the combined step list.
- A rate change takes effect **immediately** for all currently connected
  sensors and for any sensor connected subsequently.  No reconnect required.
- Rate changes are allowed **before, during, and after a recording**.

#### Consistency constraints

- At 100 Hz × 20 s live window: 2 000 samples — well within
  `LiveBuffer.MAXLEN = 20 000`.  No `MAXLEN` change is needed for Phase 1.
- Recordings use **absolute timestamps**; non-uniform sample spacing caused
  by a mid-recording rate change is handled automatically by the `(t, v)`
  plot representation.  No changes to `RecordingController` are needed.
- `conn["poll_interval"]` is a Python `float` written by the main thread and
  read by the streaming thread.  The GIL makes single-value float assignment
  atomic for this purpose; no additional lock is needed.
- The 333 ms plot timer flushes up to `333 ms × 100 Hz = 33` new samples per
  tick — well within the capacity of the flush loop.
- `STALE_TIMEOUT_S = 5.0` is unchanged: a sensor that stops responding is
  always disconnected within 5 s regardless of the configured poll rate.

#### Architecture (Phase 1)

**`ble_manager.py`:**
- Add `_sample_rate_hz: float = 20.0` field to `BLEManager.__init__`.
- Add `BLEManager.set_sample_rate(rate_hz: float)`:
  writes `1.0 / rate_hz` into `conn["poll_interval"]` for every active
  connection; stores `rate_hz` in `_sample_rate_hz` for sensors connected
  later.
- `_connect_thread_fn`: initialise `conn["poll_interval"] =
  1.0 / self._sample_rate_hz` before launching `_stream_sensor`.
- `_stream_sensor` polling loop: replace `time.sleep(0.05)` with
  ```python
  t_poll = time.time()
  val    = device.read_data(meas_name)
  elapsed = time.time() - t_poll
  time.sleep(max(0.0, conn["poll_interval"] - elapsed))
  ```
  Reading `conn["poll_interval"]` each iteration means a rate change
  set by `set_sample_rate()` takes effect on the very next sleep cycle.

**`main_window.py`:**
- Add `RatePanel` widget (sidebar, inserted between `SensorPanel` and the
  `SessionPanel` separator).
- Widget holds the ordered step list and current index; the high-rate
  indices are disabled and carry a tooltip.
- On step change: call `self._ble.set_sample_rate(rate_hz)`.
- No changes to `RecordingController`, `PlotPanel`, or `data_store.py`.

---

### Phase 2 — High-Rate Push Notifications (> 20 Hz)

> **Status:** implemented in `sensor_stream.py` (`PushStream`).  All details
> below are confirmed by BLE traffic capture and probe experiments.

#### Sensors and hardware limits

| Sensor | Model | MaxRate | MaxBurstRate | MaxBurstSamples |
|---|---|---|---|---|
| Wireless Voltage Sensor | PS-3211 | 1 000 Hz | 100 000 Hz | 1 000 |
| Wireless Current Sensor | PS-3212 | 1 000 Hz | 100 000 Hz | 1 000 |

#### Acquisition mechanisms

**Mechanism A — one-shot polling (Phase 1, currently used):**
Each call to `device.read_data(meas_name)` sends `GCMD_READ_ONE_SAMPLE`
(0x05) to the device, which responds with one measurement value.  Round-trip
over BLE on macOS: ~20–50 ms, giving ~20–50 Hz reliably and up to ~100 Hz
experimentally.

**Mechanism B — continuous push notifications (Phase 2):**
The sensor streams data autonomously at its configured internal rate.
Notification packets carry a 1-byte sequence counter (`data[0] <= 0x1F`,
values 0–31) followed by a continuous stream of uint16-LE samples spanning
packet boundaries.  The pasco library's `process_measurement_response`
already has a branch for this path, but no public API activates it.

**Commands and opcodes (confirmed by PacketLogger capture and probe
experiments):**

- `GCMD_READ_ONE_SAMPLE` (0x05): one-shot polling — responds on svc0
  RECV_CMD (h=38).  ~28 Hz pipeline.
- `0x06`: returns one RMS sample (~300 ms round-trip, ~3 Hz); the sensor
  must be ACKed before it accepts the next request.  **Does not start
  autonomous streaming.**
- `GCMD_XFER_BURST_RAM` (0x0E): RAM memory-read; used only in
  `read_factory_cal()`.  Not related to streaming.
- `WIRELESS_RMS_START = [0x37, 0x01, 0x00]`: sent only for
  `_dev_type == "Rotary Motion"` sensors.  No effect on voltage/current.
- `0x08`, `0x09`: return error responses.  `0x07`: no response.

**Confirmed streaming init sequence** (from PacketLogger capture of
SPARKvue at 2 kHz, targeting svc1 SEND_CMD `4a5c0001-0002-…`):

The pklg was recorded with two sensors running simultaneously, so the
svc0 commands appear interleaved.  The per-sensor sequence is:

```
Write svc1 SEND_CMD  [0x01, p0, p1, p2, p3, 0x02, 0x00]   # start-stream; p=period µs LE
sleep 50 ms
Write svc1 SEND_CMD  [0x28, 0x00, 0x00, N]                 # setup A
                                                             #   N=0x04 (Voltage PS-3211)
                                                             #   N=0x02 (Current PS-3212)
sleep 100 ms   # sensor sends [0xC0, 0x00, 0x28] ACK on svc1 RECV_CMD during this wait
Write svc1 SEND_CMD  [0x29, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00]  # setup B
sleep 100 ms
Write svc0 SEND_CMD  <svc0_cmd>    # sensor-specific 3-byte command:
                                   #   Voltage: [0x06, 0x5a, 0x00]
                                   #   Current: [0x06, 0x90, 0x03]
sleep 50 ms
Write svc0 SEND_CMD  [0x00]        # keepalive
sleep 50 ms
```

The delays between commands are **required**; sending them without delays
causes the sensor to produce no notifications.

**Stream-start command encoding** (`[0x01, period_lo, period_hi, 0x00, 0x00, 0x02, 0x00]`):
- Byte 0: `0x01` = start streaming.
- Bytes 1–4: sample period in µs as uint32 LE.
  e.g. 500 µs → `[0xf4, 0x01, 0x00, 0x00]` (2 kHz).
- Bytes 5–6: `0x02, 0x00` — suffix (purpose unknown; required).

**Notification packet format** (140 bytes per packet, on h=47 / `4a5c0001-0004-…`):
- Byte 0: sequence counter 0–31 (`data[0] <= 0x1F`).
- Bytes 1–139: continuous uint16-LE sample stream, spanning packet
  boundaries.  At 2 kHz, `data_size = 2` bytes per sample → 69–70
  samples per packet.

**ACK protocol** (to svc1 SEND_ACK / `4a5c0001-0005-…`):
```python
[0x00, 0x00, 0x00, 0x00, seq % 32]
```
The sensor **stops streaming after ~2 packets** if no ACK is received.
ACKs must be sent after each received packet (or at minimum every 8 packets
as SPARKvue does).

**GATT handle layout** (confirmed on intact PS-3211A / PS-3212):

_Note: the table lists both the characteristic-declaration handle and the
value handle.  Bleak operates on UUIDs and value handles; the declaration
handle is shown for reference only._

| Decl. | Value | UUID suffix | Direction | Role |
|---|---|---|---|---|
| h=35 | h=36 | `0000-0002` | write | svc0 SEND_CMD (one-shot polling) |
| h=37 | h=38 | `0000-0003` | notify | svc0 RECV_CMD (poll responses) |
| h=41 | h=42 | `0001-0002` | write | svc1 SEND_CMD (stream init) |
| h=43 | h=44 | `0001-0003` | notify | svc1 RECV_CMD (streaming acks) |
| h=46 | h=47 | `0001-0004` | notify | svc1 DATA (push stream packets) |
| h=49 | h=50 | `0001-0005` | write | svc1 SEND_ACK |

pasco UUID pattern: `4a5c000{service_id}-000{char_id}-0000-0000-5c1e741f1c00`
where `service_id` = 0 for svc0 (polling), 1 for svc1 (streaming).

**Fast calibration path** (Phase 2 implementation detail):
Rather than calling `_decode_data()` (which has significant overhead per
sample), pre-compute a linear slope/intercept at connect time from pasco's
XML `FactoryCal` parameters using `_calc_4_params(raw, x1, y1, x2, y2)`.
For the standard `Select → FactoryCal` chain used by voltage and current
sensors, calibrated value = `round(slope * raw_uint16 + intercept, precision)`.
This gives ~10× lower per-sample CPU overhead.

**Timestamp assignment for batched packets:**
```
t_sample_i = t_batch_received - n_samples_in_batch / rate + i / rate
```
where `t_batch_received` is the wall-clock time the notification callback
fired and `i` is the 0-based sample index within the batch.

#### BLE bandwidth

Each BLE notification packet carries up to ~120 two-byte samples
(ATT MTU ≈ 247 bytes).

| Sample rate | Packets/s | BLE interval needed | Feasibility |
|---|---|---|---|
| 500 Hz | ~5 | ~200 ms | Easy |
| 1 kHz | ~9 | ~112 ms | Easy |
| 5 kHz | ~42 | ~24 ms | Good |
| 10 kHz | ~84 | ~12 ms | Acceptable (BLE 4.x min ~7.5 ms) |
| 20 kHz | ~167 | ~6 ms | Needs BLE 5 / modern macOS |

Continuous push notifications have no dead zones; latency is one BLE
connection interval (~7.5–45 ms).

#### Phase 2 memory requirements

Ring buffer `MAXLEN` must be rate-adaptive:
`max(20_000, int(rate * LIVE_WINDOW_S * 1.5))`.
At 20 kHz × 20 s × 1.5 ≈ 600 000 samples per sensor (~9.6 MB as float64
pairs).  A 10-minute recording at 20 kHz = 24 000 000 samples per sensor
(~768 MB).  Users must be warned before starting a long high-rate recording.

#### Phase 2 architecture (implemented)

**`sensor_stream.py`:**
- `PushStream.stream()` runs the full svc1 init sequence and intercepts
  `DATA` characteristic notifications via bleak's internal callback dict.
- Calibration extracted once at stream start using pasco XML `FactoryCal`
  parameters; applied as `round(slope * raw_u16 + intercept, precision)`.
- ACK written to `SEND_ACK` every 8 packets; sensor stops within 2 packets
  if ACKs cease — stop is implicit (no explicit stop command required).

**`ble_manager.py`:**
- `set_sample_rate()` sets `restart_event` on every active connection.
- `_stream_sensor()` outer loop: calls `make_strategy(rate_hz)`, runs
  `strategy.stream()`, loops if `restart_event` set, exits otherwise.
- `LiveBuffer.resize()` called before each strategy run: capacity adapts
  to `max(20_000, int(rate_hz × LIVE_WINDOW_S × 1.5))`.

**`main_window.py`:**
- All rate steps now active; `RatePanel._apply()` emits `rate_changed` for
  every step without a greyed-out guard.
- `setDownsampling(auto=True, mode='peak')` applied to all curves at
  creation time; `mode='peak'` preserves spike features at high densities.

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
