"""
BLE sensor management with Qt signal-based UI communication.

All BLE I/O is serialised via _ble_lock (one CoreBluetooth manager active at
a time on macOS).  Background threads communicate with the UI exclusively by:
  1. Emitting Qt signals (delivered to the main thread via Qt's queued event loop).
  2. Writing to LiveBuffer objects (lock-protected ring buffers in data_store).

Background threads never touch any UI widget or Qt widget state directly.

Thread model
------------
  Main thread  — creates BLEManager, calls connect_sensor / disconnect_sensor,
                 calls poll_cleanup() from a QTimer.
  Scan thread  — one at a time, acquires _ble_lock, emits scan_complete.
  Connect thread (one per sensor)
               — acquires _ble_lock (scan + connect), then streams until
                 stop_event is set or sensor goes silent.

Signal summary
--------------
  scan_started()                 — scan thread has been launched
  scan_complete(list[SensorMeta])— results of the latest scan
  status_changed(addr, status)   — lifecycle transitions:
                                   "connecting…" | "connected" |
                                   "disconnecting…" | "disconnected" |
                                   "removed" | "error: <msg>"
  unit_discovered(addr, unit)    — emitted once when the sensor's measurement
                                   unit becomes known (after connect)
"""

import re
import threading
import time

from PyQt6.QtCore import QObject, pyqtSignal

try:
    import ble_patches  # noqa: F401 — applies macOS pasco compatibility fixes
    from bleak import BleakScanner
    from bleak.backends.device import BLEDevice
    from pasco.pasco_ble_device import PASCOBLEDevice
    PASCO_AVAILABLE = True
except ImportError:
    PASCO_AVAILABLE = False

from data_store import LiveStore, SensorMeta


# ── Module-level BLE state ─────────────────────────────────────────────────────

# Serialise all BLE I/O: only one CBCentralManager active at a time on macOS.
_ble_lock = threading.Lock()

# Strong reference so the CBDelegate is not garbage-collected between callbacks.
_scan_device_ref = None

# Seconds between automatic background scans in Live Discovery mode.
# Target: a nearby sensor appears in the dropdown within ~5 s of being powered on.
SCAN_INTERVAL_S: float = 5.0

# Sensor streaming considered stale after this many seconds of no data.
STALE_TIMEOUT_S: float = 5.0

# Shared time origin — set when the first sensor starts streaming;
# reset to None when all sensors have been removed.
_global_t0: float | None = None
_t0_lock = threading.Lock()


# ── [WORKAROUND] Corrupted-advertising-name support ───────────────────────────
# Two development sensors (390-900, 910-042) had their BLE advertising names
# corrupted by accidental probe commands during reverse-engineering work.  They
# now advertise as e.g. '\x02 390-900>78' instead of 'Voltage 390-900>78', so
# pasco.scan() (which filters by 'Voltage', 'Current', …) cannot find them.
#
# This block reconstructs the correct advertising name from the model-number
# suffix that is still intact in the advertisement, enabling these sensors to be
# used for further development without damaging additional units.
#
# REMOVE THIS ENTIRE BLOCK (and the _do_scan / _connect_thread_fn changes below
# marked [WORKAROUND]) once the development sensors are retired or reflashed.
# Primary behaviour — intact sensors — is unaffected: they still flow through
# pasco.scan() exactly as before.

_PASCO_MODEL_RE = re.compile(r'\d{3}-\d{3}>\w{2}')


def _reconstruct_pasco_name(raw_name: str) -> str | None:
    """Return the correct 'AdvertisingName model>id' string for a sensor whose
    advertising prefix is corrupted, or None if the name is not a PASCO model."""
    m = _PASCO_MODEL_RE.search(raw_name or "")
    if not m:
        return None
    suffix = m.group()          # e.g. "390-900>78"
    if len(suffix) < 9:
        return None
    try:
        d = PASCOBLEDevice()
        iface_id = d._decode64(suffix[8]) + 1024
        iface = d._xml_root.find(f'./Interfaces/Interface[@ID="{iface_id}"]')
        if iface is not None:
            return f"{iface.get('AdvertisingName')} {suffix}"
    except Exception:
        pass
    return None

# ── [END WORKAROUND] ───────────────────────────────────────────────────────────


# ── Device metadata ────────────────────────────────────────────────────────────

def _parse_ble_device(ble_device) -> SensorMeta:
    """Extract structured metadata from a raw BLEDevice returned by pasco.scan()."""
    name     = (ble_device.name or "").split(">")[0].strip()
    address  = ble_device.address or ""
    parts    = name.split()
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
    return SensorMeta(
        quantity=quantity, model=model, sensor_id=sensor_id,
        address=address, ble_device=ble_device,
    )


# ── BLEManager ─────────────────────────────────────────────────────────────────

class BLEManager(QObject):
    """Manages BLE sensor discovery, connection, and data streaming.

    All public methods must be called from the UI (main) thread.
    Background threads communicate back via Qt signals, which Qt delivers to
    connected slots on the main thread via the queued-connection mechanism.
    """

    # Discovery
    scan_started  = pyqtSignal()
    scan_complete = pyqtSignal(list)        # list[SensorMeta]

    # Per-sensor lifecycle — status values documented in module docstring
    status_changed  = pyqtSignal(str, str)  # addr, status
    unit_discovered = pyqtSignal(str, str)  # addr, unit

    def __init__(self, live_store: LiveStore, parent=None) -> None:
        super().__init__(parent)
        self._live_store        = live_store
        self._scan_in_progress  = False
        self._scan_last_t: float = 0.0
        # Per-address connection records (owned by the main thread).
        # Background threads write only to conn["status"].
        self._conns: dict[str, dict] = {}
        self._managed_addrs: set[str] = set()
        # Metadata cache so the sensor panel can display info after connection.
        self._meta: dict[str, SensorMeta] = {}

    # ── Read-only properties ───────────────────────────────────────────────────

    @property
    def managed_addrs(self) -> frozenset[str]:
        return frozenset(self._managed_addrs)

    @property
    def scan_in_progress(self) -> bool:
        return self._scan_in_progress

    @property
    def scan_last_t(self) -> float:
        return self._scan_last_t

    def get_status(self, addr: str) -> str:
        return self._conns.get(addr, {}).get("status", "")

    def get_unit(self, addr: str) -> str | None:
        return self._conns.get(addr, {}).get("unit")

    def get_label(self, addr: str) -> str:
        return self._conns.get(addr, {}).get("label", addr[-6:])

    def get_meta(self, addr: str) -> SensorMeta | None:
        return self._meta.get(addr)

    # ── Public API (main thread only) ─────────────────────────────────────────

    def start_scan(self) -> None:
        """Kick off a background scan if one is not already running."""
        if self._scan_in_progress:
            return
        self._scan_in_progress = True
        self.scan_started.emit()
        threading.Thread(target=self._do_scan, daemon=True).start()

    def connect_sensor(self, meta: SensorMeta) -> None:
        """Add a sensor to the managed set and start its connection thread."""
        addr = meta.address
        if addr in self._managed_addrs:
            return
        self._managed_addrs.add(addr)
        self._meta[addr] = meta
        conn = {
            "status":     "starting…",
            "stop_event": threading.Event(),
            "thread":     None,
            "unit":       None,
            "label":      meta.quantity,
        }
        self._conns[addr] = conn
        t = threading.Thread(
            target=self._connect_thread_fn,
            args=(meta, conn),
            daemon=True,
        )
        conn["thread"] = t
        t.start()
        self.status_changed.emit(addr, "connecting…")

    def disconnect_sensor(self, addr: str) -> None:
        """Two-phase disconnect.

        Phase 1 (this call): signal the streaming thread, keep addr in the
        managed set so the UI still shows "disconnecting…" — not the dropdown.
        Phase 2 (poll_cleanup): remove addr after the thread confirms it exited.
        """
        conn = self._conns.get(addr)
        if conn is None:
            self._managed_addrs.discard(addr)
            return
        status = conn.get("status", "")
        if status == "disconnected" or status.startswith("error"):
            self._cleanup(addr)
            return
        conn["status"] = "disconnecting…"
        stop_evt = conn.get("stop_event")
        if stop_evt:
            stop_evt.set()
        self._live_store.remove_addr(addr)
        self.status_changed.emit(addr, "disconnecting…")

    def poll_cleanup(self) -> None:
        """Detect sensors whose threads have exited and complete Phase 2.

        Must be called periodically from a UI-thread QTimer (e.g. every 300 ms).
        """
        for addr in list(self._managed_addrs):
            status = self._conns.get(addr, {}).get("status", "")
            if status == "disconnected" or status.startswith("error"):
                self._cleanup(addr)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _cleanup(self, addr: str) -> None:
        """Phase 2: fully remove a sensor from the managed set."""
        self._conns.pop(addr, None)
        self._managed_addrs.discard(addr)
        self._live_store.remove_addr(addr)
        global _global_t0
        if not self._managed_addrs:
            with _t0_lock:
                _global_t0 = None
        self.status_changed.emit(addr, "removed")

    # ── Background threads ─────────────────────────────────────────────────────

    def _do_scan(self) -> None:
        """Background: scan for sensors and emit scan_complete."""
        global _scan_device_ref
        with _ble_lock:
            try:
                device       = PASCOBLEDevice()
                _scan_device_ref = device
                found        = device.scan() or []
            except Exception:
                found = []

            # [WORKAROUND] Also pick up sensors whose advertising names are
            # corrupted and therefore missed by pasco.scan().  Run a raw bleak
            # scan on pasco's own loop (same CBCentralManager) and reconstruct
            # the correct name from the intact model-number suffix.
            # Remove this block when the broken development sensors are retired.
            try:
                found_addrs = {d.address for d in found}
                all_ble = device._loop.run_until_complete(
                    BleakScanner.discover(timeout=2.0)
                ) or []
                for ble_dev in all_ble:
                    if ble_dev.address in found_addrs:
                        continue
                    reconstructed = _reconstruct_pasco_name(ble_dev.name)
                    if reconstructed:
                        found.append(BLEDevice(
                            ble_dev.address, reconstructed,
                            ble_dev.details, rssi=-60,
                        ))
            except Exception:
                pass
            # [END WORKAROUND]

        results = [_parse_ble_device(d) for d in found]
        self._scan_in_progress = False
        self._scan_last_t      = time.time()
        self.scan_complete.emit(results)   # delivered to main thread

    def _connect_thread_fn(self, meta: SensorMeta, conn: dict) -> None:
        """Background: scan again (to get a fresh CBPeripheral), connect, stream."""
        conn["status"] = "connecting…"
        with _ble_lock:
            if conn["stop_event"].is_set():
                conn["status"] = "disconnected"
                return
            device = PASCOBLEDevice()
            try:
                found = device.scan() or []
                match = next((d for d in found if d.address == meta.address), None)

                # [WORKAROUND] Fall back to raw bleak scan for sensors whose
                # corrupted names are not returned by pasco.scan().
                # Remove when broken development sensors are retired.
                if match is None:
                    try:
                        all_ble = device._loop.run_until_complete(
                            BleakScanner.discover()
                        ) or []
                        raw = next(
                            (d for d in all_ble if d.address == meta.address),
                            None,
                        )
                        if raw is not None:
                            reconstructed = _reconstruct_pasco_name(raw.name)
                            if reconstructed:
                                match = BLEDevice(
                                    raw.address, reconstructed,
                                    raw.details, rssi=-60,
                                )
                    except Exception:
                        pass
                # [END WORKAROUND]

                if match is None:
                    conn["status"] = "error: sensor not found (out of range?)"
                    self.status_changed.emit(meta.address, conn["status"])
                    return
                device.connect(match)
            except Exception as exc:
                conn["status"] = f"error: {exc}"
                self.status_changed.emit(meta.address, conn["status"])
                return

        if conn["stop_event"].is_set():
            try:
                device.disconnect()
            except Exception:
                pass
            conn["status"] = "disconnected"
            return

        self._stream_sensor(device, meta.address, conn)

    def _stream_sensor(self, device, address: str, conn: dict) -> None:
        """Background: read measurements in a loop until stop_event is set."""
        # ── Discover measurement name and unit ────────────────────────────────
        try:
            meas_list = device.get_measurement_list()
            if not meas_list:
                conn["status"] = "error: no measurements found"
                self.status_changed.emit(address, conn["status"])
                device.disconnect()
                return
            meas_name = meas_list[0]

            unit = None
            for ch_m in device._device_measurements.values():
                for m_attrs in ch_m.values():
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
            self.status_changed.emit(address, conn["status"])
            try:
                device.disconnect()
            except Exception:
                pass
            return

        # ── Set up buffer and notify UI ───────────────────────────────────────
        buf = self._live_store.get_or_create(unit, address)
        conn.update({"status": "connected", "unit": unit})
        self.status_changed.emit(address, "connected")
        self.unit_discovered.emit(address, unit)

        # ── Establish shared time origin ──────────────────────────────────────
        global _global_t0
        with _t0_lock:
            if _global_t0 is None:
                _global_t0 = time.time()
            t0 = _global_t0

        # ── Streaming loop ────────────────────────────────────────────────────
        stop_event     = conn["stop_event"]
        last_data_time = time.time()
        while not stop_event.is_set():
            try:
                val = device.read_data(meas_name)
                if val is not None:
                    buf.append(time.time() - t0, float(val))
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
        # Signal to poll_cleanup() on the main thread.
        conn["status"] = "disconnected"
