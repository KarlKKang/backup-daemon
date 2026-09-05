from typing import IO, Iterable
import os
import time
import traceback
from datetime import datetime
import sys
import tempfile
from enum import Enum
from internal.log import log
from internal.is_process_running import is_process_running, PidCheckError
from internal.platform import IS_WINDOWS, IS_DARWIN
from internal import signal
from internal import subprocess
from internal import apfs

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
RUNTIME_DIR = os.path.join(SCRIPT_DIR, "runtime")
EXCLUDE_LIST = os.path.join(SCRIPT_DIR, "exclude.txt")
FILE_LIST = os.path.join(SCRIPT_DIR, "files.txt")
RESTIC_EXEC = os.environ.get("RESTIC_EXEC", None) or "restic"

DARWIN_SNAPSHOT_MOUNTPOINT = "/tmp/backupd_snapshot"


def get_list_item[T](lst: list[T], idx: int) -> T | None:
    try:
        return lst[idx]
    except IndexError:
        return None


class RuntimeState(Enum):
    BACKUP = "backup"
    FORCE_RUN = "force_run"
    CHECK = "check"
    CHECK_SUBSET = "check_subset"


def get_runtime_state(type: RuntimeState) -> str:
    state_file = os.path.join(RUNTIME_DIR, type.value)
    if not os.path.exists(state_file):
        open(state_file, "a+").close()
    with open(state_file, "r") as f:
        return f.readline().strip()


def set_runtime_state(type: RuntimeState, value: str) -> None:
    state_file = os.path.join(RUNTIME_DIR, type.value)
    with open(state_file, "w") as f:
        f.write(value)


def run_backup() -> bool:
    last_backup = get_runtime_state(RuntimeState.BACKUP)
    current_hour = datetime.now().strftime("%Y-%m-%d-%H")
    if current_hour == last_backup:
        return False

    last_force_run = get_runtime_state(RuntimeState.FORCE_RUN)
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

    def build_restic_command(file_list: str, exclude_list: str) -> list[str]:
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

    def run_restic_command(snapshot_dir: str = None) -> None:
        with (
            tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=True, delete_on_close=False
            ) as tmp_file_list,
            tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", delete=True, delete_on_close=False
            ) as tmp_exclude_list,
        ):
            copy_file_list(FILE_LIST, tmp_file_list, prefix=snapshot_dir or "")
            copy_file_list(EXCLUDE_LIST, tmp_exclude_list, prefix=snapshot_dir or "")
            if IS_DARWIN:
                timemachine_exclude = []
                subprocess.run_command(
                    ["mdfind", "com_apple_backup_excludeItem = 'com.apple.backupd'"],
                    stdout=timemachine_exclude,
                )
                write_file_list(
                    timemachine_exclude, tmp_exclude_list, prefix=snapshot_dir or ""
                )
            tmp_file_list.close()
            tmp_exclude_list.close()
            args = build_restic_command(
                file_list=tmp_file_list.name,
                exclude_list=tmp_exclude_list.name,
            )
            subprocess.run_command(args)

    if IS_DARWIN:
        snapshot_date = apfs.snapshot()
        try:
            apfs.mount_snapshot(snapshot_date, DARWIN_SNAPSHOT_MOUNTPOINT)
            run_restic_command(snapshot_dir=DARWIN_SNAPSHOT_MOUNTPOINT)
        finally:
            # No need to umount, delete will automatically clean up the mount
            apfs.delete_snapshot(snapshot_date)
    else:
        run_restic_command()

    set_runtime_state(RuntimeState.BACKUP, current_hour)
    if force_run:
        set_runtime_state(RuntimeState.FORCE_RUN, current_month)

    log(
        "Restic backup completed successfully{}.".format(
            " (forced run)" if force_run else ""
        )
    )
    return True


def run_forget() -> None:
    subprocess.run_command(
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


def run_check() -> bool:
    last_checked = get_runtime_state(RuntimeState.CHECK)
    current_week = datetime.now().strftime("%G-%V")
    if current_week == last_checked:
        return False

    check_subset = get_runtime_state(RuntimeState.CHECK_SUBSET)
    check_subset = check_subset.split(" ")
    numerator = int(get_list_item(check_subset, 0) or 0)
    denominator = int(get_list_item(check_subset, 1) or 4)

    data_subset = f"{numerator % denominator + 1}/{denominator}"
    subprocess.run_command(
        [
            RESTIC_EXEC,
            "check",
            "-q",
            "--read-data-subset",
            data_subset,
        ]
    )

    set_runtime_state(RuntimeState.CHECK, current_week)
    set_runtime_state(
        RuntimeState.CHECK_SUBSET, f"{(numerator + 1) % denominator} {denominator}"
    )

    log(f"Restic check completed without errors ({data_subset}).")
    return True


def lock_process(lock_file_path: str) -> None:
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
            signal.cleanup_done.set()
            sys.exit(1)
        log(
            "Lock file exists but no running process found, removing the lock file.",
            sys.stderr,
        )
        os.remove(lock_file_path)
        lock_process(lock_file_path)


def main() -> None:
    for f in (sys.stdout, sys.stderr):
        f.reconfigure(encoding="utf-8", errors="replace")

    signal.install_signal_handlers()
    signal.install_windows_console_handler()
    signal.install_windows_session_end_handler()

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    lock_file_path = os.path.join(RUNTIME_DIR, "lock")

    try:
        lock_process(lock_file_path)
        log("Backup daemon started.")
        while True:
            # The first run will happen after 1 minute to allow the system to set up properly after boot.
            signal.stop.wait(timeout=60)
            signal.stop_checkpoint()
            if run_backup():
                signal.stop_checkpoint()
                run_forget()
            signal.stop_checkpoint()
            run_check()
    except signal.StopRequested:
        pass
    except:
        log(traceback.format_exc(), sys.stderr)
    finally:
        try:
            os.remove(lock_file_path)
        except:
            pass
        subprocess.uninterruptible_wait()
        log(f"Backup daemon exiting. {signal.stop_reason} received.")
        signal.cleanup_done.set()

        # Let a blocked WM_ENDSESSION / console handler observe the flag
        # before the interpreter tears down and freezes daemon threads.
        time.sleep(0.05)


if __name__ == "__main__":
    main()
