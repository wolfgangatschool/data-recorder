import base64
import math
import re
import threading
import time
from collections import deque
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
import streamlit.components.v1 as components

try:
    from pasco.pasco_ble_device import PASCOBLEDevice
    PASCO_AVAILABLE = True
except ImportError:
    PASCO_AVAILABLE = False

st.set_page_config(page_title="Time Series Viewer", layout="wide")

# ── Global style ──────────────────────────────────────────────────────────────

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

/* Precisely hide the live_discovery toggle by its Streamlit key class.
   visibility:hidden (not display:none) keeps it in the DOM so JS can click it. */
.st-key-live_discovery {
    visibility: hidden !important;
    position: absolute !important;
    height: 0 !important;
    overflow: hidden !important;
    padding: 0 !important;
    margin: 0 !important;
}
/* Collapse layout space for the key container and the JS iframe */
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

/* Download link — below the live discovery badge */
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

/* Live sensor discovery button in the toolbar */
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


# ── Toolbar: live discovery indicator (rendered early — visible even before st.stop) ──
def _render_live_indicator():
    _live = st.session_state.get("live_discovery", True)
    _scanning = PASCO_AVAILABLE and st.session_state.get("scan_state", {}).get("in_progress", False)
    _cls  = ("on" + (" scanning" if _scanning else "")) if _live else "off"
    _wifi = (
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
        f'<div class="toolbar-live-badge">{_wifi}&nbsp;{_label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if PASCO_AVAILABLE:
        # Hidden toggle — Streamlit manages the boolean state.
        st.toggle("Live sensor discovery", key="live_discovery",
                  label_visibility="collapsed")
        # Wire badge click → toggle state.
        # Key insight: calling .click() from inside an iframe doesn't trigger
        # React's event system in the parent. Fix: inject a <script> tag directly
        # into the parent document — it executes in the parent window's JS context,
        # where React's synthetic events work normally.
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


# ── Session state ─────────────────────────────────────────────────────────────

# sensors:       {key: {status, label, unit, meas_name, stop_event, thread, ...}}
# live_buffers:  {unit: {key: deque[(t_sec, value)]}}
# recording:     bool
# record_data:   {unit: {sensor_key: list[(t, v)]}}
# record_last_t: {(unit, key): float}  — last t value copied into record_data
# scan_state:    {in_progress, last_time, results: [parsed-device-dicts]}
for _k, _v in [("sensors", {}), ("live_buffers", {}),
               ("record_data", {}), ("record_last_t", {})]:
    st.session_state.setdefault(_k, _v)
st.session_state.setdefault("recording", False)
st.session_state.setdefault("sensor_counter", 0)
st.session_state.setdefault("scan_state", {"in_progress": False, "last_time": 0.0, "results": []})
st.session_state.setdefault("live_discovery", True)

_render_live_indicator()

# ── Live sensor helpers ────────────────────────────────────────────────────────

# BLE operations are serialised — only one CBCentralManager at a time on macOS.
_ble_lock = threading.Lock()
# Keep a strong reference to the last scan device so its CoreBluetooth delegate
# is not garbage-collected while CoreBluetooth's background thread is still
# delivering discovery callbacks (prevents the PyObjC crash on Ctrl-C).
_scan_device_ref = None

SCAN_INTERVAL_S = 15  # seconds between automatic background scans


def _parse_ble_device(ble_device) -> dict:
    """
    Extract structured metadata from a discovered BLEDevice.
    Pasco names follow the pattern "Quantity Model Serial", e.g. "Voltage PS-3211 123456".
    Missing tokens fall back gracefully.
    """
    name    = (ble_device.name or "").strip()
    address = ble_device.address or ""
    parts   = name.split()
    quantity  = parts[0] if parts else "Sensor"
    model     = parts[1] if len(parts) >= 2 else "—"
    sensor_id = parts[2] if len(parts) >= 3 else address.replace("-", "")[-6:].upper()
    return {"quantity": quantity, "model": model, "sensor_id": sensor_id,
            "address": address, "ble_device": ble_device}


def _sensor_display_label(entry: dict) -> str:
    return f"{entry['quantity']}  {entry['model']}  {entry['sensor_id']}"


# ── Background scan ────────────────────────────────────────────────────────────

def _do_scan(scan_state: dict):
    """Background thread: discover all available PASCO sensors."""
    global _scan_device_ref
    with _ble_lock:
        try:
            device = PASCOBLEDevice()
            _scan_device_ref = device  # prevent GC; keeps CoreBluetooth delegate alive
            found  = device.scan() or []
        except Exception:
            found = []
    scan_state["results"]     = [_parse_ble_device(d) for d in found]
    scan_state["in_progress"] = False
    scan_state["last_time"]   = time.time()


def _start_scan():
    """Kick off a background scan if one is not already running."""
    if st.session_state.scan_state["in_progress"]:
        return
    st.session_state.scan_state["in_progress"] = True
    threading.Thread(
        target=_do_scan, args=(st.session_state.scan_state,), daemon=True
    ).start()


# ── Stream / connect ───────────────────────────────────────────────────────────

def _stream_sensor(device, key: str, sensor_info: dict, live_buffers: dict):
    """Poll one connected PASCOBLEDevice until stop_event is set."""
    try:
        meas_list = device.get_measurement_list()
        if not meas_list:
            sensor_info["status"] = "error: no measurements found"
            device.disconnect()
            return
        meas_name = meas_list[0]

        unit = None
        for ch_measurements in device._device_measurements.values():
            for m_attrs in ch_measurements.values():
                if m_attrs.get("NameTag") == meas_name:
                    unit = m_attrs.get("Units", "")
                    break
            if unit is not None:
                break
        if not unit:
            unit = "V" if "voltage" in meas_name.lower() else \
                   "A" if "current" in meas_name.lower() else meas_name
    except Exception as exc:
        sensor_info["status"] = f"error: {exc}"
        try:
            device.disconnect()
        except Exception:
            pass
        return

    live_buffers.setdefault(unit, {})
    live_buffers[unit][key] = deque(maxlen=20_000)
    buf = live_buffers[unit][key]
    sensor_info.update({"status": "connected", "unit": unit, "meas_name": meas_name})

    stop_event = sensor_info["stop_event"]
    t0 = time.time()
    while not stop_event.is_set():
        try:
            val = device.read_data(meas_name)
            if val is not None:
                buf.append((time.time() - t0, float(val)))
        except Exception:
            break
        time.sleep(0.05)

    try:
        device.disconnect()
    except Exception:
        pass
    sensor_info["status"] = "disconnected"


def _connect_thread_fn(entry: dict, key: str, sensor_info: dict, live_buffers: dict):
    """
    Connect using the BLEDevice already stored in the scan results.
    pasco.connect() only uses ble_device.address (a string UUID), so the
    CBPeripheral inside the BLEDevice object does not need to be fresh.
    Avoids spawning extra CBCentralManager instances (one from BleakScanner
    inside scan() + one from BleakClient inside connect()) that interfere
    on macOS CoreBluetooth.
    """
    sensor_info["status"] = "connecting…"
    with _ble_lock:
        device = PASCOBLEDevice()
        try:
            device.connect(entry["ble_device"])
        except Exception as exc:
            sensor_info["status"] = f"error: {exc}"
            return
    _stream_sensor(device, key, sensor_info, live_buffers)


def _start_connect(entry: dict):
    """Create a sensor entry and start a connection thread for a discovered device."""
    st.session_state.sensor_counter += 1
    key = f"sensor_{entry['address']}_{st.session_state.sensor_counter}"
    sensor_info = {
        "address":    entry["address"],
        "quantity":   entry["quantity"],
        "model":      entry["model"],
        "sensor_id":  entry["sensor_id"],
        "status":     "starting…",
        "label":      entry["quantity"],
        "unit":       None,
        "meas_name":  None,
        "stop_event": threading.Event(),
        "thread":     None,
    }
    st.session_state.sensors[key] = sensor_info
    t = threading.Thread(
        target=_connect_thread_fn,
        args=(entry, key, sensor_info, st.session_state.live_buffers),
        daemon=True,
    )
    sensor_info["thread"] = t
    t.start()


def _disconnect_sensor(key: str):
    info = st.session_state.sensors.get(key)
    if info:
        info["stop_event"].set()
    unit = (info or {}).get("unit")
    if unit and key in st.session_state.live_buffers.get(unit, {}):
        del st.session_state.live_buffers[unit][key]
    if key in st.session_state.sensors:
        del st.session_state.sensors[key]


# ── Recording helpers ──────────────────────────────────────────────────────────

def _start_recording():
    st.session_state.recording = True
    st.session_state.record_data = {}
    # Initialise record_last_t to the current buffer tail for every already-connected
    # sensor so that only data arriving *after* Record is pressed gets captured.
    # Sensors added later start from t=-1 (i.e. include all their data, which begins
    # at or after the moment they were connected post-Record).
    record_last_t = {}
    for unit, addr_bufs in st.session_state.live_buffers.items():
        for addr, buf in addr_bufs.items():
            if buf:
                record_last_t[(unit, addr)] = list(buf)[-1][0]
    st.session_state.record_last_t = record_last_t


def _stop_recording():
    st.session_state.recording = False


def _flush_live_to_record():
    """Copy new live points into record_data for all units."""
    if not st.session_state.recording:
        return
    for unit, addr_bufs in st.session_state.live_buffers.items():
        for addr, buf in addr_bufs.items():
            pts = list(buf)
            last_t = st.session_state.record_last_t.get((unit, addr), -1.0)
            new_pts = [(t, v) for t, v in pts if t > last_t]
            if new_pts:
                st.session_state.record_data.setdefault(unit, {}).setdefault(addr, []).extend(new_pts)
                st.session_state.record_last_t[(unit, addr)] = new_pts[-1][0]


def _download_csv() -> bytes:
    """Build a CSV with all recorded units/sensors."""
    rows = []
    for unit, addr_map in st.session_state.record_data.items():
        for addr, pts in addr_map.items():
            sensor_info = st.session_state.sensors.get(addr, {})
            label = sensor_info.get("label", addr)
            for t, v in pts:
                rows.append({"sensor": label, "time_s": t, "value": v, "unit": unit})
    if not rows:
        return b"sensor,time_s,value,unit\n"
    return pd.DataFrame(rows).to_csv(index=False).encode()


# ── Sidebar: live sensors ─────────────────────────────────────────────────────

if PASCO_AVAILABLE:
    scan_state = st.session_state.scan_state
    _live = st.session_state.live_discovery

    # Auto-scan on first load and every SCAN_INTERVAL_S seconds (only when live discovery on)
    if _live and not scan_state["in_progress"] and (
        scan_state["last_time"] == 0.0
        or time.time() - scan_state["last_time"] >= SCAN_INTERVAL_S
    ):
        _start_scan()

    st.sidebar.markdown('<p class="sidebar-section">Live sensors</p>', unsafe_allow_html=True)

    col_cap, col_ref = st.sidebar.columns([3, 1])
    with col_cap:
        if scan_state["last_time"] > 0:
            ts = time.strftime("%H:%M:%S", time.localtime(scan_state["last_time"]))
            st.caption(f"Last update at {ts}")
        elif scan_state["in_progress"]:
            st.caption("Scanning…")
        else:
            st.caption("No live sensors available")
    with col_ref:
        if st.button("↺", key="btn_refresh_scan", help="Scan for sensors now",
                     width="stretch"):
            _start_scan()
            st.rerun()

    # Available sensors dropdown — exclude already-connected ones
    connected_addrs = {
        info["address"]
        for info in st.session_state.sensors.values()
        if "address" in info
        and "error" not in info.get("status", "")
        and info.get("status") != "disconnected"
    }
    available = [s for s in scan_state["results"] if s["address"] not in connected_addrs]

    col_sel, col_add = st.sidebar.columns([3, 1])
    if available:
        # Clamp stored index when the list shrank or the value is stale/wrong type.
        _cur = st.session_state.get("sel_sensor_idx", 0)
        if not isinstance(_cur, int) or _cur >= len(available):
            st.session_state["sel_sensor_idx"] = 0

        new_idx = col_sel.selectbox(
            "Sensor",
            options=range(len(available)),
            format_func=lambda i: _sensor_display_label(available[i]),
            label_visibility="collapsed",
            key="sel_sensor_idx",
        )

        if col_add.button("＋", key="btn_connect", width="stretch",
                          help="Connect selected sensor"):
            _start_connect(available[new_idx])
            st.rerun()
    else:
        placeholder = "Scanning…" if st.session_state.live_discovery else "No sensors available"
        col_sel.selectbox(
            "Sensor", [placeholder],
            label_visibility="collapsed",
            disabled=True,
            key="sel_sensor_placeholder",
        )
        col_add.button("＋", key="btn_connect", width="stretch", disabled=True)

    # Connected sensors list — in order of connection, with - disconnect button
    if st.session_state.sensors:
        st.sidebar.markdown("---")
        for key, info in list(st.session_state.sensors.items()):
            status = info.get("status", "")
            icon   = "🟢" if status == "connected" else \
                     "🔴" if "error" in status else "🟡"
            col_lbl, col_m = st.sidebar.columns([3, 1])
            lines = [
                f"{icon} **{info.get('quantity', '?')}**",
                info.get("model", ""),
                f"`{info.get('sensor_id', '')}`",
            ]
            if status not in ("connected",):
                lines.append(f"*{status}*")
            col_lbl.markdown("  \n".join(lines))
            if col_m.button("－", key=f"disc_{key}", help="Disconnect",
                            width="stretch"):
                _disconnect_sensor(key)
                st.rerun()
else:
    st.sidebar.caption("Install `pasco` to enable live sensors.")


# ── File loading ──────────────────────────────────────────────────────────────

st.sidebar.markdown('<p class="sidebar-section">Load recorded data</p>', unsafe_allow_html=True)
uploaded = st.sidebar.file_uploader("Load CSV", type="csv", label_visibility="collapsed")

@st.cache_data
def load_csv(source) -> pd.DataFrame:
    return pd.read_csv(source, low_memory=False)

raw = load_csv(uploaded) if uploaded else None


# ── Parse column structure ────────────────────────────────────────────────────

MEAS_RE = re.compile(r"^(.+?)\s*\((.+?)\)\s*Run\s*(\d+)$", re.IGNORECASE)
TIME_RE  = re.compile(r"^Time\s*\((.+?)\)\s*Run\s*(\d+)$",  re.IGNORECASE)

series_meta: dict[tuple[str, int], dict] = {}

if raw is not None:
    for col in raw.columns:
        if TIME_RE.match(col.strip()):
            continue
        m = MEAS_RE.match(col.strip())
        if not m:
            continue
        name, unit, run_str = m.group(1).strip(), m.group(2).strip(), int(m.group(3))
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

# Build units from actual data only
units: dict[str, list[dict]] = {}

for meta in series_meta.values():
    units.setdefault(meta["unit"], []).append(meta)
for u in units:
    units[u].sort(key=lambda m: m["run"])

for live_unit, addr_bufs in st.session_state.live_buffers.items():
    if addr_bufs:
        units.setdefault(live_unit, [])

# Order: V first, then A, then anything else — only units with data
_UNIT_ORDER = ["V", "A"]
units = {u: units[u] for u in _UNIT_ORDER if u in units} | \
        {u: v for u, v in units.items() if u not in _UNIT_ORDER}


# ── Sidebar: series visibility ────────────────────────────────────────────────

visibility: dict[tuple[str, int], bool] = {}
if any(series_list for series_list in units.values()):
    st.sidebar.markdown('<p class="sidebar-section">Select measurements</p>', unsafe_allow_html=True)
    for unit, series_list in units.items():
        if series_list:
            st.sidebar.markdown(f"**{series_list[0]['name']} [{unit}]**")
            for meta in series_list:
                key = (meta["unit"], meta["run"])
                visibility[key] = st.sidebar.checkbox(meta["label"], value=True, key=str(key))


if not units:
    st.info("Connect a sensor or load a CSV to see data.")
    if PASCO_AVAILABLE:
        _ss = st.session_state.scan_state
        _conn_pending = any(
            info.get("status") not in ("connected", "disconnected")
            and "error" not in info.get("status", "")
            for info in st.session_state.sensors.values()
        )
        if _ss["in_progress"] or _conn_pending:
            time.sleep(0.3)
            st.rerun()
        elif st.session_state.live_discovery and _ss["last_time"] > 0:
            time_since = time.time() - _ss["last_time"]
            time.sleep(min(1.0, max(0.1, SCAN_INTERVAL_S - time_since)))
            st.rerun()
    st.stop()

# ── Plot ──────────────────────────────────────────────────────────────────────

FILE_COLORS = [
    "#3b82f6", "#f97316", "#10b981", "#ef4444", "#8b5cf6",
    "#06b6d4", "#f59e0b", "#6366f1", "#84cc16", "#ec4899",
]
LIVE_COLORS = [
    "#ff006e", "#fb5607", "#ffbe0b", "#8338ec", "#3a86ff",
]

unit_list = list(units.keys())
n_units   = len(unit_list)
SPACING   = 0.06

fig = make_subplots(rows=n_units, cols=1, shared_xaxes=True, vertical_spacing=SPACING)

plot_h        = (1.0 - SPACING * (n_units - 1)) / n_units
MODEBAR_PX    = 44
FIG_HEIGHT    = 400 * n_units
MODEBAR_PAPER = MODEBAR_PX / FIG_HEIGHT

# Build per-unit legends
legend_layout: dict = {}
for idx, unit in enumerate(unit_list):
    row    = idx + 1
    top_y  = 1.0 - (row - 1) * (plot_h + SPACING)
    if idx == 0:
        top_y -= MODEBAR_PAPER
    legend_key = "legend" if idx == 0 else f"legend{idx + 1}"
    series_list = units[unit]
    _unit_name  = next((info["quantity"] for info in st.session_state.sensors.values()
                        if info.get("unit") == unit), unit)
    full_name   = series_list[0]["name"] if series_list else _unit_name
    legend_layout[legend_key] = dict(
        x=1.01, xanchor="left",
        y=top_y, yanchor="top",
        orientation="v",
        title_text=f"{full_name} [{unit}]",
        title_font=dict(size=13),
        font=dict(size=13),
    )

SPIKE = dict(
    showspikes=True, spikemode="across", spikesnap="cursor",
    spikethickness=0.75, spikecolor="rgba(160,160,160,0.45)", spikedash="solid",
)

for row, unit in enumerate(unit_list, start=1):
    legend_ref  = "legend" if row == 1 else f"legend{row}"
    series_list = units[unit]

    # ── File traces ───────────────────────────────────────────────────────────
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

    # ── Live traces ───────────────────────────────────────────────────────────
    addr_bufs = st.session_state.live_buffers.get(unit, {})
    for j, (addr, buf) in enumerate(addr_bufs.items()):
        if not buf:
            continue
        sensor_info = st.session_state.sensors.get(addr, {})
        label       = sensor_info.get("label", addr[-6:])
        pts  = list(buf)                      # snapshot — thread appends concurrently
        ts   = [p[0] for p in pts]
        vs   = [p[1] for p in pts]
        fig.add_trace(go.Scatter(
            x=ts, y=vs,
            mode="lines",
            name=f"{label}",
            legend=legend_ref,
            line=dict(color=LIVE_COLORS[j % len(LIVE_COLORS)], width=2, dash="dot"),
        ), row=row, col=1)

    _unit_label = next((info["quantity"] for info in st.session_state.sensors.values()
                        if info.get("unit") == unit), unit)
    meas_name = units[unit][0]["name"] if units[unit] else _unit_label
    fig.update_yaxes(title_text=f"{meas_name} [{unit}]",
                     title_font=dict(size=13), tickfont=dict(size=12),
                     fixedrange=False, **SPIKE, row=row, col=1)
    fig.update_xaxes(title_text="Time [s]",
                     title_font=dict(size=13), tickfont=dict(size=12),
                     **SPIKE, row=row, col=1)

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

# Flush live data into record buffers on every render pass
_flush_live_to_record()

# ── Toolbar download link (fixed position, replaces deploy button) ─────────────
_has_data = bool(st.session_state.record_data)
if _has_data:
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

# ── Record / Stop bar ─────────────────────────────────────────────────────────
_any_connected = any(
    info.get("status") == "connected"
    for info in st.session_state.sensors.values()
)
rec_left, rec_mid, rec_right = st.columns([2, 1, 2])
with rec_mid:
    if st.session_state.recording:
        rec_pts = sum(
            len(pts)
            for addr_map in st.session_state.record_data.values()
            for pts in addr_map.values()
        )
        st.button(f"■  Stop  ({rec_pts} pts)", key="btn_stop_rec",
                  width="stretch", type="primary",
                  on_click=_stop_recording)
    elif _any_connected:
        st.button("●  Record", key="btn_start_rec",
                  width="stretch",
                  on_click=_start_recording)

st.plotly_chart(fig, width="stretch")


# ── Raw data preview ──────────────────────────────────────────────────────────

if raw is not None:
    with st.expander("Raw data"):
        st.dataframe(raw, width="stretch")


# ── Auto-refresh ─────────────────────────────────────────────────────────────

_sensors_active = any(
    info.get("status") not in ("connected", "disconnected") and
    "error" not in info.get("status", "")
    for info in st.session_state.sensors.values()
)
_scan_active = PASCO_AVAILABLE and st.session_state.scan_state["in_progress"]

if _scan_active or _sensors_active or (_any_connected and st.session_state.recording):
    time.sleep(0.3)
    st.rerun()
elif PASCO_AVAILABLE and st.session_state.live_discovery and st.session_state.scan_state["last_time"] > 0:
    time_since = time.time() - st.session_state.scan_state["last_time"]
    time.sleep(min(1.0, max(0.1, SCAN_INTERVAL_S - time_since)))
    st.rerun()
