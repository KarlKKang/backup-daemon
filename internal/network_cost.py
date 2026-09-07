import ctypes
import sys
import threading
from dataclasses import dataclass
from typing import List, Optional
import traceback
import time

if __name__ == "__main__":
    # for debugging purposes
    log = print
    IS_WINDOWS = sys.platform == "win32"
    IS_DARWIN = sys.platform == "darwin"
else:
    from .log import log
    from .platform import IS_WINDOWS, IS_DARWIN


__all__ = ["NetworkCost", "get_network_cost", "should_limit_network_usage"]


@dataclass(frozen=True)
class NetworkCost:
    """Cost of the connection currently carrying internet traffic.

    Fields are None when the platform or backend cannot report them.
    """

    metered: bool  # Windows: Fixed/Variable cost. macOS: Low Data Mode.
    expensive: Optional[bool]  # macOS: cellular or Personal Hotspot
    cellular: Optional[bool]
    roaming: Optional[bool]
    over_data_limit: Optional[bool]
    approaching_data_limit: Optional[bool]
    congested: Optional[bool]

    @property
    def should_limit(self) -> bool:
        """True if the app should hold back non-essential traffic."""
        return any(
            (
                self.metered,
                self.expensive,
                self.cellular,
                self.roaming,
                self.over_data_limit,
                self.approaching_data_limit,
                self.congested,
            )
        )

    def __str__(self):
        return (
            f"metered / low data    : {self.metered}\n"
            f"expensive             : {self.expensive}\n"
            f"cellular              : {self.cellular}\n"
            f"roaming               : {self.roaming}\n"
            f"over data limit       : {self.over_data_limit}\n"
            f"approaching data limit: {self.approaching_data_limit}\n"
            f"congested             : {self.congested}\n"
            f"should limit          : {self.should_limit}"
        )


# ==========================================================================
# Windows -- INetworkCostManager COM
# ==========================================================================

_NLM_UNRESTRICTED = 0x1
_NLM_FIXED = 0x2
_NLM_VARIABLE = 0x4
_NLM_OVERDATALIMIT = 0x10000
_NLM_CONGESTED = 0x20000
_NLM_ROAMING = 0x40000
_NLM_APPROACHINGDATALIMIT = 0x80000


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_CLSID_NetworkListManager = "{DCB00C01-570F-4A9B-8D69-199FDBA5723B}"
_IID_INetworkCostManager = "{DCB00008-570F-4A9B-8D69-199FDBA5723B}"

_CLSCTX_ALL = 0x17
_COINIT_APARTMENTTHREADED = 0x2
_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = 0x80010106  # -2147417850


def _win_query_com() -> Optional[NetworkCost]:
    from ctypes import wintypes

    ole32 = ctypes.WinDLL("ole32")
    ole32.CLSIDFromString.argtypes = [wintypes.LPCOLESTR, ctypes.POINTER(_GUID)]
    ole32.CLSIDFromString.restype = wintypes.HRESULT
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    ole32.CoCreateInstance.restype = wintypes.HRESULT
    ole32.CoInitializeEx.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    ole32.CoInitializeEx.restype = wintypes.HRESULT

    clsid, iid = _GUID(), _GUID()
    if (
        hr := ole32.CLSIDFromString(_CLSID_NetworkListManager, ctypes.byref(clsid))
    ) != _S_OK:
        log(f"CLSIDFromString failed for NetworkListManager: {hr}", sys.stderr)
        return None
    if (
        hr := ole32.CLSIDFromString(_IID_INetworkCostManager, ctypes.byref(iid))
    ) != _S_OK:
        log(f"CLSIDFromString failed for INetworkCostManager: {hr}", sys.stderr)
        return None

    hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if hr not in (_S_OK, _S_FALSE) and (hr & 0xFFFFFFFF) != _RPC_E_CHANGED_MODE:
        log(f"CoInitializeEx failed: {hr}", sys.stderr)
        return None
    must_uninitialize = hr in (_S_OK, _S_FALSE)

    ptr = ctypes.c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_ALL, ctypes.byref(iid), ctypes.byref(ptr)
        )
        if hr != _S_OK or not ptr:
            log(f"CoCreateInstance failed: {hr}", sys.stderr)
            return None

        # vtable: 0 QueryInterface, 1 AddRef, 2 Release, 3 GetCost
        vtbl = ctypes.cast(
            ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        get_cost = ctypes.WINFUNCTYPE(
            wintypes.HRESULT,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        )(vtbl[3])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])

        flags = wintypes.DWORD(0)
        try:
            # NULL destination address => cost of the default route
            hr = get_cost(ptr, ctypes.byref(flags), None)
        finally:
            release(ptr)

        if hr != _S_OK:
            log(f"GetCost failed: {hr}", sys.stderr)
            return None

        value = flags.value
        metered = bool(value & (_NLM_FIXED | _NLM_VARIABLE))
        return NetworkCost(
            metered=metered,
            expensive=metered,
            cellular=None,  # not reported by this interface
            roaming=bool(value & _NLM_ROAMING),
            over_data_limit=bool(value & _NLM_OVERDATALIMIT),
            approaching_data_limit=bool(value & _NLM_APPROACHINGDATALIMIT),
            congested=bool(value & _NLM_CONGESTED),
        )
    finally:
        if must_uninitialize:
            ole32.CoUninitialize()


_win_last_queried: float = None
_win_cached_result: NetworkCost = None


def _win_query() -> Optional[NetworkCost]:
    global _win_last_queried
    global _win_cached_result
    if (
        _win_cached_result is not None
        and _win_last_queried is not None
        and (time.monotonic() - _win_last_queried) < 1
    ):
        return _win_cached_result
    _win_cached_result = _win_query_com()
    _win_last_queried = time.monotonic()
    return _win_cached_result


# ==========================================================================
# macOS -- NWPathMonitor via ctypes
# ==========================================================================

_NW_IFTYPE_WIFI = 1
_NW_IFTYPE_CELLULAR = 2
_NW_IFTYPE_WIRED = 3
_NW_PATH_SATISFIED = 1
_BLOCK_IS_GLOBAL = 1 << 28

_mac_lock = threading.Lock()
_mac_ready = threading.Event()
_mac_state: Optional[dict] = None
_mac_started = False
_mac_failed = False
_mac_keepalive: List[object] = []  # blocks and callbacks must outlive the monitor


class _BlockDescriptor(ctypes.Structure):
    _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]


class _Block(ctypes.Structure):
    _fields_ = [
        ("isa", ctypes.c_void_p),
        ("flags", ctypes.c_int),
        ("reserved", ctypes.c_int),
        ("invoke", ctypes.c_void_p),
        ("descriptor", ctypes.POINTER(_BlockDescriptor)),
    ]


def _mac_make_block(cfunc, libsystem) -> _Block:
    """Wrap a ctypes callback in a no-capture global block literal."""
    isa = ctypes.addressof(ctypes.c_void_p.in_dll(libsystem, "_NSConcreteGlobalBlock"))
    descriptor = _BlockDescriptor(0, ctypes.sizeof(_Block))
    block = _Block(
        isa=isa,
        flags=_BLOCK_IS_GLOBAL,
        reserved=0,
        invoke=ctypes.cast(cfunc, ctypes.c_void_p),
        descriptor=ctypes.pointer(descriptor),
    )
    _mac_keepalive.extend((cfunc, descriptor, block))
    return block


def _mac_state_to_cost(state: dict) -> NetworkCost:
    return NetworkCost(
        metered=state["constrained"],  # Low Data Mode
        expensive=state["expensive"],  # cellular or Personal Hotspot
        cellular=state["cellular"],
        roaming=None,
        over_data_limit=None,
        approaching_data_limit=None,
        congested=None,
    )


def _mac_start() -> bool:
    """Create and start the shared path monitor. Returns False if unavailable."""
    global _mac_started, _mac_failed, _mac_state

    with _mac_lock:
        if _mac_started:
            return True
        if _mac_failed:
            return False

        try:
            libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            net = ctypes.CDLL("/System/Library/Frameworks/Network.framework/Network")

            net.nw_path_monitor_create.restype = ctypes.c_void_p
            net.nw_path_monitor_create.argtypes = []
            net.nw_path_monitor_set_update_handler.restype = None
            net.nw_path_monitor_set_update_handler.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            net.nw_path_monitor_set_queue.restype = None
            net.nw_path_monitor_set_queue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            net.nw_path_monitor_start.restype = None
            net.nw_path_monitor_start.argtypes = [ctypes.c_void_p]
            net.nw_path_get_status.restype = ctypes.c_int
            net.nw_path_get_status.argtypes = [ctypes.c_void_p]
            net.nw_path_is_expensive.restype = ctypes.c_bool
            net.nw_path_is_expensive.argtypes = [ctypes.c_void_p]
            net.nw_path_uses_interface_type.restype = ctypes.c_bool
            net.nw_path_uses_interface_type.argtypes = [ctypes.c_void_p, ctypes.c_int]
            libsystem.dispatch_queue_create.restype = ctypes.c_void_p
            libsystem.dispatch_queue_create.argtypes = [
                ctypes.c_char_p,
                ctypes.c_void_p,
            ]
        except (OSError, AttributeError):
            log(traceback.format_exc(), sys.stderr)
            _mac_failed = True
            return False

        # nw_path_is_constrained (Low Data Mode) is macOS 10.15+.
        try:
            is_constrained = net.nw_path_is_constrained
            is_constrained.restype = ctypes.c_bool
            is_constrained.argtypes = [ctypes.c_void_p]
        except AttributeError:
            is_constrained = None

        handler_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

        def _on_path_update(_block, path):
            # Runs on a dispatch worker thread. Everything is read here rather
            # than retaining the nw_path_t, so there are no lifetime concerns.
            global _mac_state
            try:
                state = {
                    "satisfied": net.nw_path_get_status(path) == _NW_PATH_SATISFIED,
                    "constrained": (
                        bool(is_constrained(path)) if is_constrained else False
                    ),
                    "expensive": bool(net.nw_path_is_expensive(path)),
                    "cellular": bool(
                        net.nw_path_uses_interface_type(path, _NW_IFTYPE_CELLULAR)
                    ),
                    "wifi": bool(
                        net.nw_path_uses_interface_type(path, _NW_IFTYPE_WIFI)
                    ),
                    "wired": bool(
                        net.nw_path_uses_interface_type(path, _NW_IFTYPE_WIRED)
                    ),
                }
            except Exception:
                log(traceback.format_exc(), sys.stderr)
                return  # never let an exception escape into C

            _mac_state = state
            _mac_ready.set()

        callback = handler_type(_on_path_update)
        block = _mac_make_block(callback, libsystem)

        monitor = net.nw_path_monitor_create()
        if not monitor:
            log("Failed to create NWPathMonitor", sys.stderr)
            _mac_failed = True
            return False
        queue = libsystem.dispatch_queue_create(b"network_cost.monitor", None)
        net.nw_path_monitor_set_update_handler(monitor, ctypes.byref(block))
        net.nw_path_monitor_set_queue(monitor, queue)
        net.nw_path_monitor_start(monitor)
        _mac_keepalive.extend((net, libsystem, monitor, queue))

        _mac_started = True
        return True


def _mac_query(timeout: float = 1.0) -> Optional[NetworkCost]:
    if not _mac_start():
        return None
    if not _mac_ready.wait(timeout):
        return None  # first path update has not arrived yet
    state = _mac_state
    if state is None or not state["satisfied"]:
        return None  # offline: nothing to price
    return _mac_state_to_cost(state)


# ==========================================================================
# Public API
# ==========================================================================


def get_network_cost(timeout: float = 1.0) -> Optional[NetworkCost]:
    """Cost details for the current internet connection, or None when the state
    can't be determined (unsupported platform, offline, or API unavailable).

    `timeout` applies only on macOS, where the first reading arrives
    asynchronously; later calls return the cached value immediately.
    """
    if IS_WINDOWS:
        return _win_query()
    if IS_DARWIN:
        return _mac_query(timeout)
    return None


def should_limit_network_usage(default: bool = False) -> bool:
    """True when the connection is metered, in Low Data Mode, expensive,
    roaming, congested, or near a data cap. `default` is returned when the
    state can't be determined."""
    info = get_network_cost()
    return default if info is None else info.should_limit


if __name__ == "__main__":
    cost = get_network_cost()
    if cost is None:
        print("Network cost unknown (offline, or API unavailable).")
    else:
        print(cost)
