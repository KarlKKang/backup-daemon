import subprocess
from typing import List
import sys
import time


def log(message: str, file=None) -> None:
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    for line in message.splitlines():
        print(f"[{timestamp}] {line}", file=file or sys.stdout)


def run_command(command: List[str]) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        if e.stdout:
            log("stdout:", file=sys.stderr)
            log(e.stdout.decode("utf-8"), file=sys.stderr)
        else:
            log("No stdout captured", file=sys.stderr)
        if e.stderr:
            log("stderr:", file=sys.stderr)
            log(e.stderr.decode("utf-8"), file=sys.stderr)
        else:
            log("No stderr captured", file=sys.stderr)
        sys.exit(1)
