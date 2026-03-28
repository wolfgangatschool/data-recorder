"""
BLE sensor management: discovery, connection, streaming, and disconnection.

All BLE operations are serialised via _ble_lock because macOS CoreBluetooth
supports only one active CBCentralManager at a time.  Scan and connect must
also run on the *same* PASCOBLEDevice instance so that the CBCentralManager
and its asyncio event loop are shared — CentralManagerDelegate captures the
running loop at scan time, and connecting on a different loop causes failures.

Background threads communicate with the main (Streamlit) thread exclusively
through plain dicts and threading.Event objects that are passed as arguments.
st.session_state must never be accessed from a background thread.
"""

import threading
import time
from collections import deque

import streamlit as st
from pasco.pasco_ble_device import PASCOBLEDevice


# Serialise all BLE I/O — only one CBCentralManager may be active at a time.
_ble_lock = threading.Lock()

# Strong reference to the last scan device so its CoreBluetooth delegate is
# not garbage-collected while CoreBluetooth's background thread is still
# delivering discovery callbacks (prevents a PyObjC crash on Ctrl-C).
_scan_device_ref = None

# Seconds between automatic background scans.
SCAN_INTERVAL_S = 15


# ── Device metadata ────────────────────────────────────────────────────────────

def _parse_ble_device(ble_device) -> dict:
    """
    Extract structured metadata from a raw BLEDevice returned by pasco.scan().

    Pasco advertises sensor names as "Quantity Model Serial", e.g.:
        "Voltage PS-3211 123456"
    Missing tokens fall back gracefully.

    Returns a dict with keys: quantity, model, sensor_id, address, ble_device.
    """
    name    = (ble_device.name or "").strip()
    address = ble_device.address or ""
    parts   = name.split()
    quantity  = parts[0] if parts else "Sensor"
    model     = parts[1] if len(parts) >= 2 else "—"
    sensor_id = parts[2] if len(parts) >= 3 else address.replace("-", "")[-6:].upper()
    return {
        "quantity":   quantity,
        "model":      model,
        "sensor_id":  sensor_id,
        "address":    address,
        "ble_device": ble_device,
    }


def _sensor_display_label(entry: dict) -> str:
    """Format a parsed sensor entry as a human-readable dropdown label."""
    return f"{entry['quantity']}  {entry['model']}  {entry['sensor_id']}"


# ── Background scan ────────────────────────────────────────────────────────────

def _do_scan(scan_state: dict, known_sensors: dict) -> None:
    """
    Background thread: discover all available PASCO BLE sensors.

    Takes plain dicts as arguments instead of reading st.session_state
    directly, because session_state must not be accessed from background
    threads (it is not thread-safe).

    Results are merged into known_sensors so that sensors that were previously
    seen (and removed from the active list) reappear in the dropdown as soon
    as they are rediscovered.
    """
    global _scan_device_ref
    with _ble_lock:
        try:
            device = PASCOBLEDevice()
            _scan_device_ref = device  # prevent GC during CoreBluetooth callbacks
            found = device.scan() or []
        except Exception:
            found = []

    scan_state["results"]     = [_parse_ble_device(d) for d in found]
    scan_state["in_progress"] = False
    scan_state["last_time"]   = time.time()

    for entry in scan_state["results"]:
        known_sensors[entry["address"]] = entry


def _start_scan() -> None:
    """Kick off a background scan if one is not already running."""
    if st.session_state.scan_state["in_progress"]:
        return
    st.session_state.scan_state["in_progress"] = True
    threading.Thread(
        target=_do_scan,
        args=(st.session_state.scan_state, st.session_state.known_sensors),
        daemon=True,
    ).start()


# ── Streaming ─────────────────────────────────────────────────────────────────

def _stream_sensor(device, key: str, sensor_info: dict, live_buffers: dict) -> None:
    """
    Poll a connected PASCOBLEDevice until its stop_event is set.

    Reads the first available measurement at ~20 Hz, appending (elapsed_s, value)
    tuples to a rolling deque in live_buffers[unit][key].  The deque is capped
    at 20 000 points to bound memory usage over long sessions.

    sensor_info is updated in place so the main thread can observe status changes.
    """
    # --- Resolve measurement name and physical unit ---
    try:
        meas_list = device.get_measurement_list()
        if not meas_list:
            sensor_info["status"] = "error: no measurements found"
            device.disconnect()
            return
        meas_name = meas_list[0]

        # Walk the device's channel/measurement tree to find the unit string.
        unit = None
        for ch_measurements in device._device_measurements.values():
            for m_attrs in ch_measurements.values():
                if m_attrs.get("NameTag") == meas_name:
                    unit = m_attrs.get("Units", "")
                    break
            if unit is not None:
                break

        # Fallback: derive a unit from the measurement name.
        if not unit:
            unit = ("V" if "voltage" in meas_name.lower() else
                    "A" if "current" in meas_name.lower() else meas_name)

    except Exception as exc:
        sensor_info["status"] = f"error: {exc}"
        try:
            device.disconnect()
        except Exception:
            pass
        return

    # --- Set up the live data buffer and mark sensor as connected ---
    live_buffers.setdefault(unit, {})
    live_buffers[unit][key] = deque(maxlen=20_000)
    buf = live_buffers[unit][key]
    sensor_info.update({"status": "connected", "unit": unit, "meas_name": meas_name})

    # --- Polling loop at ~20 Hz ---
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


def _connect_thread_fn(entry: dict, key: str, sensor_info: dict, live_buffers: dict) -> None:
    """
    Background thread: scan and connect using the same PASCOBLEDevice instance.

    Both scan and connect must share one PASCOBLEDevice (and therefore one
    CBCentralManager and one asyncio event loop).  Using separate instances
    causes CentralManagerDelegate to look up the wrong event loop and fail
    with "Timeout should be used inside a task".

    After a successful connection, hands off to _stream_sensor which runs until
    the sensor is removed.
    """
    sensor_info["status"] = "connecting…"
    with _ble_lock:
        device = PASCOBLEDevice()
        try:
            found = device.scan() or []
            match = next((d for d in found if d.address == entry["address"]), None)
            if match is None:
                sensor_info["status"] = "error: sensor not found (out of range?)"
                return
            device.connect(match)
        except Exception as exc:
            sensor_info["status"] = f"error: {exc}"
            return

    _stream_sensor(device, key, sensor_info, live_buffers)


# ── Connect / disconnect (called from main thread) ────────────────────────────

def _start_connect(entry: dict) -> None:
    """
    Register a sensor in session state and start a background connection thread.

    entry is a dict from _parse_ble_device() describing the target sensor.
    The sensor key is stable for the lifetime of the connection and is used as
    the index into sensors, live_buffers, and record_data.
    """
    st.session_state.known_sensors[entry["address"]] = entry
    st.session_state.sensor_counter += 1
    key = f"sensor_{entry['address']}_{st.session_state.sensor_counter}"

    sensor_info = {
        "address":    entry["address"],
        "quantity":   entry["quantity"],
        "model":      entry["model"],
        "sensor_id":  entry["sensor_id"],
        "status":     "starting…",
        "label":      entry["quantity"],   # editable display name for plots/CSV
        "unit":       None,                # filled in by _stream_sensor
        "meas_name":  None,                # filled in by _stream_sensor
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


def _disconnect_sensor(key: str) -> None:
    """
    Signal a sensor's streaming thread to stop and remove it from session state.

    The sensor stays in known_sensors so it reappears in the dropdown without
    waiting for the next scan cycle.
    """
    info = st.session_state.sensors.get(key)
    if info:
        info["stop_event"].set()

    # Remove the live buffer so its trace disappears from the plot immediately.
    unit = (info or {}).get("unit")
    if unit and key in st.session_state.live_buffers.get(unit, {}):
        del st.session_state.live_buffers[unit][key]

    if key in st.session_state.sensors:
        del st.session_state.sensors[key]
