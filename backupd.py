from __future__ import annotations
from typing import List
import os
import signal
import threading
import ctypes
import time
import subprocess
import traceback
from datetime import datetime
import sys

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
RUNTIME_DIR = os.path.join(SCRIPT_DIR, "runtime")


stop = threading.Event()  # set => please shut down
cleanup_done = threading.Event()  # set => cleanup finished


def request_stop(reason: str) -> None:
    """Idempotent shutdown request. Safe to call from any thread."""
    if not stop.is_set():
        log(f"Shutdown requested: {reason}", file=sys.stderr)
    stop.set()


# --------------------------------------------------------------------------
# POSIX-style signals (also covers Ctrl+C on Windows)
# --------------------------------------------------------------------------


def _on_signal(signum, _frame) -> None:
    if stop.is_set():
        # Second signal: the user is impatient, or cleanup is wedged.
        log("Second signal received - exiting immediately.", file=sys.stderr)
        os._exit(128 + signum)
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
# Windows console control events
# --------------------------------------------------------------------------

_WIN_EVENTS = {
    0: "CTRL_C_EVENT",
    1: "CTRL_BREAK_EVENT",
    2: "CTRL_CLOSE_EVENT",  # window's X button
    5: "CTRL_LOGOFF_EVENT",
    6: "CTRL_SHUTDOWN_EVENT",  # machine shutting down
}

_console_handler = None  # module-level ref so it is not garbage collected


def install_windows_console_handler() -> None:
    if sys.platform != "win32":
        return

    HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def handler(event: int) -> int:
        if event in (0, 1):
            # Let Python's own handler turn these into SIGINT / SIGBREAK.
            return 0
        request_stop(_WIN_EVENTS.get(event, f"CTRL_EVENT_{event}"))
        # Windows terminates the process the moment this returns, so block
        # here until the main thread has finished cleaning up.
        if not cleanup_done.wait(4.0):
            log("Cleanup is taking longer than expected, may be terminated by the OS.")
            cleanup_done.wait()
        return 1  # handled

    global _console_handler
    _console_handler = HandlerRoutine(handler)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.SetConsoleCtrlHandler(_console_handler, True):
        raise ctypes.WinError(ctypes.get_last_error())


# --------------------------------------------------------------------------
# Helper functions
# --------------------------------------------------------------------------


def log(message: str, file=None) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    for line in message.splitlines():
        print(f"[{timestamp}] {line}", file=file or sys.stdout, flush=True)


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
    global running_subprocess
    running_subprocess = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
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
            daemon=True,
        ),
        threading.Thread(
            target=print_output,
            args=(running_subprocess.stderr, sys.stderr, stderr),
            daemon=True,
        ),
    ]
    for t in subprocess_io_threads:
        t.start()

    while (rc := running_subprocess.poll()) is None:
        stop.wait(0.1)
        stop_checkpoint()
    running_subprocess = None
    for t in subprocess_io_threads:
        t.join()
    subprocess_io_threads = []
    if rc != 0:
        raise subprocess.CalledProcessError(rc, running_subprocess.args)


def get_list_item(lst, idx):
    try:
        return lst[idx]
    except IndexError:
        return None


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
        ]
    )


# --------------------------------------------------------------------------
# Program code
# --------------------------------------------------------------------------


def backup():
    force_run_file = os.path.join(RUNTIME_DIR, "force_run")
    if not os.path.exists(force_run_file):
        open(force_run_file, "a+").close()
    with open(force_run_file, "r") as f:
        last_force_run = f.readline().strip()
    current_month = datetime.now().strftime("%Y-%m")
    force_run = current_month != last_force_run

    args = [
        "restic",
        "backup",
        "-q",
        "--use-fs-snapshot",
        "--iexclude-file",
        os.path.join(SCRIPT_DIR, "exclude.txt"),
    ]
    if force_run:
        args.append("--force")
    args.append("C:")
    args.append("D:")
    run_command(args)

    if force_run:
        with open(force_run_file, "w") as f:
            f.write(current_month)

    log(
        "Restic backup completed successfully{}.".format(
            " (forced run)" if force_run else ""
        )
    )


def forget():
    run_command(
        [
            "restic",
            "forget",
            "-q",
            "--keep-within",
            "1d",
            "--keep-within-hourly",
            "3d",
            "--keep-within-daily",
            "1m",
            "--keep-within-weekly",
            "3m",
            "--keep-within-monthly",
            "1y",
            "--prune",
        ]
    )


def check():
    check_file = os.path.join(RUNTIME_DIR, "check")
    if not os.path.exists(check_file):
        open(check_file, "a+").close()
    with open(check_file, "r") as f:
        last_checked = f.readline().strip()
    current_week = datetime.now().strftime("%G-%V")
    if current_week == last_checked:
        return

    check_subset_file = os.path.join(RUNTIME_DIR, "check_subset")
    with open(check_subset_file, "r") as f:
        check_subset = f.readline().strip()
        check_subset = check_subset.split(" ")
        numerator = int(get_list_item(check_subset, 0) or 0)
        denominator = int(get_list_item(check_subset, 1) or 4)

    data_subset = f"{numerator % denominator + 1}/{denominator}"
    run_command(
        [
            "restic",
            "check",
            "-q",
            "--read-data-subset",
            data_subset,
        ]
    )

    with open(check_file, "w") as f:
        f.write(current_week)

    with open(check_subset_file, "w") as f:
        f.write(f"{(numerator + 1) % denominator} {denominator}")

    log(f"Restic check completed without errors ({data_subset}).")


def main() -> int:
    install_signal_handlers()
    install_windows_console_handler()

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    lock_file_path = os.path.join(RUNTIME_DIR, "lock")
    try:
        with open(lock_file_path, "x") as lock_file:
            lock_file.write(str(os.getpid()))
    except FileExistsError:
        log("Another instance is already running.", sys.stderr)
        cleanup_done.set()
        return 1

    while True:
        try:
            # Run every hour.
            # The first run will happen after 1 hour to allow the system to set up properly after boot.
            stop.wait(timeout=3600)
            stop_checkpoint()
            backup()
            stop_checkpoint()
            forget()
            stop_checkpoint()
            check()
        except StopRequested:
            break
        except:
            print(traceback.format_exc())

    try:
        os.remove(lock_file_path)
    except:
        pass
    if running_subprocess is not None:
        running_subprocess.wait()
    for t in subprocess_io_threads:
        t.join()
    cleanup_done.set()

    return 0


if __name__ == "__main__":
    sys.exit(main())
