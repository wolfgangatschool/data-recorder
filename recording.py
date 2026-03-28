"""
Recording session management: start/stop, live-to-record buffer flush, and
CSV export.

A recording session accumulates all live sensor data that arrives after the
user presses Record.  Data already in the live ring-buffer when Record is
pressed is excluded by snapshotting the current buffer tail as a watermark.
Sensors added *after* recording starts are included from their first sample.

All functions read and write st.session_state and must only be called from
the main Streamlit thread.
"""

import pandas as pd
import streamlit as st


def _start_recording() -> None:
    """
    Start a new recording session.

    Clears any previous record_data and sets a per-sensor watermark at the
    current tail of each live buffer so that only newly arriving samples are
    captured.  Sensors connected after this point start with watermark -1,
    meaning all their data is included.
    """
    st.session_state.recording  = True
    st.session_state.record_data = {}

    # Snapshot the current tail timestamp for every already-connected sensor.
    record_last_t = {}
    for unit, addr_bufs in st.session_state.live_buffers.items():
        for sensor_key, buf in addr_bufs.items():
            if buf:
                record_last_t[(unit, sensor_key)] = list(buf)[-1][0]

    st.session_state.record_last_t = record_last_t


def _stop_recording() -> None:
    """Stop the active recording session (data is kept for download)."""
    st.session_state.recording = False


def _flush_live_to_record() -> None:
    """
    Copy new live-buffer samples into record_data on every render pass.

    Only copies points with a timestamp strictly greater than the last
    recorded timestamp, preventing duplicates across rerender cycles.
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


def _download_csv() -> bytes:
    """
    Build and return a UTF-8-encoded CSV of all recorded data.

    Columns: sensor (display label), time_s (elapsed seconds since connect),
             value, unit.

    Returns a minimal header-only CSV if nothing has been recorded yet.
    """
    rows = []
    for unit, sensor_map in st.session_state.record_data.items():
        for sensor_key, pts in sensor_map.items():
            # sensor_key matches the key in st.session_state.sensors
            label = st.session_state.sensors.get(sensor_key, {}).get("label", sensor_key)
            for t, v in pts:
                rows.append({"sensor": label, "time_s": t, "value": v, "unit": unit})

    if not rows:
        return b"sensor,time_s,value,unit\n"

    return pd.DataFrame(rows).to_csv(index=False).encode()
