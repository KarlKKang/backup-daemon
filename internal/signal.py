import threading
import signal
import ctypes
import sys
from .platform import IS_WINDOWS
from .log import log

__all__ = [
    "stop",
    "cleanup_done",
    "stop_reason",
    "StopRequested",
    "stop_checkpoint",
    "install_signal_handlers",
    "install_windows_console_handler",
    "install_windows_session_end_handler",
]

stop = threading.Event()  # set => please shut down
cleanup_done = threading.Event()  # set => cleanup finished
stop_reason = "<not set>"


def request_stop(reason: str) -> None:
    """Idempotent shutdown request. Safe to call from any thread."""
    if not stop.is_set():
        # print the reason in main program flow, to avoid re-entrancy issues
        global stop_reason
        stop_reason = reason
    stop.set()


class StopRequested(Exception):
    """Exception raised when a stop is requested for the backup process."""

    pass


def stop_checkpoint() -> None:
    if stop.is_set():
        raise StopRequested()


# --------------------------------------------------------------------------
# POSIX-style signals (also covers Ctrl+C on Windows)
# --------------------------------------------------------------------------


def _on_signal(signum, _frame) -> None:
    request_stop(signal.Signals(signum).name)


def install_signal_handlers() -> None:
    # SIGINT   - Ctrl+C, everywhere
    # SIGTERM  - kill / systemd / docker stop / launchd (POSIX only in practice)
    # SIGHUP   - terminal window closed or SSH session dropped (POSIX only)
    # SIGBREAK - Ctrl+Break (Windows only)
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue  # not defined on this platform
        try:
            signal.signal(sig, _on_signal)
        except ValueError:
            pass  # not settable here (e.g. not running in the main thread or unsupported signal on Windows)


# --------------------------------------------------------------------------
# Windows: console control events
# --------------------------------------------------------------------------
#
# This path reliably delivers CTRL_CLOSE_EVENT (the window's X button) only.
# CTRL_LOGOFF_EVENT / CTRL_SHUTDOWN_EVENT will NOT arrive here for the
# following reasons:
#
#   1. Those two are only sent to services. Interactive console programs are
#      already terminated by the time the system sends them.
#   2. Once a process loads user32.dll or gdi32.dll, Windows classifies it as
#      a GUI app and stops routing logoff/shutdown to its console handler at
#      all -- which is exactly what happens below, since the session-end
#      window pulls in user32.
#
# They are still listed so the code behaves correctly if it is ever hosted as
# a Windows service, where those events do fire.

_WIN_EVENTS = {
    0: "CTRL_C_EVENT",
    1: "CTRL_BREAK_EVENT",
    2: "CTRL_CLOSE_EVENT",  # window's X button
    5: "CTRL_LOGOFF_EVENT",
    6: "CTRL_SHUTDOWN_EVENT",  # machine shutting down
}

_console_handler = None  # module-level ref so it is not garbage collected


def _windows_cleanup_wait() -> None:
    if not cleanup_done.wait(4.0):
        # On Windows it's safe to do buffered print, since HandlerRoutine are run in a separate thread.
        log(
            "Cleanup is taking longer than expected, may be terminated by the OS.",
            file=sys.stderr,
        )
        cleanup_done.wait()


def install_windows_console_handler() -> None:
    if not IS_WINDOWS:
        return

    HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def handler(event: int) -> int:
        if event in (0, 1):
            # Let Python's own handler turn these into SIGINT / SIGBREAK.
            return 0
        request_stop(_WIN_EVENTS.get(event, f"CTRL_EVENT_{event}"))
        # Windows terminates the process the moment this returns, so block
        # here until the main thread has finished cleaning up.
        _windows_cleanup_wait()
        return 1  # handled

    global _console_handler
    _console_handler = HandlerRoutine(handler)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.SetConsoleCtrlHandler(_console_handler, True):
        raise ctypes.WinError(ctypes.get_last_error())


# --------------------------------------------------------------------------
# Windows: logoff / system shutdown, via a hidden top-level window
# --------------------------------------------------------------------------
#
# The supported notification for an interactive process is the
# WM_QUERYENDSESSION / WM_ENDSESSION pair, which requires a window. We create
# one and never show it.
#
# It must be a real top-level window, NOT a message-only (HWND_MESSAGE) one:
# message-only windows are treated as children of a hidden parent and are
# therefore skipped by broadcasts, including WM_QUERYENDSESSION.

_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_QUERYENDSESSION = 0x0011
_WM_ENDSESSION = 0x0016

_ENDSESSION_CLOSEAPP = 0x00000001
_ENDSESSION_CRITICAL = 0x40000000
_ENDSESSION_LOGOFF = 0x80000000

_ERROR_CLASS_ALREADY_EXISTS = 1410
_WINDOW_CLASS = "GracefulShutdownSessionEndSink"


def install_windows_session_end_handler(timeout: float = 5.0):
    """Start the hidden-window message pump. Returns True once it is live."""
    if not IS_WINDOWS:
        return
    ready = threading.Event()
    threading.Thread(
        target=_session_end_pump,
        args=(ready,),
        name="win32-session-end",
        daemon=True,
    ).start()
    if not ready.wait(timeout):
        raise TimeoutError("Windows session end handler did not become ready in time.")


def _session_end_pump(ready: threading.Event) -> None:
    import ctypes.wintypes as w  # importable on Windows only

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", w.HINSTANCE),
            ("hIcon", w.HICON),
            ("hCursor", w.HCURSOR),
            ("hbrBackground", w.HBRUSH),
            ("lpszMenuName", w.LPCWSTR),
            ("lpszClassName", w.LPCWSTR),
        ]

    # Explicit signatures: without these, 64-bit handles get truncated to int.
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [w.HWND, ctypes.c_uint, w.WPARAM, w.LPARAM]
    user32.RegisterClassW.restype = w.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.CreateWindowExW.restype = w.HWND
    user32.CreateWindowExW.argtypes = [
        w.DWORD,
        w.LPCWSTR,
        w.LPCWSTR,
        w.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        w.HWND,
        w.HMENU,
        w.HINSTANCE,
        w.LPVOID,
    ]
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(w.MSG),
        w.HWND,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    user32.DestroyWindow.argtypes = [w.HWND]
    kernel32.GetModuleHandleW.restype = w.HMODULE
    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]

    def wndproc(hwnd, msg, wparam, lparam):
        if msg == _WM_QUERYENDSESSION:
            kind = "logoff" if lparam & _ENDSESSION_LOGOFF else "shutdown"
            if lparam & _ENDSESSION_CRITICAL:
                kind += ", critical"
            # Windows wants an immediate answer here, so just wake the loop.
            # Cleanup overlaps the rest of the session-end handshake, which
            # buys us most of the grace period. If the shutdown is later
            # cancelled we exit anyway -- a better outcome than being killed
            # mid-write, and the script can simply be restarted.
            request_stop(f"WM_QUERYENDSESSION ({kind})")
            return 1  # TRUE: we consent to the session ending
        if msg == _WM_ENDSESSION:
            if wparam:  # the session really is ending
                # The system waits for us to return from this message,
                # subject to HungAppTimeout (~5 s).
                _windows_cleanup_wait()
            return 0
        if msg == _WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == _WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # Locals below stay alive because this frame lives as long as the thread;
    # that keeps the WNDPROC callback and class-name buffer from being freed.
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(wndproc)
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = _WINDOW_CLASS

    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.get_last_error()
        if err != _ERROR_CLASS_ALREADY_EXISTS:
            ready.set()
            raise ctypes.WinError(err)

    hwnd = user32.CreateWindowExW(
        0,
        _WINDOW_CLASS,
        "graceful-shutdown",
        0,
        0,
        0,
        0,
        0,
        None,
        None,
        wc.hInstance,
        None,
    )
    if not hwnd:
        ready.set()
        raise ctypes.WinError(ctypes.get_last_error())
    # Never call ShowWindow: the window stays invisible but is still top-level.

    # Be notified early in the shutdown sequence (0x100-0x3FF is the app
    # range; the default is 0x280 and higher values are notified sooner).
    kernel32.SetProcessShutdownParameters(0x3FF, 0)

    ready.set()

    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
