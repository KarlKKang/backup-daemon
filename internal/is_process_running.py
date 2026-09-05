import ctypes
import os
from .platform import IS_WINDOWS

__all__ = ["is_process_running", "PidCheckError"]


class PidCheckError(RuntimeError):
    """Raised when the state of a PID could not be determined."""


def _running_posix(pid: int) -> bool:
    """Linux/macOS: signal 0 runs the permission and existence checks only."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # ESRCH -- no such process
    except PermissionError:
        return True  # EPERM -- it exists, we are just not allowed to signal it
    except OSError as exc:
        raise PidCheckError(f"os.kill({pid}, 0) failed: {exc}") from exc
    return True


def _running_windows(pid: int) -> bool:
    """Windows: open a handle to the process and read its exit code."""
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5
    ERROR_INVALID_PARAMETER = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Declaring the signatures matters: without an explicit restype ctypes
    # assumes int and truncates the 64-bit handle.
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, wintypes.LPDWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == ERROR_INVALID_PARAMETER:
            return False  # no process carries that id
        if err == ERROR_ACCESS_DENIED:
            return True  # it exists, the handle just cannot be opened
        raise PidCheckError(f"OpenProcess failed with error {err}")

    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            err = ctypes.get_last_error()
            raise PidCheckError(f"GetExitCodeProcess failed with error {err}")
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_process_running(pid: int) -> bool:
    """Return True if a process with this PID exists right now.

    Raises ValueError for a non-positive PID and PidCheckError if the state
    genuinely could not be determined.
    """
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise ValueError("pid must be an int")
    if pid <= 0:
        # 0 and negatives are not real PIDs: on POSIX, kill(0, ...) targets the
        # caller's whole process group and kill(-n, ...) targets group n.
        raise ValueError(f"pid must be a positive integer, got {pid}")

    if IS_WINDOWS:
        return _running_windows(pid)
    return _running_posix(pid)
