"""
Compatibility patches for running PASCO BLE sensors on macOS with Python 3.14
and nest_asyncio.

Background
----------
nest_asyncio forces asyncio to use the pure-Python Task implementation
(_PyTask), which tracks the running task in the Python-level dict
``asyncio.tasks._current_tasks``.  However, ``asyncio.current_task()`` is the
*C* extension that reads C-level task tracking — it never sees _PyTask entries
and always returns None inside a pasco coroutine.

Both ``asyncio.timeout()`` and ``asyncio.TaskGroup()`` abort with
  "Timeout should be used inside a task"
  "TaskGroup cannot determine the parent task"
when ``current_task()`` returns None.

Three patches are applied here. Each is guarded by a sentinel attribute so
that re-importing this module does not stack another wrapper on top of an
already-patched function (that would cause a RecursionError).

This module is imported for its side-effects only.  It must be imported before
any pasco or bleak code runs.
"""

import asyncio
import asyncio.events as _asyncio_events
import asyncio.tasks as _asyncio_tasks

from bleak import BleakClient
from bleak.backends.corebluetooth.CentralManagerDelegate import CentralManagerDelegate
from pasco.pasco_ble_device import PASCOBLEDevice


# ── Patch 1: asyncio.current_task ─────────────────────────────────────────────

def _patch_current_task() -> None:
    """
    Replace asyncio.current_task() with a version that falls back to the
    Python-level _current_tasks dict when the C implementation returns None.

    This makes asyncio.timeout() and asyncio.TaskGroup() work correctly inside
    a nest_asyncio-patched event loop running _PyTask instances.
    """
    if getattr(asyncio.current_task, "_pasco_patched", False):
        return  # already applied — do not rewrap

    _c_current_task = asyncio.current_task

    def _patched(loop=None):
        # Try the C implementation first (fast path, works outside nest_asyncio).
        task = _c_current_task(loop)
        if task is not None:
            return task
        # Fallback: look up the Python-level tracking dict.
        running = _asyncio_events._get_running_loop()
        if running is None:
            return None
        return _asyncio_tasks._current_tasks.get(running)

    _patched._pasco_patched = True
    asyncio.current_task = _patched
    asyncio.tasks.current_task = _patched


# ── Patch 2: CentralManagerDelegate.connect ───────────────────────────────────

def _patch_central_manager_connect() -> None:
    """
    Replace the asyncio.timeout()-based connect with a loop.call_later()-based
    version.

    asyncio.timeout() calls current_task() internally.  Even after Patch 1,
    the async_timeout back-end shipped with older bleak versions uses the C
    function directly.  loop.call_later() schedules a plain cancellation handle
    and bypasses the task check entirely.
    """
    if getattr(CentralManagerDelegate.connect, "_pasco_patched", False):
        return

    async def _connect(self, peripheral, disconnect_callback, timeout=10.0):
        self._disconnect_callbacks[peripheral.identifier()] = disconnect_callback
        future = self.event_loop.create_future()
        self._connect_futures[peripheral.identifier()] = future

        # Schedule future cancellation after `timeout` seconds using call_later.
        # This avoids asyncio.timeout() and its current_task() requirement.
        _handle = self.event_loop.call_later(
            timeout,
            lambda: future.cancel() if not future.done() else None,
        )
        try:
            self.central_manager.connectPeripheral_options_(peripheral, None)
            try:
                await future
            except asyncio.CancelledError:
                raise asyncio.TimeoutError()
        except asyncio.TimeoutError:
            # Clean up before re-raising so no stale futures linger.
            del self._connect_futures[peripheral.identifier()]
            disc_future = self.event_loop.create_future()
            self._disconnect_futures[peripheral.identifier()] = disc_future
            try:
                self.central_manager.cancelPeripheralConnection_(peripheral)
                await disc_future
            finally:
                del self._disconnect_futures[peripheral.identifier()]
            del self._disconnect_callbacks[peripheral.identifier()]
            raise
        finally:
            _handle.cancel()
            self._connect_futures.pop(peripheral.identifier(), None)

    _connect._pasco_patched = True
    CentralManagerDelegate.connect = _connect


# ── Patch 3: PASCOBLEDevice.connect ───────────────────────────────────────────

def _patch_pasco_connect() -> None:
    """
    Pass the full BLEDevice object to BleakClient instead of just the address.

    Pasco's default connect() extracts only ble_device.address (a string) and
    passes that to BleakClient.  On macOS, BleakClient with a bare address
    starts a *new* device discovery on a different CBCentralManager instance,
    which fails because the CBPeripheral from the earlier scan is not shared.

    Passing the full BLEDevice lets BleakClient reuse the CBPeripheral that
    was already found by the preceding scan — same CBCentralManager, same loop.
    """
    if getattr(PASCOBLEDevice.connect, "_pasco_patched", False):
        return

    def _connect(self, ble_device):
        if ble_device is None:
            raise self.InvalidParameter
        if self._client is not None:
            raise self.BLEAlreadyConnectedError("Device already connected")

        # Pass the full BLEDevice object, not just .address.
        self._client = BleakClient(ble_device)
        try:
            self._loop.run_until_complete(self._async_connect())
        except Exception as exc:
            raise self.BLEConnectionError(f"Could not connect: {exc}")

        self._set_device_params(ble_device)
        if self._dev_type == "Rotary Motion":
            self._loop.run_until_complete(
                self.write_await_callback(self.SENSOR_SERVICE_ID, self.WIRELESS_RMS_START)
            )
        try:
            self.initialize_device()
        except Exception as exc:
            raise self.BLEConnectionError(f"initialize_device failed: {exc}")

    _connect._pasco_patched = True
    PASCOBLEDevice.connect = _connect


# ── Patch 4: Fix Select-measurement input selection and precision ──────────────

def _patch_select_measurement() -> None:
    """
    Fix two bugs in PASCOBLEDevice._calculate_with_input for Select-type
    measurements (used by WirelessCurrentSensor PS-3212 and WirelessVoltageSensor
    PS-3211).

    Bug 1 — Wrong range selection: pasco always uses inputs[0] regardless of
    the sensor's actual range setting, causing the WirelessCurrentSensor to
    report values ≈10× too small (±100 mA calibration used when sensor is in
    the default ±1 A mode).

    Bug 2 — Pre-rounded intermediate values: CalCurrent (Internal=1) carries
    Precision=2, so _decode_data rounds it to 10 mA steps before the Select
    measurement reads it.  By always recomputing via _get_measurement_value
    (which reads from the raw integer stored in _sensor_data by the first
    decode loop, not from the Precision=2-rounded intermediate value), full
    ADC precision flows through to the Precision=3 (1 mA) Select output.

    Fix applies only to Select measurements that carry a
    RangeSettingsDigitsCounts attribute encoding the range-selector constant
    measurement ID and the per-range digit counts.  For Select measurements
    without that attribute (e.g. the voltage sensor), the original behaviour
    is preserved.
    """
    if getattr(PASCOBLEDevice._calculate_with_input,
               "_pasco_patched_select", False):
        return

    _orig = PASCOBLEDevice._calculate_with_input

    def _patched(self, m, sensor_id):
        if m.get('Type') != 'Select':
            return _orig(self, m, sensor_id)

        inputs = m['Inputs'].split(',') if isinstance(m['Inputs'], str) else [str(m['Inputs'])]
        selected_idx = 0   # default: first input (original pasco behaviour)

        rsd = m.get('RangeSettingsDigitsCounts', '')
        if rsd:
            try:
                rs_id  = int(str(rsd).split('|')[0])
                rs_val = self._sensor_data[sensor_id].get(rs_id)
                rs_m   = self._device_measurements[sensor_id].get(rs_id, {})
                if rs_val is not None and 'Values' in rs_m:
                    # Values format: "label1:value1:label2:value2:…"
                    parts = rs_m['Values'].split(':')
                    for i in range(1, len(parts), 2):
                        if float(parts[i]) == float(rs_val):
                            selected_idx = (i - 1) // 2
                            break
            except Exception:
                selected_idx = 0   # fall back to first input on any error

        need_input = (int(inputs[selected_idx])
                      if selected_idx < len(inputs) else int(inputs[0]))

        # Always recompute from raw to bypass any Precision-truncated value
        # stored in _sensor_data during an earlier _decode_data iteration.
        return self._get_measurement_value(sensor_id, need_input)

    _patched._pasco_patched_select = True
    PASCOBLEDevice._calculate_with_input = _patched


# ── Patch 5: Drop push-stream data packets in process_measurement_response ────

def _patch_process_measurement_response() -> None:
    """
    Silently discard push-stream data packets that reach pasco's measurement
    notification handler.

    When PushStream intercepts the DATA characteristic callback (by replacing
    the entry in bleak's _characteristic_notify_callbacks dict), pasco's own
    callback for that characteristic is bypassed.  However, if the interception
    is not fully effective (e.g. the DATA handle is not pre-registered by pasco
    at connect time), push-stream packets can still reach
    process_measurement_response, which then calls _decode_data() and tries
    asyncio.create_task(_decode_data()), failing with TypeError because
    _decode_data is synchronous and returns None.  A second problem is pasco's
    _send_ack, which passes the already-resolved UUID string back through
    write(), causing a ValueError.

    Push-stream data packets are identified by their first byte being a rolling
    sequence counter in 0–31 (≤ 0x1F).  When detected, this patch resets the
    internal ACK counter and returns without calling _decode_data or _send_ack.
    """
    if getattr(PASCOBLEDevice.process_measurement_response,
               "_pasco_patched_push_drop", False):
        return

    _orig_pmr = PASCOBLEDevice.process_measurement_response

    def _patched(self, sensor_id, data):
        if data and data[0] <= 0x1F:
            # Push-stream sequence packet: reset ACK counter and discard.
            self._data_ack_counter = 0
            return
        return _orig_pmr(self, sensor_id, data)

    _patched._pasco_patched_push_drop = True
    PASCOBLEDevice.process_measurement_response = _patched


# ── Apply all patches once at import time ─────────────────────────────────────

_patch_current_task()
_patch_central_manager_connect()
_patch_pasco_connect()
_patch_select_measurement()
_patch_process_measurement_response()
