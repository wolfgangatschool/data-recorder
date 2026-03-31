# Physics Data Recorder — Requirements

## Functional Requirements

### BLE Sensor Discovery
- On startup, automatically scan for PASCO BLE sensors.
- In "Live Discovery" mode, re-scan periodically (currently every 15 s).
- A manual refresh button triggers an immediate scan.
- A "Live Discovery" toggle in the toolbar enables/disables auto-scanning.

### Sensor Connection
- Sidebar shows a dropdown of discovered, unmanaged sensors.
- User clicks ＋ (moouse over: Connect) to connect a sensor; connection runs on a background thread.
- Multiple sensors can be connected simultaneously.
- A sensor must **never** appear in both the dropdown and the connected list at the same time.
- Immediately after the start of the connection process, the sensor is removed from the dropdown and added to the connected list. The status of the sensor in the connected list is 🟡 (transitioning) with the message "connecting...".
- While disconnecting, the sensor stays in the connected list (showing 🟡 (transitioning) with the status message "disconnecting…"). The sensor is only added back to the dropdown, when the disconnection process is complete and it was removed from the connected list.

### Sensor Status Display
- Each connected sensor shows a status indicator: 🟢 connected / 🟡 transitioning / 🔴 error.
- The disconnect button (－) is disabled while the sensor is disconnecting.
- After a sensor disconnects (or errors), it is removed from the list and reappears in the dropdown.

### Live Data Streaming
- Connected sensors stream data continuously into a per-sensor ring buffer of a initially fixed size (constant `LIVE_WINDOW_S`).
- Live (unrecorded) signal is shown in the plot as **grey, semi-opaque** lines
- Plot window is initially fixed at **20 s** wide (constant `LIVE_WINDOW_S`), scrolling so the newest sample is always visible on the right edge. 
- When not recording, the 0s marker of the time axis of a plot is aligned with the newest sample.

### Recording
- "Record" button starts a recording session; "Stop" finalises it.
- Only samples arriving after Record is pressed are captured (for being persisted after the "Stop" of the recording).
- The timestamp of the first recorded sample is always 0 and subsequent samples receive timestamps (unit seconds) relative to the first sample of the recording.
- The previous requirement implies that the time axis of a plot is aligned with the increasing timestamp of the newest sample when recording, i.e. the axis is scrolling with the recorded data. 
- Every session is uniquely identified by the date-time stamp of its start.
- Every session holds as many signals as there are sensors connected, i.e. one signal per sensor.
- Multiple sessions can be recorded sequentially.
- Each session appears in the sidebar with a HH:MM:SS, DD-MM-YY label indicating the session start, including a visibility checkbox and a remove (-) button aligned to the right.
- Recorded traces appear as **vivid coloured** solid lines in the plot (one colour per session)

### CSV Import
- User can upload a Pasco/SPARKvue CSV file via the sidebar
- Each run in the CSV appears as a separate entry with a visibility checkbox
- CSV runs are plotted as vivid coloured solid lines, the colours are assigned in a way that they are distinct from existing signals, recorded or loaded.

### Plot Layout
- One subplot per physical unit (e.g. V, A); units share a common x-axis
- Subplots stacked vertically; each has its own legend and y-axis label
- Crosshair spike lines shown on hover across all subplots
- When no live sensors are connected, the user can pan/zoom freely and the view is preserved between reruns

### Data Export
- "Download CSV" link in the toolbar; active only when at least one recorded session exists, or if there is loaded data.
- Exported columns: session_start, sensor_id_1, time_s_1, value_1, unit_1, sensor_id_2, time_s_2, value_2, unit_2, ... , sensor_id_n, time_s_n, value_n, unit_n. If a timestamp does not exist for a specific sensor_id_i (i = 1...n) then the unknown entries, i.e. time_s_i and value_i shall be set to nan (not a number)?
- Hint for extended features to be implemented: the CSV export will be replaced by an more efficient format to store time series data.
---

## Non-Functional Requirements

### UI Responsiveness
- **No flickering** of any UI element (toolbar, sidebar, plot modebar) during live data updates or live sensor discovery
- Plot scrolls smoothly at approximately 3 fps during live streaming.
- Sensor connection/disconnection status must update in the sidebar promptly

### Thread Safety
- All BLE I/O is serialised via a single lock (one CoreBluetooth manager active at a time)
- Background streaming threads **never** access `st.session_state`; they only write to a plain dict passed by reference
- The main thread is the sole writer of `managed_addrs` and `connections`

---

## Key Architectural Constraints

### Streamlit Rendering Model
- The entire script re-executes on every user interaction (button click, widget change)
- `st.rerun()` interrupts the current render mid-execution; code after it never runs in that pass
- Streamlit streams DOM deltas to the browser incrementally as the script executes (not batched at the end)
- **This means**: a full-script rerun every 0.3 s replaces every DOM node — including fixed-position toolbar elements — causing visible flickering

### Fragment Approach (tried, reverted)
- `@st.fragment(run_every=0.3)` isolates the plot rerun from the outer script
- **Problem observed**: increased connection latency; existing connect/disconnect behavior broke
- Root cause not yet diagnosed — needs careful investigation before retry

### State Communication
- `managed_addrs` (set): single source of truth for "is this sensor managed?" — written only on the main thread
- `connections` (dict): per-sensor status/data written by both main thread and background threads
- `live_buffers` (dict of deques): written by streaming threads, read by main thread for plotting
- `sensor_meta` (dict): metadata cache for display, written by main thread from scan results

---

## Open Questions / To-Do

1. **Flickering fix**: find an approach that isolates the 0.3 s plot refresh from the full page rerun *without* breaking connect/disconnect behavior. Options to evaluate:
   - `@st.fragment` with careful scoping (previous attempt broke things — needs diagnosis)
   - Render the toolbar elements via JavaScript/CSS so they are not replaced by Streamlit reruns
   - Reduce what the 0.3 s rerun touches (e.g. only update the `st.empty()` chart container)

2. **Faster sensor discovery**: reduce scan interval and/or pass a shorter scan duration to `pasco.scan()` if the library supports it

3. **Recording workflow**: verify that the Record/Stop button inside a fragment triggers a full rerun to update the sidebar "Selected measurements" section
