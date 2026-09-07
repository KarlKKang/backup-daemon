import time
import sys
import threading

__all__ = ["log"]


_file_locks = {}


def log(message, file=None) -> None:
    """WARNING: This function should NOT be called in the signal handler (registered by `signal.signal`)."""
    file = file or sys.stdout
    lock = _file_locks.get(file)
    if lock is None:
        lock = threading.Lock()
        _file_locks[file] = lock
    if not isinstance(message, str):
        try:
            message = str(message)
        except Exception:
            message = "<unprintable object>"
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    lines = message.splitlines()
    with lock:
        for line in lines:
            try:
                print(f"[{timestamp}] {line}", file=file, flush=True)
            except Exception:
                pass
