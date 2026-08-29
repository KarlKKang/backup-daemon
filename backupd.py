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
    if platform.system() != "Windows":
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
        raise subprocess.CalledProcessError(rc, command)


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
        if platform.system() == "Windows":
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
            if platform.system() == "Darwin":
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

    if platform.system() == "Darwin":
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


def main() -> int:
    log("Backup daemon started.")

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
            # The first run will happen after 1 minute to allow the system to set up properly after boot.
            stop.wait(timeout=60)
            stop_checkpoint()
            backup()
            stop_checkpoint()
            check()
        except StopRequested:
            break
        except:
            log(traceback.format_exc(), sys.stderr)

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
