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

# sensors:       {key: {status, label, unit, meas_name, stop_event, thread, …}}
# live_buffers:  {unit: {sensor_key: deque[(elapsed_s, value)]}}
# record_data:   {unit: {sensor_key: [(t, v), …]}}
# record_last_t: {(unit, sensor_key): float}  — watermark for flush
# scan_state:    {in_progress: bool, last_time: float, results: [parsed-device-dict]}
# known_sensors: {address: parsed-device-dict}  — persists across scans

st.session_state.setdefault("sensors",       {})
st.session_state.setdefault("live_buffers",  {})
st.session_state.setdefault("record_data",   {})
st.session_state.setdefault("record_last_t", {})
st.session_state.setdefault("recording",     False)
st.session_state.setdefault("sensor_counter", 0)
st.session_state.setdefault("scan_state",    {"in_progress": False, "last_time": 0.0, "results": []})
st.session_state.setdefault("known_sensors", {})  # address → entry; populated by scans
st.session_state.setdefault("live_discovery", True)

_render_live_indicator()


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

    st.sidebar.markdown('<p class="sidebar-section">Live sensors</p>', unsafe_allow_html=True)

    # Scan status caption + manual refresh button
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
        if st.button("↺", key="btn_refresh_scan", help="Scan for sensors now", width="stretch"):
            _start_scan()
            st.rerun()

    # Sensor dropdown — exclude sensors that are already connected (by address).
    connected_addrs = {
        info["address"]
        for info in st.session_state.sensors.values()
        if "address" in info
    }
    available = [
        s for s in st.session_state.known_sensors.values()
        if s["address"] not in connected_addrs
    ]

    col_sel, col_add = st.sidebar.columns([3, 1])
    if available:
        # Track the current selection by address (not list index) so that the
        # displayed item stays correct when the list changes between reruns.
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

        if col_add.button("＋", key="btn_connect", width="stretch", help="Connect selected sensor"):
            _start_connect(available[new_idx])
            st.rerun()
    else:
        # Nothing to select — show a disabled placeholder.
        placeholder = "Scanning…" if st.session_state.live_discovery else "No sensors available"
        col_sel.selectbox(
            "Sensor", [placeholder],
            label_visibility="collapsed",
            disabled=True,
            key="sel_sensor_placeholder",
        )
        col_add.button("＋", key="btn_connect", width="stretch", disabled=True)

    # Connected sensor list with status indicators and disconnect buttons.
    if st.session_state.sensors:
        st.sidebar.markdown("---")
        for key, info in list(st.session_state.sensors.items()):
            status = info.get("status", "")
            icon   = ("🟢" if status == "connected" else
                      "🔴" if "error" in status else "🟡")
            col_lbl, col_m = st.sidebar.columns([3, 1])
            label = f"{icon} **{info.get('quantity', '?')}** {_BT_SVG} {info.get('sensor_id', '')}"
            if status not in ("connected",):
                label += f"  \n*{status}*"
            col_lbl.markdown(label, unsafe_allow_html=True)
            if col_m.button("－", key=f"disc_{key}", help="Disconnect", width="stretch"):
                _disconnect_sensor(key)
                st.rerun()

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

# Build a per-unit series list from the parsed metadata.
units: dict[str, list[dict]] = {}
for meta in series_meta.values():
    units.setdefault(meta["unit"], []).append(meta)
for u in units:
    units[u].sort(key=lambda m: m["run"])

# Add units that only have live data (no CSV runs).
for live_unit, addr_bufs in st.session_state.live_buffers.items():
    if addr_bufs:
        units.setdefault(live_unit, [])

# Display order: V first, A second, then anything else.
_UNIT_ORDER = ["V", "A"]
units = {u: units[u] for u in _UNIT_ORDER if u in units} | \
        {u: v for u, v in units.items() if u not in _UNIT_ORDER}


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar: series visibility checkboxes
# ─────────────────────────────────────────────────────────────────────────────

visibility: dict[tuple[str, int], bool] = {}
if any(series_list for series_list in units.values()):
    st.sidebar.markdown('<p class="sidebar-section">Select measurements</p>', unsafe_allow_html=True)
    for unit, series_list in units.items():
        if series_list:
            st.sidebar.markdown(f"**{series_list[0]['name']} [{unit}]**")
            for meta in series_list:
                key = (meta["unit"], meta["run"])
                visibility[key] = st.sidebar.checkbox(meta["label"], value=True, key=str(key))


# ─────────────────────────────────────────────────────────────────────────────
# Empty state — nothing to show yet
# ─────────────────────────────────────────────────────────────────────────────

if not units:
    st.info("Connect a sensor or load a CSV to see data.")
    if PASCO_AVAILABLE:
        _ss = st.session_state.scan_state
        _conn_pending = any(
            info.get("status") not in ("connected", "disconnected")
            and "error" not in info.get("status", "")
            for info in st.session_state.sensors.values()
        )
        # Keep refreshing while a scan or connection is in progress.
        if _ss["in_progress"] or _conn_pending:
            time.sleep(0.3)
            st.rerun()
        elif st.session_state.live_discovery and _ss["last_time"] > 0:
            time_since = time.time() - _ss["last_time"]
            time.sleep(min(1.0, max(0.1, SCAN_INTERVAL_S - time_since)))
            st.rerun()
    st.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Flush live data into the recording buffer (every render pass)
# ─────────────────────────────────────────────────────────────────────────────

_flush_live_to_record()


# ─────────────────────────────────────────────────────────────────────────────
# Toolbar: Download CSV link
# ─────────────────────────────────────────────────────────────────────────────

if st.session_state.record_data:
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
    info.get("status") == "connected"
    for info in st.session_state.sensors.values()
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

# Colour palettes — file data uses cool/neutral colours, live data uses vivid colours.
FILE_COLORS = [
    "#3b82f6", "#f97316", "#10b981", "#ef4444", "#8b5cf6",
    "#06b6d4", "#f59e0b", "#6366f1", "#84cc16", "#ec4899",
]
LIVE_COLORS = [
    "#ff006e", "#fb5607", "#ffbe0b", "#8338ec", "#3a86ff",
]

unit_list = list(units.keys())
n_units   = len(unit_list)
SPACING   = 0.06  # vertical gap between subplots as a fraction of figure height

fig       = make_subplots(rows=n_units, cols=1, shared_xaxes=True, vertical_spacing=SPACING)

plot_h        = (1.0 - SPACING * (n_units - 1)) / n_units
FIG_HEIGHT    = 400 * n_units
MODEBAR_PX    = 44
MODEBAR_PAPER = MODEBAR_PX / FIG_HEIGHT   # modebar height in paper-space units

# Build one legend per subplot so that each unit has its own independent legend.
legend_layout: dict = {}
for idx, unit in enumerate(unit_list):
    row    = idx + 1
    top_y  = 1.0 - (row - 1) * (plot_h + SPACING)
    if idx == 0:
        top_y -= MODEBAR_PAPER  # leave room for the modebar above the first subplot
    legend_key  = "legend" if idx == 0 else f"legend{idx + 1}"
    series_list = units[unit]
    # Prefer the full measurement name from CSV metadata; fall back to live sensor quantity.
    _unit_name  = next(
        (info["quantity"] for info in st.session_state.sensors.values() if info.get("unit") == unit),
        unit,
    )
    full_name = series_list[0]["name"] if series_list else _unit_name
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
    legend_ref  = "legend" if row == 1 else f"legend{row}"
    series_list = units[unit]

    # File traces (CSV data)
    for i, meta in enumerate(series_list):
        key = (meta["unit"], meta["run"])
        if not visibility.get(key, True):
            continue
        time_col  = meta["time_col"]
        value_col = meta["value_col"]
        if time_col is None or raw is None:
            continue
        t = pd.to_numeric(raw[time_col],  errors="coerce")
        v = pd.to_numeric(raw[value_col], errors="coerce")
        mask = t.notna() & v.notna()
        fig.add_trace(go.Scatter(
            x=t[mask], y=v[mask],
            mode="lines",
            name=meta["label"],
            legend=legend_ref,
            line=dict(color=FILE_COLORS[i % len(FILE_COLORS)], width=1.5),
        ), row=row, col=1)

    # Live traces (BLE sensor data)
    addr_bufs = st.session_state.live_buffers.get(unit, {})
    for j, (sensor_key, buf) in enumerate(addr_bufs.items()):
        if not buf:
            continue
        sensor_info = st.session_state.sensors.get(sensor_key, {})
        label       = sensor_info.get("label", sensor_key[-6:])
        pts = list(buf)  # snapshot — streaming thread may append concurrently
        fig.add_trace(go.Scatter(
            x=[p[0] for p in pts],
            y=[p[1] for p in pts],
            mode="lines",
            name=label,
            legend=legend_ref,
            line=dict(color=LIVE_COLORS[j % len(LIVE_COLORS)], width=2, dash="dot"),
        ), row=row, col=1)

    # Axis labels
    _unit_label = next(
        (info["quantity"] for info in st.session_state.sensors.values() if info.get("unit") == unit),
        unit,
    )
    meas_name = units[unit][0]["name"] if units[unit] else _unit_label
    fig.update_yaxes(
        title_text=f"{meas_name} [{unit}]",
        title_font=dict(size=13), tickfont=dict(size=12),
        fixedrange=False, **SPIKE, row=row, col=1,
    )
    fig.update_xaxes(
        title_text="Time [s]",
        title_font=dict(size=13), tickfont=dict(size=12),
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
    **legend_layout,
)

st.plotly_chart(fig, width="stretch")


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
    info.get("status") not in ("connected", "disconnected")
    and "error" not in info.get("status", "")
    for info in st.session_state.sensors.values()
)
_scan_active = PASCO_AVAILABLE and st.session_state.scan_state["in_progress"]

if _scan_active or _sensors_transitioning or (_any_connected and st.session_state.recording):
    time.sleep(0.3)
    st.rerun()
