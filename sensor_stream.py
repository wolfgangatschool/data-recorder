"""
Streaming strategies for BLE sensor data acquisition.

Two concrete strategies implement the ``StreamStrategy`` interface:

``PollStream``
    BLE one-shot polling via ``device.read_data()``.  Works reliably up to
    ~20 Hz (``_POLL_CEILING_HZ``).  No changes to sensor firmware state; safe
    at any time.

``PushStream``
    Continuous push notifications at configurable rates (> 20 Hz).
    Sends an init sequence to the sensor, intercepts the svc1 DATA
    characteristic notification callback, decodes raw ADC samples, ACKs
    every 8 packets, and sends a stop command on exit.

GATT layout (PS-3211 / PS-3212)
--------------------------------
``4a5c000{svc}-000{char}-0000-0000-5c1e741f1c00``

  svc=0, char=2  →  svc0 SEND_CMD   (polling commands, stop, keepalive)
  svc=0, char=3  →  svc0 RECV_CMD   (poll responses)
  svc=1, char=2  →  svc1 SEND_CMD   (streaming init commands)
  svc=1, char=3  →  svc1 RECV_CMD   (streaming ACK responses)
  svc=1, char=4  →  svc1 DATA       (push-notification stream)
  svc=1, char=5  →  svc1 SEND_ACK   (per-packet ACK writes)

Confirmed by PacketLogger capture of SPARKvue at 20 / 50 / 100 / 250 / 500 /
2000 Hz.

Packet format
-------------
Each notification: ``[seq:1][uint16-LE ADC samples …]``

  seq       : rolling 0–31 counter (used in ACK byte)
  payload   : rate_hz / 10 samples per packet at all rates ≤ 500 Hz
              (sensor groups exactly 100 ms of data per packet; payload is
              always an even number of bytes — no leftover at these rates)
  2000 Hz   : BLE MTU caps packet at 140 bytes → 139-byte payload →
              69 complete uint16s + 1 leftover byte that is the LOW byte of
              the next sample (prepend to following packet before unpacking)

Calibration
-----------
Pre-computed slope / intercept from pasco XML FactoryCal parameters.
Falls back to PollStream if calibration cannot be extracted.

  Voltage PS-3211:  Params="32768,0,65536,5"  → slope=5/32768, b=−5  (±5 V)
  Current PS-3212:  Params="32768,0,65536,0.1"→ slope=0.1/32768, b=−0.1 (±100 mA)
  (or the matching ±15 V / ±1 A calibration if the sensor's range register so
  indicates)

ADC range
---------
Unsigned offset binary: raw=0x0000 → −full-scale, raw=0x8000 → 0,
raw=0xFFFF → ≈+full-scale.  Both boundary values are valid measurements —
never filter them.

Timestamp design
----------------
``t_first`` anchored to wall-clock of first recording sample; sample k gets
timestamp ``t_first + k / rate_hz``.  Monotonically increasing regardless of
CoreBluetooth batching behaviour.

Transition drain
----------------
After ``_send_init``, residual old-rate packets buffered by CoreBluetooth
arrive for a brief period.  The drain phase actively consumes (and ACKs) all
incoming packets for ``_TRANSITION_DRAIN_S`` without recording any of them.
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

# Seconds to actively drain post-init residual packets before recording begins.
_TRANSITION_DRAIN_S: float = 0.5

# GATT UUIDs for push-notification path.
_SVC1_DATA_UUID     = "4a5c0001-0004-0000-0000-5c1e741f1c00"  # DATA notifications
_SVC1_SEND_ACK_UUID = "4a5c0001-0005-0000-0000-5c1e741f1c00"  # per-packet ACK writes

# Sensor-specific parameters for the push init / stop sequences.
#
# setup_a_n : last byte of the 0x28 setup-A command (sensor type selector)
# svc0_cmd  : 3-byte command written to svc0 SEND_CMD after svc1 init
#             (values confirmed from PacketLogger captures of SPARKvue;
#              exact semantics unknown but must match to get correct data)
_PUSH_SENSOR_PARAMS: dict[str, dict] = {
    "Voltage": {
        "setup_a_n": 0x04,
        "svc0_cmd":  bytes([0x06, 0x7c, 0x06]),
    },
    "Current": {
        "setup_a_n": 0x02,
        "svc0_cmd":  bytes([0x06, 0x0b, 0x03]),
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

    Init sequence (confirmed from PacketLogger captures of SPARKvue):

        Write svc1 SEND_CMD  [0x01, p0, p1, p2, p3, 0x02, 0x00]  # period µs LE
        Write svc1 SEND_CMD  [0x28, 0x00, 0x00, N]                # N = sensor type
        sleep 55 ms
        Write svc1 SEND_CMD  [0x29, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00]
        Write svc0 SEND_CMD  svc0_cmd                              # sensor-specific
        sleep 10 ms
        Write svc0 SEND_CMD  [0x00]                                # keepalive

    Stop command (sent on exit to clean up sensor state):

        Write svc0 SEND_CMD  [0x07]

    Notification format:
        byte 0      : sequence counter 0–31
        bytes 1–end : uint16-LE ADC samples
        payload len : rate_hz / 10 samples (100 ms window per packet)
                      except at 2000 Hz where BLE MTU caps at 139 payload bytes
                      (69 samples + 1 leftover byte, handled via ``leftover``
                      accumulator across packets)

    ACK (every 8 packets):
        Write svc1 SEND_ACK  [0x00, 0x00, 0x00, 0x00, last_seq % 32]
        Seq cycles: 7 → 15 → 23 → 31 → 7 → …
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

        rate_hz   = 1.0 / conn["poll_interval"]
        period_us = max(1, int(1_000_000 / rate_hz))

        async def _run() -> None:
            # ── Intercept the DATA characteristic callback ─────────────────
            backend   = device._client._backend
            callbacks = backend._delegate._characteristic_notify_callbacks
            data_char = device._client.services.get_characteristic(_SVC1_DATA_UUID)
            if data_char is None:
                return  # DATA characteristic not found — cannot push-stream

            data_handle = data_char.handle
            orig_cb     = callbacks.get(data_handle)

            data_queue: asyncio.Queue[bytes] = asyncio.Queue()

            def _push_cb(raw: bytearray) -> None:
                data_queue.put_nowait(bytes(raw))

            callbacks[data_handle] = _push_cb

            try:
                await _send_init(device, period_us,
                                 params["setup_a_n"], params["svc0_cmd"])

                # ── Phase 1: transition drain ──────────────────────────────
                # Consume residual old-rate packets for _TRANSITION_DRAIN_S.
                # ACK every 8 to keep the sensor alive.
                drain_deadline  = time.monotonic() + _TRANSITION_DRAIN_S
                drain_pkt_count = 0
                while time.monotonic() < drain_deadline:
                    if stop_event.is_set() or restart_event.is_set():
                        return
                    try:
                        raw_data = await asyncio.wait_for(
                            data_queue.get(), timeout=0.05
                        )
                        drain_pkt_count += 1
                        if drain_pkt_count % 8 == 0 and raw_data:
                            seq = raw_data[0] if raw_data else 0
                            ack = bytes([0x00, 0x00, 0x00, 0x00, seq % 32])
                            await device._client.write_gatt_char(
                                _SVC1_SEND_ACK_UUID, ack, response=False
                            )
                    except asyncio.TimeoutError:
                        pass

                # ── Phase 2: recording ─────────────────────────────────────
                # t_first anchored to wall-clock of first sample so timestamps
                # are evenly spaced regardless of CoreBluetooth batch delivery.
                # leftover accumulates the single split byte that occurs at
                # 2000 Hz (139-byte payload → 69 complete uint16s + 1 byte).
                t_first:      float | None = None
                sample_count: int          = 0
                pkt_count:    int          = 0
                leftover:     bytes        = b""
                last_data_t                = time.monotonic()

                while not stop_event.is_set() and not restart_event.is_set():
                    try:
                        raw_data = await asyncio.wait_for(
                            data_queue.get(), timeout=0.1
                        )
                        last_data_t = time.monotonic()
                    except asyncio.TimeoutError:
                        if time.monotonic() - last_data_t > _STALE_TIMEOUT_S:
                            break
                        continue

                    seq     = raw_data[0] if raw_data else 0
                    payload = leftover + raw_data[1:]
                    n_smp   = len(payload) // 2
                    leftover = payload[n_smp * 2:]   # 0 bytes at ≤500 Hz, 1 byte at 2 kHz

                    if n_smp > 0:
                        if t_first is None:
                            t_first = time.time() - t0

                        for i in range(n_smp):
                            raw_u16 = struct.unpack_from("<H", payload, i * 2)[0]
                            t_s = t_first + sample_count / rate_hz
                            v   = round(slope * raw_u16 + intercept, precision)
                            buf.append(t_s, v)
                            sample_count += 1

                    pkt_count += 1
                    if pkt_count % 8 == 0:
                        ack = bytes([0x00, 0x00, 0x00, 0x00, seq % 32])
                        await device._client.write_gatt_char(
                            _SVC1_SEND_ACK_UUID, ack, response=False
                        )

            finally:
                # Restore original callback so pasco regains control.
                if orig_cb is not None:
                    callbacks[data_handle] = orig_cb
                elif data_handle in callbacks:
                    del callbacks[data_handle]

                # Send stop command so the sensor ceases streaming.
                # This keeps the sensor state clean for a subsequent init
                # (rate change) and avoids saturating CoreBluetooth buffers
                # while the connection is idle.
                try:
                    await _send_stop(device)
                except Exception:
                    pass

        device._loop.run_until_complete(_run())


# ── Push init / stop sequences ─────────────────────────────────────────────────

async def _send_init(
    device,
    period_us: int,
    setup_a_n: int,
    svc0_cmd: bytes,
) -> None:
    """Send the push-streaming init sequence (confirmed from PacketLogger)."""
    p = period_us
    start_cmd = bytes([
        0x01,
        p & 0xFF, (p >> 8) & 0xFF, (p >> 16) & 0xFF, (p >> 24) & 0xFF,
        0x02, 0x00,
    ])
    await device.write(1, list(start_cmd))
    await device.write(1, [0x28, 0x00, 0x00, setup_a_n])
    await asyncio.sleep(0.055)   # sensor ACKs 0x28 during this window

    await device.write(1, [0x29, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00])
    await asyncio.sleep(0.055)   # ~55 ms gap observed in all SPARKvue captures

    await device.write(0, list(svc0_cmd))
    await asyncio.sleep(0.010)

    await device.write(0, [0x00])   # keepalive


async def _send_stop(device) -> None:
    """Tell the sensor to stop streaming (confirmed from PacketLogger)."""
    await device.write(0, [0x07])
    await asyncio.sleep(0.050)


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

    For Params="32768,0,65536,y2":
        b = (32768×y2 - 0) / (32768 - 65536) = -y2
        slope = (0 - (-y2)) / 32768 = y2 / 32768
    So raw=0x0000 → -y2 (negative full-scale), raw=0x8000 → 0, raw=0xFFFF ≈ +y2.
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
