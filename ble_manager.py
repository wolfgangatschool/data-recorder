"""
BLE sensor management: discovery, connection, streaming, and disconnection.

All BLE operations are serialised via _ble_lock because macOS CoreBluetooth
supports only one active CBCentralManager at a time.  Scan and connect must
also run on the *same* PASCOBLEDevice instance so that the CBCentralManager
and its asyncio event loop are shared.

Thread/main-thread communication
---------------------------------
Three session-state structures are maintained:

  managed_addrs : set[str]
      Addresses currently in the sensor list.  Modified ONLY by on_click
      callbacks on the main thread — never by background threads.  This is
      the single source of truth for "is this sensor managed?".

  connections : dict[str, dict]
      Per-connection details (status, label, unit, stop_event, …).
      Each background thread receives the conn dict as a plain argument and
      writes to it directly.  When _disconnect_sensor() pops the dict out of
      connections, the thread still holds a reference to the now-orphaned dict
      and may keep writing to it safely — those writes are invisible to the
      main thread because the dict is no longer reachable from session_state.

  sensor_meta : dict[str, dict]
      Metadata cache: {addr: {quantity, model, sensor_id, address, ble_device}}.
      Updated by the main thread from scan results and from connect metadata.
      Used as a fallback in the dropdown for recently disconnected sensors
      that have not yet been rediscovered by a scan.

Background threads must never access st.session_state.
"""

import threading
import time
from collections import deque

import streamlit as st
from pasco.pasco_ble_device import PASCOBLEDevice


# Serialise all BLE I/O — only one CBCentralManager may be active at a time.
_ble_lock = threading.Lock()

# Strong reference so CoreBluetooth's delegate is not GC'd between callbacks.
_scan_device_ref = None

# Seconds between automatic background scans.
SCAN_INTERVAL_S = 15

# Sensor considered lost after this many seconds of no data.
STALE_TIMEOUT_S = 5

# Shared time origin — set when the first sensor starts streaming.
# Reset to None when all sensors are removed.
_global_t0: float | None = None
_t0_lock = threading.Lock()


# ── Device metadata ────────────────────────────────────────────────────────────

def _parse_ble_device(ble_device) -> dict:
    """Extract structured metadata from a raw BLEDevice returned by pasco.scan()."""
    name    = (ble_device.name or "").split(">")[0].strip()
    address = ble_device.address or ""
    parts   = name.split()
    quantity = parts[0] if parts else "Sensor"
    if len(parts) >= 3:
        model, raw_id = parts[1], parts[2]
    elif len(parts) == 2:
        model, raw_id = "—", parts[1]
    else:
        model  = "—"
        raw_id = address.replace("-", "")[-6:].upper()
    sensor_id = (f"{raw_id[:3]}-{raw_id[3:]}"
                 if len(raw_id) > 3 and "-" not in raw_id else raw_id)
    return {
        "quantity":   quantity,
        "model":      model,
        "sensor_id":  sensor_id,
        "address":    address,
        "ble_device": ble_device,
    }


def _sensor_display_label(entry: dict) -> str:
    """Format a sensor entry as a human-readable dropdown label."""
    return f"{entry['quantity']} ᛒ {entry['sensor_id']}"


# ── Background scan ────────────────────────────────────────────────────────────

def _do_scan(scan_state: dict) -> None:
    """
    Background thread: discover all available PASCO BLE sensors.

    Writes only to scan_state (a plain dict passed as argument) — never to
    st.session_state.  The main thread merges scan_state["results"] into
    sensor_meta on each render.
    """
    global _scan_device_ref
    with _ble_lock:
        try:
            device = PASCOBLEDevice()
            _scan_device_ref = device
            found = device.scan() or []
        except Exception:
            found = []

    scan_state["results"]     = [_parse_ble_device(d) for d in found]
    scan_state["in_progress"] = False
    scan_state["last_time"]   = time.time()


def _start_scan() -> None:
    """Kick off a background scan if one is not already running."""
    if st.session_state.scan_state["in_progress"]:
        return
    st.session_state.scan_state["in_progress"] = True
    threading.Thread(
        target=_do_scan,
        args=(st.session_state.scan_state,),
        daemon=True,
    ).start()


# ── Streaming ─────────────────────────────────────────────────────────────────

def _stream_sensor(device, address: str, conn: dict, live_buffers: dict) -> None:
    """
    Poll a connected PASCOBLEDevice until conn["stop_event"] is set.

    conn is a plain dict owned by this connection session.  All writes stay
    within conn — st.session_state is never accessed from this thread.
    """
    try:
        meas_list = device.get_measurement_list()
        if not meas_list:
            conn["status"] = "error: no measurements found"
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
            unit = ("V" if "voltage" in meas_name.lower() else
                    "A" if "current" in meas_name.lower() else meas_name)

    except Exception as exc:
        conn["status"] = f"error: {exc}"
        try:
            device.disconnect()
        except Exception:
            pass
        return

    live_buffers.setdefault(unit, {})
    live_buffers[unit][address] = deque(maxlen=20_000)
    buf = live_buffers[unit][address]
    conn.update({"status": "connected", "unit": unit, "meas_name": meas_name})

    global _global_t0
    with _t0_lock:
        if _global_t0 is None:
            _global_t0 = time.time()
        t0 = _global_t0

    stop_event     = conn["stop_event"]
    last_data_time = time.time()
    while not stop_event.is_set():
        try:
            val = device.read_data(meas_name)
            if val is not None:
                buf.append((time.time() - t0, float(val)))
                last_data_time = time.time()
            elif time.time() - last_data_time > STALE_TIMEOUT_S:
                break
        except Exception:
            break
        time.sleep(0.05)

    try:
        device.disconnect()
    except Exception:
        pass
    # Signal the main thread's safety sweep (runs on every rerun).
    # This write goes to the conn dict; if _disconnect_sensor() already popped
    # this dict from connections, the write is to an orphaned dict and is harmless.
    conn["status"] = "disconnected"


def _connect_thread_fn(address: str, conn: dict, live_buffers: dict) -> None:
    """
    Background thread: scan and connect using the same PASCOBLEDevice instance.
    """
    conn["status"] = "connecting…"
    with _ble_lock:
        if conn["stop_event"].is_set():
            conn["status"] = "disconnected"
            return
        device = PASCOBLEDevice()
        try:
            found = device.scan() or []
            match = next((d for d in found if d.address == address), None)
            if match is None:
                conn["status"] = "error: sensor not found (out of range?)"
                return
            device.connect(match)
        except Exception as exc:
            conn["status"] = f"error: {exc}"
            return

    if conn["stop_event"].is_set():
        try:
            device.disconnect()
        except Exception:
            pass
        conn["status"] = "disconnected"
        return

    _stream_sensor(device, address, conn, live_buffers)


# ── Connect / disconnect (called from main thread via on_click) ───────────────

def _start_connect(entry: dict) -> None:
    """
    Add a sensor to the managed set and start a connection thread.

    managed_addrs is the single source of truth.  Adding the address here
    (before the next render) guarantees the sensor cannot appear in the
    dropdown on the very next render — the dropdown filters by managed_addrs.

    Guard: if the address is already in managed_addrs (e.g. rapid double-click),
    this call is a no-op.
    """
    addr = entry["address"]

    if addr in st.session_state.managed_addrs:
        return  # guard: already managed — ignore duplicate

    st.session_state.managed_addrs.add(addr)

    # Cache metadata for display (sensor list + fallback dropdown).
    st.session_state.sensor_meta[addr] = {
        "quantity":  entry["quantity"],
        "model":     entry["model"],
        "sensor_id": entry["sensor_id"],
        "address":   addr,
        "ble_device": entry.get("ble_device"),
    }

    conn = {
        "status":     "starting…",
        "label":      entry["quantity"],
        "unit":       None,
        "meas_name":  None,
        "stop_event": threading.Event(),
        "thread":     None,
    }
    st.session_state.connections[addr] = conn

    t = threading.Thread(
        target=_connect_thread_fn,
        args=(addr, conn, st.session_state.live_buffers),
        daemon=True,
    )
    conn["thread"] = t
    t.start()


def _disconnect_sensor(address: str) -> None:
    """
    Two-phase removal of a sensor from the managed set.

    Phase 1 — user clicks "－" (status is connected/connecting/starting):
      Sets conn["status"] = "disconnecting…" and signals the thread to stop.
      The address is intentionally kept in managed_addrs so the sensor remains
      visible in the list (showing "disconnecting…") and stays out of the
      dropdown until the thread actually finishes.

    Phase 2 — safety sweep (status == "disconnected") or error state:
      Pops the conn dict, discards the address from managed_addrs, cleans up
      live buffers, and resets the shared time origin if no sensors remain.

    Calling this function when conn is already gone (address not in connections)
    is safe — it just ensures managed_addrs is clean.
    """
    conn = st.session_state.connections.get(address)

    if conn is None:
        # Already fully removed; ensure managed_addrs is consistent.
        st.session_state.managed_addrs.discard(address)
        return

    status = conn.get("status", "")

    # ── Phase 2: complete removal ─────────────────────────────────────────────
    if status == "disconnected" or status.startswith("error"):
        st.session_state.connections.pop(address, None)
        st.session_state.managed_addrs.discard(address)
        for unit_bufs in list(st.session_state.live_buffers.values()):
            unit_bufs.pop(address, None)
        if address in st.session_state.sensor_meta:
            st.session_state.sensor_meta[address]["_last_seen_at"] = time.time()
        global _global_t0
        if not st.session_state.managed_addrs:
            with _t0_lock:
                _global_t0 = None
        return

    # ── Phase 1: initiate graceful disconnect ─────────────────────────────────
    # Signal the thread; managed_addrs is unchanged until Phase 2.
    conn["status"] = "disconnecting…"
    if conn.get("stop_event"):
        conn["stop_event"].set()
    # Drop the live buffer immediately — no new samples will be recorded.
    for unit_bufs in list(st.session_state.live_buffers.values()):
        unit_bufs.pop(address, None)
    if address in st.session_state.sensor_meta:
        st.session_state.sensor_meta[address]["_last_seen_at"] = time.time()
