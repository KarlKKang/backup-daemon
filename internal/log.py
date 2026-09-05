import time
import sys


def log(message: str, file=None) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    for line in message.splitlines():
        try:
            print(f"[{timestamp}] {line}", file=file or sys.stdout, flush=True)
        except Exception:
            pass
