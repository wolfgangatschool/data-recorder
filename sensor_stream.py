"""
Streaming strategies for BLE sensor data acquisition.

Two concrete strategies implement the ``StreamStrategy`` interface:

``PollStream``
    BLE one-shot polling via ``device.read_data()``.  Works reliably up to
    ~20 Hz (``_POLL_CEILING_HZ``).  No changes to sensor firmware state; safe
    at any time.

``PushStream``
    Continuous push notifications at configurable rates (≥ 200 Hz typical).
    Sends an init sequence to the sensor, intercepts notifications on the svc1
    DATA characteristic, decodes samples, and ACKs every 8 packets.

Why a strategy class?
---------------------
Polling and push differ significantly in their protocol, timing, and cleanup
requirements.  Encapsulating each as a ``StreamStrategy`` keeps all variance
in this file.  ``BLEManager._stream_sensor`` just selects a strategy based on
the configured rate and calls ``strategy.stream()``.  Nothing else in the app
needs to know which transport is active.

GATT layout (PS-3211 / PS-3212)
--------------------------------
``4a5c000{svc}-000{char}-0000-0000-5c1e741f1c00``

  svc=0, char=2  →  svc0 SEND_CMD   (polling commands, keepalives)
  svc=0, char=3  →  svc0 RECV_CMD   (poll responses)
  svc=1, char=2  →  svc1 SEND_CMD   (streaming init commands)
  svc=1, char=3  →  svc1 RECV_CMD   (streaming ACK responses)
  svc=1, char=4  →  svc1 DATA       (push-notification stream)
  svc=1, char=5  →  svc1 SEND_ACK   (per-packet ACK writes)

Confirmed by PacketLogger capture of SPARKvue at 2 kHz.
"""

import asyncio
import struct
import threading
import time
from abc import ABC, abstractmethod

# ── Constants ──────────────────────────────────────────────────────────────────

# BLE one-shot polling is reliable up to this rate.  Above it, use PushStream.
_POLL_CEILING_HZ: float = 20.0

# Seconds without data before declaring the sensor stale and exiting stream().
_STALE_TIMEOUT_S: float = 5.0

# GATT UUIDs for push-notification path.
_SVC1_SEND_CMD_UUID = "4a5c0001-0002-0000-0000-5c1e741f1c00"  # streaming init
_SVC1_DATA_UUID     = "4a5c0001-0004-0000-0000-5c1e741f1c00"  # streaming data
_SVC1_SEND_ACK_UUID = "4a5c0001-0005-0000-0000-5c1e741f1c00"  # per-packet ACK

# Sensor-specific parameters for the push init sequence.
# setup_a_n : last byte of the 0x28 setup-A command (sensor type selector)
# svc0_cmd  : 3-byte command written to svc0 SEND_CMD after init
_PUSH_SENSOR_PARAMS: dict[str, dict] = {
    "Voltage": {
        "setup_a_n": 0x04,
        "svc0_cmd":  bytes([0x06, 0x5a, 0x00]),
    },
    "Current": {
        "setup_a_n": 0x02,
        "svc0_cmd":  bytes([0x06, 0x90, 0x03]),
    },
}


# ── Base class ─────────────────────────────────────────────────────────────────

class StreamStrategy(ABC):
    """Abstract base for sensor streaming strategies.

    ``stream()`` blocks until one of the following occurs:
    - ``stop_event`` is set  (clean disconnect requested)
    - ``restart_event`` is set  (rate change; caller will restart with new strategy)
    - Sensor becomes stale / an unrecoverable error occurs

    The caller (``BLEManager._stream_sensor``) checks ``restart_event.is_set()``
    after ``stream()`` returns to decide whether to restart or exit.
    """

    @abstractmethod
    def stream(
        self,
        device,
        meas_name: str,
        t0: float,
        buf,
        stop_event: threading.Event,
        restart_event: threading.Event,
        conn: dict,
    ) -> None:
        """Block, writing (t, v) pairs into *buf* until done."""


# ── Factory ────────────────────────────────────────────────────────────────────

def make_strategy(rate_hz: float) -> "StreamStrategy":
    """Return the appropriate strategy for *rate_hz*."""
    return PollStream() if rate_hz <= _POLL_CEILING_HZ else PushStream()


# ── Poll strategy ──────────────────────────────────────────────────────────────

class PollStream(StreamStrategy):
    """One-shot polling via ``device.read_data()``.

    Adapts immediately to ``conn["poll_interval"]`` changes without restart.
    Exits on stale sensor (> _STALE_TIMEOUT_S without a valid reading) or
    on any exception from ``read_data``.
    """

    def stream(
        self,
        device,
        meas_name: str,
        t0: float,
        buf,
        stop_event: threading.Event,
        restart_event: threading.Event,
        conn: dict,
    ) -> None:
        last_data_t = time.time()
        while not stop_event.is_set() and not restart_event.is_set():
            t_poll = time.time()
            try:
                val = device.read_data(meas_name)
                if val is not None:
                    buf.append(time.time() - t0, float(val))
                    last_data_t = time.time()
                elif time.time() - last_data_t > _STALE_TIMEOUT_S:
                    break
            except Exception:
                break
            elapsed = time.time() - t_poll
            time.sleep(max(0.0, conn["poll_interval"] - elapsed))


# ── Push strategy ──────────────────────────────────────────────────────────────

class PushStream(StreamStrategy):
    """Continuous push-notification streaming.

    Init sequence (confirmed from PacketLogger capture of SPARKvue at 2 kHz):

        Write svc1 SEND_CMD  [0x01, p0, p1, p2, p3, 0x02, 0x00]  # start; p=period µs LE
        sleep 50 ms
        Write svc1 SEND_CMD  [0x28, 0x00, 0x00, N]                # setup A; N=sensor-specific
        sleep 100 ms
        Write svc1 SEND_CMD  [0x29, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00]
        sleep 100 ms
        Write svc0 SEND_CMD  svc0_cmd                              # sensor-specific
        sleep 50 ms
        Write svc0 SEND_CMD  [0x00]                                # keepalive
        sleep 50 ms

    Notification format (140 bytes):
        byte 0      : sequence counter 0-31
        bytes 1-139 : continuous uint16-LE ADC sample stream (spans packets)

    ACK (every 8 packets):
        Write svc1 SEND_ACK  [0x00, 0x00, 0x00, 0x00, seq % 32]

    Calibration:
        Pre-computed slope / intercept from pasco XML FactoryCal parameters
        to avoid calling ``_decode_data()`` per sample.  Falls back to
        PollStream if calibration cannot be extracted (e.g. unknown sensor).
    """

    def stream(
        self,
        device,
        meas_name: str,
        t0: float,
        buf,
        stop_event: threading.Event,
        restart_event: threading.Event,
        conn: dict,
    ) -> None:
        cal = _extract_calibration(device, meas_name)
        if cal is None:
            # Unknown calibration chain — fall back gracefully
            PollStream().stream(device, meas_name, t0, buf,
                                stop_event, restart_event, conn)
            return

        slope, intercept, precision = cal
        quantity = meas_name.split()[0]
        params = _PUSH_SENSOR_PARAMS.get(quantity) or _PUSH_SENSOR_PARAMS.get(meas_name)
        if params is None:
            PollStream().stream(device, meas_name, t0, buf,
                                stop_event, restart_event, conn)
            return

        rate_hz = 1.0 / conn["poll_interval"]
        period_us = max(1, int(1_000_000 / rate_hz))

        async def _run() -> None:
            data_queue: asyncio.Queue[bytes] = asyncio.Queue()

            # ── Intercept the DATA characteristic callback ─────────────────
            # We replace the callback stored in bleak's PeripheralDelegate for
            # the DATA characteristic only, restoring it in the finally block.
            # This avoids stop_notify / start_notify round-trips (which send
            # BLE commands) and leaves all other characteristic callbacks intact.
            callbacks = device._client._backend._delegate._characteristic_notify_callbacks
            data_char  = device._client.services.get_characteristic(_SVC1_DATA_UUID)
            data_handle = data_char.handle
            orig_cb = callbacks.get(data_handle)

            def _push_cb(raw: bytearray) -> None:
                data_queue.put_nowait(bytes(raw))

            callbacks[data_handle] = _push_cb

            try:
                await _send_init(device, period_us, params["setup_a_n"], params["svc0_cmd"])

                leftover   = b""
                pkt_count  = 0
                last_data_t = time.time()

                while not stop_event.is_set() and not restart_event.is_set():
                    try:
                        raw_data = await asyncio.wait_for(data_queue.get(), timeout=0.1)
                        last_data_t = time.time()
                    except asyncio.TimeoutError:
                        if time.time() - last_data_t > _STALE_TIMEOUT_S:
                            break
                        continue

                    seq     = raw_data[0] if raw_data else 0
                    payload = leftover + raw_data[1:]
                    n_smp   = len(payload) // 2
                    leftover = payload[n_smp * 2:]

                    if n_smp > 0:
                        t_batch = time.time() - t0
                        for i in range(n_smp):
                            raw_u16 = struct.unpack_from("<H", payload, i * 2)[0]
                            v = round(slope * raw_u16 + intercept, precision)
                            t_s = t_batch - n_smp / rate_hz + i / rate_hz
                            buf.append(t_s, v)

                    pkt_count += 1
                    if pkt_count % 8 == 0:
                        ack = bytes([0x00, 0x00, 0x00, 0x00, seq % 32])
                        await device._client.write_gatt_char(
                            _SVC1_SEND_ACK_UUID, ack, response=False
                        )

            finally:
                # Restore original callback; sensor stops on its own within
                # ~2 unACKed packets — no explicit stop command needed.
                if orig_cb is not None:
                    callbacks[data_handle] = orig_cb
                elif data_handle in callbacks:
                    del callbacks[data_handle]

        device._loop.run_until_complete(_run())


# ── Push init sequence ─────────────────────────────────────────────────────────

async def _send_init(
    device,
    period_us: int,
    setup_a_n: int,
    svc0_cmd: bytes,
) -> None:
    """Send the confirmed 6-command streaming init sequence."""
    p = period_us
    start_cmd = bytes([
        0x01,
        p & 0xFF, (p >> 8) & 0xFF, (p >> 16) & 0xFF, (p >> 24) & 0xFF,
        0x02, 0x00,
    ])
    # Use pasco's write() for svc0/svc1 SEND_CMD (handles UUID construction)
    await device.write(1, list(start_cmd))
    await asyncio.sleep(0.05)

    await device.write(1, [0x28, 0x00, 0x00, setup_a_n])
    await asyncio.sleep(0.1)   # sensor sends [0xC0, 0x00, 0x28] ACK during this wait

    await device.write(1, [0x29, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00])
    await asyncio.sleep(0.1)

    await device.write(0, list(svc0_cmd))
    await asyncio.sleep(0.05)

    await device.write(0, [0x00])  # keepalive
    await asyncio.sleep(0.05)


# ── Calibration extraction ─────────────────────────────────────────────────────

def _extract_calibration(
    device, meas_name: str
) -> "tuple[float, float, int] | None":
    """Return (slope, intercept, precision) from pasco's FactoryCal params.

    Follows the Select → input → FactoryCal chain, mirroring the logic in
    ble_patches.py Patch 4.  Returns None if the chain cannot be resolved
    (e.g., sensor type not yet mapped, or non-standard calibration type).
    """
    sensor_id    = 0
    measurements = device._device_measurements.get(sensor_id)
    if not measurements:
        return None

    # Locate the Select measurement for meas_name
    select_m = None
    for m in measurements.values():
        if m.get("NameTag") == meas_name and m.get("Type") == "Select":
            select_m = m
            break

    if select_m is None:
        # Try a direct FactoryCal measurement (no Select wrapper)
        for m in measurements.values():
            if m.get("NameTag") == meas_name and m.get("Type") == "FactoryCal":
                return _cal_from_factory_m(m)
        return None

    # Determine the active input index (mirrors Patch 4 range-selection logic)
    inputs_raw = select_m.get("Inputs", "0")
    inputs     = (inputs_raw.split(",") if isinstance(inputs_raw, str)
                  else [str(inputs_raw)])
    selected_idx = 0

    rsd = select_m.get("RangeSettingsDigitsCounts", "")
    if rsd:
        try:
            rs_id  = int(str(rsd).split("|")[0])
            rs_val = device._sensor_data[sensor_id].get(rs_id)
            rs_m   = measurements.get(rs_id, {})
            if rs_val is not None and "Values" in rs_m:
                parts = rs_m["Values"].split(":")
                for i in range(1, len(parts), 2):
                    if float(parts[i]) == float(rs_val):
                        selected_idx = (i - 1) // 2
                        break
        except Exception:
            selected_idx = 0

    cal_id = (int(inputs[selected_idx])
              if selected_idx < len(inputs) else int(inputs[0]))
    cal_m  = measurements.get(cal_id, {})

    if cal_m.get("Type") != "FactoryCal":
        return None
    return _cal_from_factory_m(cal_m)


def _cal_from_factory_m(m: dict) -> "tuple[float, float, int] | None":
    """Compute slope/intercept from a FactoryCal measurement dict.

    Uses the same formula as pasco's ``_calc_4_params(raw, x1, y1, x2, y2)``:
        b = (x1*y2 - x2*y1) / (x1 - x2)
        slope = (y1 - b) / x1   (or via x2 when x1 == 0)
        value = slope * raw + b
    """
    if "FactoryCalParams" in m and len(m["FactoryCalParams"]) == 4:
        params = [float(v) for v in m["FactoryCalParams"]]
    elif "Params" in m:
        params = [float(v) for v in m["Params"].split(",")]
    else:
        return None

    x1, y1, x2, y2 = params
    if x1 == x2:
        return None

    b = (x1 * y2 - x2 * y1) / (x1 - x2)
    slope = (y1 - b) / x1 if x1 != 0 else (y2 - b) / x2

    precision = m.get("Precision", 3)
    try:
        precision = int(precision)
    except (TypeError, ValueError):
        precision = 3

    return slope, b, precision
