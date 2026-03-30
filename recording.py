"""
Recording session management: start/stop, live-to-record buffer flush, and
CSV export.

Each time the user presses Record then Stop, the accumulated data is finalised
into an entry in st.session_state.recorded_sessions with a HH:MM:SS datetime
label.  Multiple sessions can be recorded sequentially; all appear in the
"Selected measurements" sidebar section and are plotted independently.

Live sensor data that was never recorded is ephemeral — it is visible in the
plot only while the sensor is connected, and disappears on disconnect.

All functions read and write st.session_state and must only be called from
the main Streamlit thread.
"""

import datetime

import pandas as pd
import streamlit as st


def _start_recording() -> None:
    """
    Start a new recording session.

    Clears any previous in-progress record_data, resets the sensor-label
    cache, and sets a per-sensor watermark at the current tail of each live
    buffer so that only newly arriving samples are captured.  Sensors
    connected after this point start with watermark -1 (all data included).
    """
    st.session_state.recording            = True
    st.session_state.record_data          = {}
    st.session_state.record_sensor_labels = {}

    record_last_t = {}
    for unit, addr_bufs in st.session_state.live_buffers.items():
        for sensor_key, buf in addr_bufs.items():
            if buf:
                record_last_t[(unit, sensor_key)] = list(buf)[-1][0]

    st.session_state.record_last_t = record_last_t


def _stop_recording() -> None:
    """
    Stop the active recording session and finalise it.

    If any data was captured, a session entry is appended to
    recorded_sessions with the current time as its label (HH:MM:SS).
    The in-progress buffers are then cleared so the next recording cycle
    starts fresh.
    """
    st.session_state.recording = False

    if not st.session_state.record_data:
        return  # nothing captured — discard silently

    label = datetime.datetime.now().strftime("%H:%M:%S")

    # Build the sensor-label map: prefer labels captured during the flush loop
    # (sensor may have disconnected before Stop was pressed); fall back to the
    # current session state for sensors still connected.
    sensor_labels = dict(st.session_state.record_sensor_labels)
    for unit, sensor_map in st.session_state.record_data.items():
        for sensor_key in sensor_map:
            if sensor_key not in sensor_labels:
                conn = st.session_state.connections.get(sensor_key, {})
                sensor_labels[sensor_key] = conn.get("label", sensor_key)

    st.session_state.recorded_sessions.append({
        "id":            f"rec_{len(st.session_state.recorded_sessions)}",
        "label":         label,
        "data":          st.session_state.record_data,
        "sensor_labels": sensor_labels,
    })

    # Reset the in-progress buffers so the next Record cycle starts clean.
    st.session_state.record_data          = {}
    st.session_state.record_sensor_labels = {}


def _flush_live_to_record() -> None:
    """
    Copy new live-buffer samples into record_data on every render pass.

    Only copies points with a timestamp strictly greater than the last
    recorded timestamp, preventing duplicates across rerender cycles.
    Also caches sensor display labels while the sensors are still connected,
    so that labels survive a disconnect that happens before Stop is pressed.
    Must be called unconditionally each render so no data is missed.
    """
    if not st.session_state.recording:
        return

    for unit, addr_bufs in st.session_state.live_buffers.items():
        for sensor_key, buf in addr_bufs.items():
            pts    = list(buf)  # snapshot — streaming thread appends concurrently
            last_t = st.session_state.record_last_t.get((unit, sensor_key), -1.0)
            new_pts = [(t, v) for t, v in pts if t > last_t]
            if new_pts:
                (st.session_state.record_data
                    .setdefault(unit, {})
                    .setdefault(sensor_key, [])
                    .extend(new_pts))
                st.session_state.record_last_t[(unit, sensor_key)] = new_pts[-1][0]

            # Cache the display label while the sensor is alive in session state.
            conn = st.session_state.connections.get(sensor_key)
            if conn and "label" in conn:
                st.session_state.record_sensor_labels[sensor_key] = conn["label"]


def _download_csv() -> bytes:
    """
    Build and return a UTF-8-encoded CSV of all finalised recorded sessions.

    Columns: session (HH:MM:SS label), sensor (display label), time_s
             (elapsed seconds since first sensor connected), value, unit.

    Returns a header-only CSV if no sessions have been recorded yet.
    """
    rows = []
    for session in st.session_state.recorded_sessions:
        for unit, sensor_map in session["data"].items():
            for sensor_key, pts in sensor_map.items():
                label = session["sensor_labels"].get(sensor_key, sensor_key)
                for t, v in pts:
                    rows.append({
                        "session": session["label"],
                        "sensor":  label,
                        "time_s":  t,
                        "value":   v,
                        "unit":    unit,
                    })

    if not rows:
        return b"session,sensor,time_s,value,unit\n"

    return pd.DataFrame(rows).to_csv(index=False).encode()
