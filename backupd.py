from __future__ import annotations
from typing import List, IO, Iterable
import os
import signal
import threading
import ctypes
import time
import subprocess
import traceback
from datetime import datetime
import sys
import platform
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
RUNTIME_DIR = os.path.join(SCRIPT_DIR, "runtime")
EXCLUDE_LIST = os.path.join(SCRIPT_DIR, "exclude.txt")
FILE_LIST = os.path.join(SCRIPT_DIR, "files.txt")
RESTIC_EXEC = os.environ.get("RESTIC_EXEC", None) or "restic"

DARWIN_SNAPSHOT_MOUNTPOINT = "/tmp/backupd_snapshot"

IS_WINDOWS = platform.system() == "Windows"
IS_DARWIN = platform.system() == "Darwin"


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


def windows_cleanup_wait() -> None:
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
        windows_cleanup_wait()
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

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016

ENDSESSION_CLOSEAPP = 0x00000001
ENDSESSION_CRITICAL = 0x40000000
ENDSESSION_LOGOFF = 0x80000000

ERROR_CLASS_ALREADY_EXISTS = 1410
WINDOW_CLASS = "GracefulShutdownSessionEndSink"


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
        if msg == WM_QUERYENDSESSION:
            kind = "logoff" if lparam & ENDSESSION_LOGOFF else "shutdown"
            if lparam & ENDSESSION_CRITICAL:
                kind += ", critical"
            # Windows wants an immediate answer here, so just wake the loop.
            # Cleanup overlaps the rest of the session-end handshake, which
            # buys us most of the grace period. If the shutdown is later
            # cancelled we exit anyway -- a better outcome than being killed
            # mid-write, and the script can simply be restarted.
            request_stop(f"WM_QUERYENDSESSION ({kind})")
            return 1  # TRUE: we consent to the session ending
        if msg == WM_ENDSESSION:
            if wparam:  # the session really is ending
                # The system waits for us to return from this message,
                # subject to HungAppTimeout (~5 s).
                windows_cleanup_wait()
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # Locals below stay alive because this frame lives as long as the thread;
    # that keeps the WNDPROC callback and class-name buffer from being freed.
    wc = WNDCLASSW()
    wc.lpfnWndProc = WNDPROC(wndproc)
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = WINDOW_CLASS

    if not user32.RegisterClassW(ctypes.byref(wc)):
        err = ctypes.get_last_error()
        if err != ERROR_CLASS_ALREADY_EXISTS:
            ready.set()
            raise ctypes.WinError(err)

    hwnd = user32.CreateWindowExW(
        0,
        WINDOW_CLASS,
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


# --------------------------------------------------------------------------
# is_process_running
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------


def log(message: str, file=None) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    for line in message.splitlines():
        try:
            print(f"[{timestamp}] {line}", file=file or sys.stdout, flush=True)
        except Exception:
            pass


class StopRequested(Exception):
    """Exception raised when a stop is requested for the backup process."""

    pass


def stop_checkpoint() -> None:
    if stop.is_set():
        raise StopRequested()


running_subprocess = None
subprocess_io_threads = []


def run_command(
    command: List[str], stdout: List[str] = None, stderr: List[str] = None
) -> None:
    kwargs = {}
    if IS_WINDOWS:
        # NOTE: a Windows process group is only a TARGETING mechanism for
        # GenerateConsoleCtrlEvent -- unlike a POSIX foreground process group,
        # it does not filter delivery. Keyboard events still go to every
        # process attached to the console. What this flag actually buys:
        #   * pid becomes a group id that CTRL_BREAK_EVENT can target
        #   * an implicit SetConsoleCtrlHandler(NULL, TRUE), whose only effect
        #     is that Ctrl+C handlers are not invoked. The event is still
        #     delivered; the child could re-enable it with (NULL, FALSE).
        # Ctrl+BREAK has no such opt-out and ALWAYS invokes handlers, and
        # CTRL_CLOSE_EVENT still fans out too. So a console-sharing child has
        # two unavoidable extra delivery paths.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # setsid(): new session, new process group, no controlling terminal.
        # Terminal-generated SIGINT/SIGQUIT/SIGHUP can no longer reach it.
        # Being a group leader also lets killpg() sweep up its descendants.
        kwargs["start_new_session"] = True

    global running_subprocess
    running_subprocess = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )

    print_lock = threading.Lock()

    def print_output(pipe, file, output_list):
        try:
            for line in iter(pipe.readline, ""):
                if output_list is None:
                    with print_lock:
                        log(line, file=file)
                else:
                    output_list.append(line)
        finally:
            pipe.close()

    global subprocess_io_threads
    subprocess_io_threads = [
        threading.Thread(
            target=print_output,
            args=(running_subprocess.stdout, sys.stdout, stdout),
        ),
        threading.Thread(
            target=print_output,
            args=(running_subprocess.stderr, sys.stderr, stderr),
        ),
    ]
    for t in subprocess_io_threads:
        t.start()

    while (rc := running_subprocess.poll()) is None:
        if stop.is_set():
            # this will send a second termination signal for broadcasted signals (mostly relevant on Windows)
            killpg(running_subprocess)
            raise StopRequested()
        stop.wait(0.1)
    running_subprocess = None
    for t in subprocess_io_threads:
        t.join()
    subprocess_io_threads = []
    if rc != 0:
        raise subprocess.CalledProcessError(rc, command)


def killpg(p: subprocess.Popen):
    try:
        if IS_WINDOWS:
            # Works because the child was created with CREATE_NEW_PROCESS_GROUP
            # (pid == process group id) and still shares our console.
            # CTRL_C_EVENT would silently no-op against a specific group.
            os.kill(p.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
    except OSError:
        pass


def get_list_item(lst, idx):
    try:
        return lst[idx]
    except IndexError:
        return None


def get_runtime_state(type: str) -> str:
    state_file = os.path.join(RUNTIME_DIR, type)
    if not os.path.exists(state_file):
        open(state_file, "a+").close()
    with open(state_file, "r") as f:
        return f.readline().strip()


def set_runtime_state(type: str, value: str) -> None:
    state_file = os.path.join(RUNTIME_DIR, type)
    with open(state_file, "w") as f:
        f.write(value)


# --------------------------------------------------------------------------
# APFS functions
# --------------------------------------------------------------------------


def apfs_snapshot() -> str:
    stdout = []
    run_command(["tmutil", "localsnapshot"], stdout=stdout)
    prefix = "Created local snapshot with date: "
    for line in stdout:
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise RuntimeError("Failed to read APFS snapshot from tmutil output")


def apfs_mount_snapshot(snapshot_date: str, mount_point: str) -> None:
    os.makedirs(mount_point, exist_ok=True)
    run_command(
        [
            "mount_apfs",
            "-o",
            "nobrowse,rdonly",
            "-s",
            "com.apple.TimeMachine.{}.local".format(snapshot_date),
            "/System/Volumes/Data",
            mount_point,
        ]
    )


def apfs_delete_snapshot(snapshot_date: str) -> None:
    run_command(
        [
            "tmutil",
            "deletelocalsnapshots",
            snapshot_date,
        ],
        stdout=[],
    )


# --------------------------------------------------------------------------
# Program code
# --------------------------------------------------------------------------


def backup():
    last_backup = get_runtime_state("backup")
    current_hour = datetime.now().strftime("%Y-%m-%d-%H")
    if current_hour == last_backup:
        return

    last_force_run = get_runtime_state("force_run")
    current_month = datetime.now().strftime("%Y-%m")
    force_run = current_month != last_force_run

    def write_file_list(
        src_list: Iterable[str], dest_file: IO, prefix: str = ""
    ) -> None:
        for line in src_list:
            line = line.strip()
            if line.startswith("/"):
                dest_file.write(f"{prefix}{line}\n")
            else:
                dest_file.write(f"{line}\n")

    def copy_file_list(src_path: str, dest_file: IO, prefix: str = "") -> None:
        with open(src_path, "r", encoding="utf-8") as src:
            write_file_list(src, dest_file, prefix)

    def build_backup_command(file_list: str, exclude_list: str) -> list:
        args = [
            RESTIC_EXEC,
            "backup",
            "-q",
            "--no-scan",
            "--exclude-caches",
            "--files-from",
            file_list,
        ]
        if IS_WINDOWS:
            args.append("--use-fs-snapshot")  # only supported on Windows
            args.append("--iexclude-file")
        else:
            args.append("--exclude-file")
        args.append(exclude_list)
        if force_run:
            args.append("--force")
        return args

    def run_backup_command(snapshot_dir: str = None) -> None:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=True, delete_on_close=False
        ) as tmp_file_list, tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=True, delete_on_close=False
        ) as tmp_exclude_list:
            copy_file_list(FILE_LIST, tmp_file_list, prefix=snapshot_dir or "")
            copy_file_list(EXCLUDE_LIST, tmp_exclude_list, prefix=snapshot_dir or "")
            if IS_DARWIN:
                timemachine_exclude = []
                run_command(
                    ["mdfind", "com_apple_backup_excludeItem = 'com.apple.backupd'"],
                    stdout=timemachine_exclude,
                )
                write_file_list(
                    timemachine_exclude, tmp_exclude_list, prefix=snapshot_dir or ""
                )
            tmp_file_list.close()
            tmp_exclude_list.close()
            args = build_backup_command(
                file_list=tmp_file_list.name,
                exclude_list=tmp_exclude_list.name,
            )
            run_command(args)

    if IS_DARWIN:
        snapshot_date = apfs_snapshot()
        try:
            apfs_mount_snapshot(snapshot_date, DARWIN_SNAPSHOT_MOUNTPOINT)
            run_backup_command(snapshot_dir=DARWIN_SNAPSHOT_MOUNTPOINT)
        finally:
            # No need to umount, delete will automatically clean up the mount
            apfs_delete_snapshot(snapshot_date)
    else:
        run_backup_command()

    set_runtime_state("backup", current_hour)
    if force_run:
        set_runtime_state("force_run", current_month)

    log(
        "Restic backup completed successfully{}.".format(
            " (forced run)" if force_run else ""
        )
    )

    stop_checkpoint()
    forget()


def forget():
    run_command(
        [
            RESTIC_EXEC,
            "forget",
            "-q",
            "--keep-within",
            "3d",
            "--keep-within-hourly",
            "7d",
            "--keep-within-daily",
            "1m",
            "--keep-within-weekly",
            "3m",
            "--keep-within-monthly",
            "1y",
            "--keep-within-yearly",
            "3y",
            "--prune",
        ]
    )


def check():
    last_checked = get_runtime_state("check")
    current_week = datetime.now().strftime("%G-%V")
    if current_week == last_checked:
        return

    check_subset = get_runtime_state("check_subset")
    check_subset = check_subset.split(" ")
    numerator = int(get_list_item(check_subset, 0) or 0)
    denominator = int(get_list_item(check_subset, 1) or 4)

    data_subset = f"{numerator % denominator + 1}/{denominator}"
    run_command(
        [
            RESTIC_EXEC,
            "check",
            "-q",
            "--read-data-subset",
            data_subset,
        ]
    )

    set_runtime_state("check", current_week)
    set_runtime_state("check_subset", f"{(numerator + 1) % denominator} {denominator}")

    log(f"Restic check completed without errors ({data_subset}).")


def lock_process(lock_file_path):
    try:
        with open(lock_file_path, "x") as lock_file:
            lock_file.write(str(os.getpid()))
    except FileExistsError:
        with open(lock_file_path, "r") as lock_file:
            running_pid = lock_file.read().strip()
        try:
            running = is_process_running(int(running_pid))
        except ValueError:
            running = False
            log(f"The lock file contains an invalid PID ({running_pid}).", sys.stderr)
        except PidCheckError as e:
            # Assume the process is running if we cannot check its status
            running = True
            log(
                f"Failed to check if the process with PID {running_pid} is running: {e}",
                sys.stderr,
            )
        if running:
            log("Another instance is already running.", sys.stderr)
            cleanup_done.set()
            sys.exit(1)
        log(
            "Lock file exists but no running process found, removing the lock file.",
            sys.stderr,
        )
        os.remove(lock_file_path)
        lock_process(lock_file_path)


def main():
    for f in (sys.stdout, sys.stderr):
        f.reconfigure(encoding="utf-8", errors="replace")

    install_signal_handlers()
    install_windows_console_handler()
    install_windows_session_end_handler()

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    lock_file_path = os.path.join(RUNTIME_DIR, "lock")

    try:
        lock_process(lock_file_path)
        log("Backup daemon started.")
        while True:
            # The first run will happen after 1 minute to allow the system to set up properly after boot.
            stop.wait(timeout=60)
            stop_checkpoint()
            backup()
            stop_checkpoint()
            check()
    except StopRequested:
        pass
    except:
        log(traceback.format_exc(), sys.stderr)
    finally:
        try:
            os.remove(lock_file_path)
        except:
            pass
        if running_subprocess is not None:
            running_subprocess.wait()
        for t in subprocess_io_threads:
            t.join()
        log(f"Backup daemon exiting. {stop_reason} received.")
        cleanup_done.set()

        # Let a blocked WM_ENDSESSION / console handler observe the flag
        # before the interpreter tears down and freezes daemon threads.
        time.sleep(0.05)


if __name__ == "__main__":
    main()
