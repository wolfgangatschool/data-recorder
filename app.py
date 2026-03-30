"""
Physics Data Recorder — main Streamlit application.

Layout
------
  Toolbar (fixed):  title | Live Discovery badge | Download CSV button
  Sidebar:          live sensor controls | CSV file upload | series visibility
  Main area:        time-series plot (one subplot per unit) | Record/Stop bar

Live sensor flow
----------------
  1. Background scan discovers PASCO BLE sensors (ble_manager._do_scan).
  2. User picks a sensor from the dropdown and clicks ＋.
  3. A background thread scans again, connects, and streams data into a ring
     buffer (ble_manager._connect_thread_fn → _stream_sensor).
  4. The plot is rebuilt and _flush_live_to_record copies new points into the
     recording buffer on every Streamlit rerun.
  5. User clicks Record / Stop to bracket a session, then downloads a CSV.
"""

import base64
import re
import time

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components

# ── BLE patches and management (optional — disabled if bleak/pasco not installed) ──
try:
    # ble_patches applies asyncio/pasco compatibility fixes on import (side-effects only).
    import ble_patches  # noqa: F401
    from ble_manager import (
        SCAN_INTERVAL_S,
        _sensor_display_label,
        _start_scan,
        _start_connect,
        _disconnect_sensor,
    )
    PASCO_AVAILABLE = True
except ImportError:
    PASCO_AVAILABLE = False

# Recording helpers are always available (no BLE dependency).
from recording import _start_recording, _stop_recording, _flush_live_to_record, _download_csv

# Inline SVG of the Bluetooth logo — the canonical symbol made from its two paths:
# a vertical stroke and the two right-pointing V-bumps that form the B shape.
# Used in sidebar markdown (unsafe_allow_html=True); the selectbox dropdown uses
# the plain-text ᛒ in _sensor_display_label since it only accepts a string.
_BT_SVG = (
    '<svg width="9" height="13" viewBox="0 0 10 16" fill="none" '
    'style="vertical-align:-2px;display:inline">'
    '<path d="M5 0 L5 16 M5 0 L9 4 L5 8 L9 12 L5 16" '
    'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>'
    '</svg>'
)


# ─────────────────────────────────────────────────────────────────────────────
# Page configuration
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Time Series Viewer", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

.block-container {
    padding-top: 3.5rem !important;
    padding-bottom: 0.5rem !important;
    padding-left: 1.5rem !important;
    padding-right: 1.5rem !important;
}

footer { visibility: hidden; }

/* Pin title into the toolbar strip */
h1 {
    position: fixed !important;
    top: 0.45rem !important;
    left: 4.5rem !important;
    z-index: 999990 !important;
    font-size: 1.3rem !important;
    font-weight: 600 !important;
    margin: 0 !important;
    line-height: 1 !important;
}

/* Hide the Streamlit deploy button */
.stAppDeployButton,
[data-testid="stAppDeployButton"] { display: none !important; }

[data-testid="stSidebar"] .block-container {
    padding-top: 1rem !important;
}

[data-testid="stCheckbox"] {
    margin-bottom: -0.3rem;
}

/* Hide the live_discovery toggle visually but keep it in the DOM so JS can
   click it.  visibility:hidden (not display:none) preserves the element. */
.st-key-live_discovery {
    visibility: hidden !important;
    position: absolute !important;
    height: 0 !important;
    overflow: hidden !important;
    padding: 0 !important;
    margin: 0 !important;
}
/* Collapse layout space for the hidden toggle and the JS injection iframe */
[data-testid="element-container"]:has(.st-key-live_discovery),
[data-testid="element-container"]:has(iframe) {
    height: 0 !important;
    min-height: 0 !important;
    overflow: hidden !important;
    padding: 0 !important;
    margin: 0 !important;
}

.sidebar-section {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #9ca3af;
    margin: 1rem 0 0.4rem 0;
}

/* Hide Streamlit's native running spinner and stop button */
[data-testid="stStatusWidget"] { display: none !important; }

/* Toolbar: Download CSV link */
.toolbar-dl {
    position: fixed;
    top: 2.875rem;
    right: 3.2rem;
    z-index: 999990;
}
.toolbar-dl a {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.25rem 0.65rem;
    border-radius: 0.4rem;
    border: 1px solid rgba(128,128,128,0.35);
    font-size: 0.8rem;
    font-weight: 500;
    text-decoration: none;
    color: inherit;
    background: transparent;
}
.toolbar-dl a:hover {
    border-color: rgba(128,128,128,0.65);
    background: rgba(128,128,128,0.07);
}

/* Toolbar: Live Discovery badge */
.toolbar-live {
    position: fixed;
    top: 0;
    right: 3.2rem;
    height: 2.875rem;
    z-index: 999990;
    display: inline-flex;
    align-items: center;
}
.toolbar-live-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.22rem 0.7rem;
    border-radius: 0.4rem;
    border: 1px solid;
    font-size: 0.8rem;
    font-weight: 500;
    cursor: pointer;
    user-select: none;
    transition: opacity 0.15s;
}
.toolbar-live-badge:hover { opacity: 0.75; }
.toolbar-live.on .toolbar-live-badge {
    border-color: #10b981;
    color: #10b981;
    background: rgba(16,185,129,0.07);
}
.toolbar-live.off .toolbar-live-badge {
    border-color: rgba(128,128,128,0.3);
    color: rgba(128,128,128,0.4);
    background: transparent;
}

/* Wifi arcs animate outward when actively scanning */
@keyframes wifi-emit {
    0%, 100% { opacity: 0.15; }
    50%       { opacity: 1;    }
}
.toolbar-live.on.scanning .arc { animation: wifi-emit 1.4s ease-in-out infinite; }
.toolbar-live.on.scanning .a1  { animation-delay: 0s;    }
.toolbar-live.on.scanning .a2  { animation-delay: 0.28s; }
.toolbar-live.on.scanning .a3  { animation-delay: 0.56s; }
.toolbar-live.off .arc          { opacity: 0.25; }
</style>
""", unsafe_allow_html=True)

st.title("Time Series Viewer")


# ─────────────────────────────────────────────────────────────────────────────
# Toolbar: Live Discovery badge
# ─────────────────────────────────────────────────────────────────────────────

def _render_live_indicator() -> None:
    """
    Render the fixed-position Live Discovery badge in the toolbar.

    The badge is a styled HTML element.  A hidden Streamlit toggle holds the
    boolean state, and a small JS snippet wires a click on the badge to a
    programmatic click on the toggle — this is the only reliable way to update
    Streamlit state from an arbitrary DOM element without a form submission.
    """
    _live     = st.session_state.get("live_discovery", True)
    _scanning = PASCO_AVAILABLE and st.session_state.get("scan_state", {}).get("in_progress", False)
    _cls      = ("on" + (" scanning" if _scanning else "")) if _live else "off"
    _wifi_svg = (
        '<svg width="15" height="13" viewBox="0 0 20 16" fill="none" '
        'stroke="currentColor" stroke-width="2.2" stroke-linecap="round">'
        '<path class="arc a3" d="M1 7 Q10 0 19 7"/>'
        '<path class="arc a2" d="M4.5 10.5 Q10 5.5 15.5 10.5"/>'
        '<path class="arc a1" d="M8 13.5 Q10 11.5 12 13.5"/>'
        '<circle cx="10" cy="15.2" r="1.4" fill="currentColor" stroke="none"/>'
        '</svg>'
    )
    _label = "Live Discovery" if _live else "Discovery off"
    st.markdown(
        f'<div class="toolbar-live {_cls}" id="tlc">'
        f'<div class="toolbar-live-badge">{_wifi_svg}&nbsp;{_label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if PASCO_AVAILABLE:
        # Hidden toggle that Streamlit uses to manage the live_discovery boolean.
        st.toggle("Live sensor discovery", key="live_discovery",
                  label_visibility="collapsed")
        # Wire badge click → toggle click via a <script> injected into the
        # parent document.  A plain iframe script cannot trigger React's
        # synthetic events in the parent; injecting into document.head can.
        components.html("""<script>
(function(){
  var d = window.parent.document;
  var tlc = d.getElementById('tlc');
  if (!tlc || tlc._wired) return;
  tlc._wired = true;
  tlc.addEventListener('click', function() {
    var s = d.createElement('script');
    s.textContent = [
      '(function(){',
      '  var c = document.querySelector(".st-key-live_discovery");',
      '  if (!c) return;',
      '  var e = c.querySelector("label") || c.querySelector("input") || c;',
      '  e.click();',
      '})();'
    ].join('');
    d.head.appendChild(s);
    s.remove();
  });
})();
</script>""", height=0)


# ─────────────────────────────────────────────────────────────────────────────
# Session state initialisation
# ─────────────────────────────────────────────────────────────────────────────

# managed_addrs : set[str]        addresses currently in the sensor list
#                                  modified ONLY by on_click callbacks, never by threads
# connections   : dict[str, dict] per-connection details; background thread writes here
# sensor_meta   : dict[str, dict] metadata cache for display (dropdown fallback + sensor list identity)
# live_buffers  : {unit: {addr: deque[(elapsed_s, value)]}}
# record_data   : {unit: {addr: [(t, v), …]}}
# record_last_t : {(unit, addr): float}  watermark for flush
# scan_state    : {in_progress, last_time, results}

st.session_state.setdefault("managed_addrs",  set())
st.session_state.setdefault("connections",     {})
st.session_state.setdefault("sensor_meta",     {})
st.session_state.setdefault("live_buffers",    {})
st.session_state.setdefault("record_data",     {})
st.session_state.setdefault("record_last_t",   {})
st.session_state.setdefault("recording",       False)
st.session_state.setdefault("scan_state",      {"in_progress": False, "last_time": 0.0, "results": []})
st.session_state.setdefault("live_discovery",  True)
# x_range_initialized: True after the plot has been shown once with [0, 10].
# Reset to False when all units disappear so the next sensor connection re-initialises.
st.session_state.setdefault("x_range_initialized", False)
# recorded_sessions: list of finalised recording sessions, each a dict with
# keys: id, label (HH:MM:SS), data ({unit: {sensor_key: [(t,v)]}}), sensor_labels.
st.session_state.setdefault("recorded_sessions",    [])
# record_sensor_labels: {sensor_key: label} populated during flush so that
# labels are captured even if the sensor disconnects before Stop is pressed.
st.session_state.setdefault("record_sensor_labels", {})

_render_live_indicator()

# Safety sweep: when a streaming thread exits unexpectedly (sensor turned off
# or out of range) it writes conn["status"] = "disconnected" to the conn dict
# it holds.  This sweep detects that signal and calls _disconnect_sensor so the
# address is removed from managed_addrs and the sensor reappears in the dropdown.
if PASCO_AVAILABLE:
    _stale_addrs = [
        addr for addr in list(st.session_state.managed_addrs)
        if st.session_state.connections.get(addr, {}).get("status") == "disconnected"
    ]
    if _stale_addrs:
        for _addr in _stale_addrs:
            _disconnect_sensor(_addr)


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: CSV file upload  (shown first)
# ─────────────────────────────────────────────────────────────────────────────

st.sidebar.markdown('<p class="sidebar-section">Load recorded data</p>', unsafe_allow_html=True)
uploaded = st.sidebar.file_uploader("Load CSV", type="csv", label_visibility="collapsed")


@st.cache_data
def _load_csv(source) -> pd.DataFrame:
    return pd.read_csv(source, low_memory=False)


raw = _load_csv(uploaded) if uploaded else None


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: live sensor controls
# ─────────────────────────────────────────────────────────────────────────────

if PASCO_AVAILABLE:
    scan_state = st.session_state.scan_state
    _live      = st.session_state.live_discovery

    # Auto-scan on first load and every SCAN_INTERVAL_S seconds when live discovery is on.
    if _live and not scan_state["in_progress"] and (
        scan_state["last_time"] == 0.0
        or time.time() - scan_state["last_time"] >= SCAN_INTERVAL_S
    ):
        _start_scan()

    _now = time.time()

    # Update sensor_meta with fresh scan results (for sensors not currently managed).
    # This keeps metadata up-to-date with live ble_device objects for reconnection.
    for _entry in scan_state.get("results", []):
        _addr = _entry["address"]
        if _addr not in st.session_state.managed_addrs:
            st.session_state.sensor_meta[_addr] = {**_entry, "_last_seen_at": _now}

    # Expire sensor_meta entries that are not managed and have not been seen in
    # recent scans (device is likely off).
    for _addr in list(st.session_state.sensor_meta):
        _meta = st.session_state.sensor_meta[_addr]
        if (_addr not in st.session_state.managed_addrs
                and _addr not in {e["address"] for e in scan_state.get("results", [])}
                and _now - _meta.get("_last_seen_at", _now) > SCAN_INTERVAL_S * 2):
            del st.session_state.sensor_meta[_addr]

    st.sidebar.markdown('<p class="sidebar-section">Live sensors</p>', unsafe_allow_html=True)

    col_cap, col_ref = st.sidebar.columns([3, 1])
    with col_cap:
        if scan_state["last_time"] > 0:
            ts = time.strftime("%H:%M:%S", time.localtime(scan_state["last_time"]))
            st.caption(f"Last scan at {ts}")
        elif scan_state["in_progress"]:
            st.caption("Scanning…")
        else:
            st.caption("No live sensors available")
    with col_ref:
        st.button("↺", key="btn_refresh_scan", help="Scan for sensors now",
                  width="stretch", on_click=_start_scan)

    # Build the available-sensor list (sensors NOT in managed_addrs).
    available = []
    _seen_in_available = set()
    for _entry in scan_state.get("results", []):
        if _entry["address"] not in st.session_state.managed_addrs:
            available.append(_entry)
            _seen_in_available.add(_entry["address"])
    for _addr, _meta in st.session_state.sensor_meta.items():
        if _addr not in st.session_state.managed_addrs and _addr not in _seen_in_available:
            available.append({**_meta, "ble_device": None})

    # Reserve two sidebar slots in visual order (dropdown on top, sensor list below).
    # The slots are filled in REVERSE order so the sensor-list delta reaches the
    # browser before the dropdown delta — eliminating the brief window where a
    # disconnecting sensor would appear in both places simultaneously.
    _dropdown_slot     = st.sidebar.empty()
    _sensor_list_slot  = st.sidebar.empty()

    # ── Fill sensor list first ────────────────────────────────────────────────
    if st.session_state.managed_addrs:
        with _sensor_list_slot.container():
            st.markdown("---")
            for _addr in sorted(st.session_state.managed_addrs):
                _conn = st.session_state.connections.get(_addr, {})
                _meta = st.session_state.sensor_meta.get(_addr, {})
                _status = _conn.get("status", "")
                _icon   = ("🟢" if _status == "connected" else
                           "🔴" if "error" in _status else "🟡")
                col_lbl, col_m = st.columns([3, 1])
                _lbl = (f"{_icon} **{_meta.get('quantity', '?')}** "
                        f"ᛒ {_meta.get('sensor_id', '')}")
                if _status not in ("connected",):
                    _lbl += f"  \n*{_status}*"
                col_lbl.markdown(_lbl)
                col_m.button("－", key=f"disc_{_addr}", help="Disconnect",
                             width="stretch",
                             disabled=(_status == "disconnecting…"),
                             on_click=_disconnect_sensor, args=(_addr,))
    else:
        _sensor_list_slot.empty()

    # ── Fill dropdown second ──────────────────────────────────────────────────
    with _dropdown_slot.container():
        col_sel, col_add = st.columns([3, 1])
        if available:
            _saved_addr  = st.session_state.get("sel_sensor_addr")
            _avail_addrs = [s["address"] for s in available]
            if _saved_addr not in _avail_addrs:
                st.session_state["sel_sensor_addr"] = _avail_addrs[0]
                _saved_addr = _avail_addrs[0]
            _cur_idx = _avail_addrs.index(_saved_addr)
            new_idx = col_sel.selectbox(
                "Sensor",
                options=range(len(available)),
                index=_cur_idx,
                format_func=lambda i: _sensor_display_label(available[i]),
                label_visibility="collapsed",
            )
            st.session_state["sel_sensor_addr"] = available[new_idx]["address"]
            col_add.button("＋", key="btn_connect", width="stretch",
                           help="Connect selected sensor",
                           on_click=_start_connect, args=(available[new_idx],))
        else:
            placeholder = "Scanning…" if st.session_state.live_discovery else "No sensors available"
            col_sel.selectbox(
                "Sensor", [placeholder],
                label_visibility="collapsed",
                disabled=True,
                key="sel_sensor_placeholder",
            )
            col_add.button("＋", key="btn_connect", width="stretch", disabled=True)

else:
    st.sidebar.caption("Install `pasco` to enable live sensors.")


# ─────────────────────────────────────────────────────────────────────────────
# Parse CSV column structure into series metadata
# ─────────────────────────────────────────────────────────────────────────────

# Expected column formats (Pasco / SPARKvue CSV export):
#   "Voltage (V) Run 1"   → MEAS_RE  (measurement column)
#   "Time (s) Run 1"      → TIME_RE  (time column)
MEAS_RE = re.compile(r"^(.+?)\s*\((.+?)\)\s*Run\s*(\d+)$", re.IGNORECASE)
TIME_RE  = re.compile(r"^Time\s*\((.+?)\)\s*Run\s*(\d+)$",  re.IGNORECASE)

series_meta: dict[tuple[str, int], dict] = {}

if raw is not None:
    for col in raw.columns:
        if TIME_RE.match(col.strip()):
            continue  # skip time columns; they are referenced by measurement columns
        m = MEAS_RE.match(col.strip())
        if not m:
            continue
        name, unit, run_str = m.group(1).strip(), m.group(2).strip(), int(m.group(3))
        # Find the matching time column for this run number.
        time_col = next(
            (c for c in raw.columns
             if TIME_RE.match(c.strip()) and TIME_RE.match(c.strip()).group(2) == str(run_str)),
            None,
        )
        series_meta[(unit, run_str)] = {
            "label":     f"Run {run_str}",
            "name":      name,
            "unit":      unit,
            "run":       run_str,
            "time_col":  time_col,
            "value_col": col.strip(),
        }

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: selected measurements (CSV runs + recorded sessions)
# ─────────────────────────────────────────────────────────────────────────────

# csv_run_visible: {run_number: bool} — one checkbox per CSV run.
# rec_session_visible: {session_id: bool} — one checkbox per recorded session.
# No unit headers: a measurement may contain multiple signals across units.
csv_run_visible:     dict[int, bool] = {}
rec_session_visible: dict[str, bool] = {}

_has_csv_runs = bool(series_meta)
_has_recorded = bool(st.session_state.recorded_sessions)

if _has_csv_runs or _has_recorded:
    st.sidebar.markdown(
        '<p class="sidebar-section">Selected measurements</p>', unsafe_allow_html=True
    )

    # CSV runs — one entry per run number (covers all units in that run).
    if _has_csv_runs:
        for run in sorted({meta["run"] for meta in series_meta.values()}):
            csv_run_visible[run] = st.sidebar.checkbox(
                f"Run {run}", value=True, key=f"csv_run_{run}"
            )

    if _has_csv_runs and _has_recorded:
        st.sidebar.markdown("---")

    # Recorded sessions — labelled with the HH:MM:SS of when recording stopped.
    for session in st.session_state.recorded_sessions:
        rec_session_visible[session["id"]] = st.sidebar.checkbox(
            session["label"], value=True, key=f"rec_{session['id']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Collect all units that have visible data → unit_list drives the plot layout
# ─────────────────────────────────────────────────────────────────────────────

_plot_units: set[str] = set()

# CSV: include units from visible runs.
for (unit, run) in series_meta:
    if csv_run_visible.get(run, True):
        _plot_units.add(unit)

# Recorded sessions: include units from visible sessions.
for session in st.session_state.recorded_sessions:
    if rec_session_visible.get(session["id"], True):
        _plot_units.update(session["data"].keys())

# Live sensors: include units from actively streaming sensors.
# Only count buffers whose address is still a managed sensor — orphaned buffers
# from zombie connect threads must not create ghost subplots.
for live_unit, addr_bufs in st.session_state.live_buffers.items():
    if any(a in st.session_state.managed_addrs for a in addr_bufs):
        _plot_units.add(live_unit)

# Display order: V first, A second, then alphabetical for anything else.
_UNIT_ORDER = ["V", "A"]
unit_list = (
    [u for u in _UNIT_ORDER if u in _plot_units] +
    [u for u in sorted(_plot_units) if u not in _UNIT_ORDER]
)


# ─────────────────────────────────────────────────────────────────────────────
# Empty state — nothing to show yet
# ─────────────────────────────────────────────────────────────────────────────

if not unit_list:
    st.session_state.x_range_initialized = False
    if PASCO_AVAILABLE:
        _ss = st.session_state.scan_state
        _conn_pending = any(
            st.session_state.connections.get(addr, {}).get("status")
            not in ("connected", "disconnected")
            and "error" not in st.session_state.connections.get(addr, {}).get("status", "")
            for addr in st.session_state.managed_addrs
        )
        # While connecting, stay silent — the sensor list in the sidebar already
        # shows the status.  Only show the hint when there is genuinely nothing happening.
        if _ss["in_progress"] or _conn_pending:
            time.sleep(0.3)
            st.rerun()
        else:
            st.info("Connect a sensor or load a CSV to see data.")
            if st.session_state.live_discovery and _ss["last_time"] > 0:
                time_since = time.time() - _ss["last_time"]
                time.sleep(min(1.0, max(0.1, SCAN_INTERVAL_S - time_since)))
                st.rerun()
    else:
        st.info("Connect a sensor or load a CSV to see data.")
    st.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Flush live data into the recording buffer (every render pass)
# ─────────────────────────────────────────────────────────────────────────────

_flush_live_to_record()


# ─────────────────────────────────────────────────────────────────────────────
# Toolbar: Download CSV link
# ─────────────────────────────────────────────────────────────────────────────

if st.session_state.recorded_sessions:
    _b64 = base64.b64encode(_download_csv()).decode()
    _dl_html = (
        f'<div class="toolbar-dl">'
        f'<a href="data:text/csv;base64,{_b64}" download="recording.csv">↓ Download CSV</a>'
        f'</div>'
    )
else:
    _dl_html = (
        '<div class="toolbar-dl">'
        '<a style="opacity:0.35;cursor:default;pointer-events:none;">↓ Download CSV</a>'
        '</div>'
    )
st.markdown(_dl_html, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Record / Stop bar
# ─────────────────────────────────────────────────────────────────────────────

_any_connected = any(
    st.session_state.connections.get(addr, {}).get("status") == "connected"
    for addr in st.session_state.managed_addrs
)

rec_left, rec_mid, rec_right = st.columns([2, 1, 2])
with rec_mid:
    if st.session_state.recording:
        rec_pts = sum(
            len(pts)
            for sensor_map in st.session_state.record_data.values()
            for pts in sensor_map.values()
        )
        st.button(
            f"■  Stop  ({rec_pts} pts)",
            key="btn_stop_rec",
            width="stretch",
            type="primary",
            on_click=_stop_recording,
        )
    elif _any_connected:
        st.button(
            "●  Record",
            key="btn_start_rec",
            width="stretch",
            on_click=_start_recording,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Plot
# ─────────────────────────────────────────────────────────────────────────────

# Colour palettes — static data (CSV + recorded) uses cool/neutral colours;
# live sensor data uses vivid dotted lines so it is visually distinct.
FILE_COLORS = [
    "#3b82f6", "#f97316", "#10b981", "#ef4444", "#8b5cf6",
    "#06b6d4", "#f59e0b", "#6366f1", "#84cc16", "#ec4899",
]
LIVE_COLORS = [
    "#ff006e", "#fb5607", "#ffbe0b", "#8338ec", "#3a86ff",
]

# Pre-compute stable colors for CSV runs and recorded sessions so that the
# same run/session always gets the same color regardless of which subplot is
# being rendered.  Live sensors use LIVE_COLORS independently.
_color_idx      = 0
csv_run_colors: dict[int, str] = {}
for _run in sorted(csv_run_visible):
    if csv_run_visible[_run]:
        csv_run_colors[_run] = FILE_COLORS[_color_idx % len(FILE_COLORS)]
        _color_idx          += 1

session_colors: dict[str, str] = {}
for _sess in st.session_state.recorded_sessions:
    if rec_session_visible.get(_sess["id"], True):
        session_colors[_sess["id"]] = FILE_COLORS[_color_idx % len(FILE_COLORS)]
        _color_idx                 += 1


def _unit_display_name(unit: str) -> str:
    """Return a human-readable measurement name for the given physical unit.

    Checks CSV metadata first (has explicit names like 'Voltage'), then live
    sensors, then recorded session labels.  Falls back to the unit string.
    """
    for (u, _), meta in series_meta.items():
        if u == unit:
            return meta["name"]
    for addr in st.session_state.managed_addrs:
        if st.session_state.connections.get(addr, {}).get("unit") == unit:
            return st.session_state.sensor_meta.get(addr, {}).get("quantity", unit)
    for _sess in st.session_state.recorded_sessions:
        for u, sensor_map in _sess["data"].items():
            if u == unit:
                for sk in sensor_map:
                    lbl = _sess["sensor_labels"].get(sk, "")
                    if lbl:
                        return lbl
    return unit


n_units   = len(unit_list)
# Wider spacing than the default 0.06: every subplot shows its own x-axis labels,
# so we need room for the tick labels and axis title between rows.
SPACING   = 0.12

fig = make_subplots(rows=n_units, cols=1, shared_xaxes=True, vertical_spacing=SPACING)

plot_h        = (1.0 - SPACING * (n_units - 1)) / n_units
FIG_HEIGHT    = 400 * n_units
MODEBAR_PX    = 44
MODEBAR_PAPER = MODEBAR_PX / FIG_HEIGHT   # modebar height in paper-space units

# Build one legend per subplot so that each unit has its own independent legend.
legend_layout: dict = {}
for idx, unit in enumerate(unit_list):
    row        = idx + 1
    top_y      = 1.0 - (row - 1) * (plot_h + SPACING)
    if idx == 0:
        top_y -= MODEBAR_PAPER  # leave room for the modebar above the first subplot
    legend_key = "legend" if idx == 0 else f"legend{idx + 1}"
    full_name  = _unit_display_name(unit)
    legend_layout[legend_key] = dict(
        x=1.01, xanchor="left",
        y=top_y, yanchor="top",
        orientation="v",
        title_text=f"{full_name} [{unit}]",
        title_font=dict(size=13),
        font=dict(size=13),
    )

# Crosshair spike lines shown on hover (shared across all subplots via shared_xaxes).
SPIKE = dict(
    showspikes=True, spikemode="across", spikesnap="cursor",
    spikethickness=0.75, spikecolor="rgba(160,160,160,0.45)", spikedash="solid",
)

for row, unit in enumerate(unit_list, start=1):
    legend_ref = "legend" if row == 1 else f"legend{row}"

    # ── CSV traces (solid lines, color per run) ───────────────────────────────
    for run in sorted(run for (u, run) in series_meta if u == unit and csv_run_visible.get(run, True)):
        meta     = series_meta[(unit, run)]
        time_col = meta["time_col"]
        if time_col is None or raw is None:
            continue
        t    = pd.to_numeric(raw[time_col],          errors="coerce")
        v    = pd.to_numeric(raw[meta["value_col"]], errors="coerce")
        mask = t.notna() & v.notna()
        fig.add_trace(go.Scatter(
            x=t[mask], y=v[mask],
            mode="lines",
            name=meta["label"],                  # "Run N"
            legend=legend_ref,
            line=dict(color=csv_run_colors[run], width=1.5),
        ), row=row, col=1)

    # ── Recorded session traces (solid lines, color per session) ──────────────
    for session in st.session_state.recorded_sessions:
        if not rec_session_visible.get(session["id"], True):
            continue
        sensor_map = session["data"].get(unit, {})
        if not sensor_map:
            continue
        color = session_colors[session["id"]]
        for sensor_key, pts in sensor_map.items():
            sensor_label = session["sensor_labels"].get(sensor_key, sensor_key[-6:])
            # Include sensor label in the trace name only when a session has
            # multiple sensors for the same unit (unusual but possible).
            trace_name = (
                f"{session['label']} — {sensor_label}"
                if len(sensor_map) > 1
                else session["label"]
            )
            fig.add_trace(go.Scatter(
                x=[p[0] for p in pts],
                y=[p[1] for p in pts],
                mode="lines",
                name=trace_name,
                legend=legend_ref,
                line=dict(color=color, width=1.5),
            ), row=row, col=1)

    # ── Live traces (dotted lines, vivid colors) ──────────────────────────────
    # Ephemeral: only rendered while the sensor is actively connected.
    addr_bufs = st.session_state.live_buffers.get(unit, {})
    for j, (addr, buf) in enumerate(addr_bufs.items()):
        if addr not in st.session_state.managed_addrs or not buf:
            continue
        label = st.session_state.connections.get(addr, {}).get("label", addr[-6:])
        pts = list(buf)  # snapshot — streaming thread may append concurrently
        fig.add_trace(go.Scatter(
            x=[p[0] for p in pts],
            y=[p[1] for p in pts],
            mode="lines",
            name=label,
            legend=legend_ref,
            line=dict(color=LIVE_COLORS[j % len(LIVE_COLORS)], width=2, dash="dot"),
        ), row=row, col=1)

    # ── Axis labels ───────────────────────────────────────────────────────────
    meas_name = _unit_display_name(unit)
    fig.update_yaxes(
        title_text=f"{meas_name} [{unit}]",
        title_font=dict(size=13), tickfont=dict(size=12),
        fixedrange=False, **SPIKE, row=row, col=1,
    )
    fig.update_xaxes(
        title_text="Time [s]",
        title_font=dict(size=13), tickfont=dict(size=12),
        showticklabels=True,  # show on every row (shared_xaxes hides non-bottom rows by default)
        **SPIKE, row=row, col=1,
    )

fig.update_layout(
    font=dict(family="Inter, sans-serif", size=13),
    hovermode="x unified",
    hoverlabel=dict(font_size=13, font_family="Inter, sans-serif"),
    dragmode="zoom",
    height=FIG_HEIGHT,
    spikedistance=-1,
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=10, r=145, t=10, b=40),
    # uirevision: when this value stays constant across reruns, Plotly.js preserves
    # the user's zoom/pan state even though the Python figure is rebuilt every time.
    # This means "don't recreate the view for existing plots" — only new units get
    # the default initialisation below.
    uirevision="timeseries",
    **legend_layout,
)

# Initialise the x-axis to 10 s on the very first render.  After that, uirevision
# prevents Plotly.js from resetting whatever range the user has set.
# shared_xaxes=True means this range applies to all subplots simultaneously.
if not st.session_state.x_range_initialized:
    fig.update_xaxes(range=[0, 10])
    st.session_state.x_range_initialized = True

st.plotly_chart(fig, width="stretch", key="main_plot")


# ─────────────────────────────────────────────────────────────────────────────
# Raw data preview (CSV only)
# ─────────────────────────────────────────────────────────────────────────────

if raw is not None:
    with st.expander("Raw data"):
        st.dataframe(raw, width="stretch")


# ─────────────────────────────────────────────────────────────────────────────
# Auto-refresh
# ─────────────────────────────────────────────────────────────────────────────

# Rerun while any sensor is connecting or a scan is in progress so the UI
# stays responsive without the user having to interact.
_sensors_transitioning = any(
    st.session_state.connections.get(addr, {}).get("status")
    not in ("connected", "disconnected")
    and "error" not in st.session_state.connections.get(addr, {}).get("status", "")
    for addr in st.session_state.managed_addrs
)
_scan_active = PASCO_AVAILABLE and st.session_state.scan_state["in_progress"]

if _scan_active or _sensors_transitioning or (_any_connected and st.session_state.recording):
    time.sleep(0.3)
    st.rerun()
