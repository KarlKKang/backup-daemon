from . import subprocess
import os


def snapshot() -> str:
    stdout = []
    subprocess.run_command(["tmutil", "localsnapshot"], stdout=stdout)
    prefix = "Created local snapshot with date: "
    for line in stdout:
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise RuntimeError("Failed to read APFS snapshot from tmutil output")


def mount_snapshot(snapshot_date: str, mount_point: str) -> None:
    os.makedirs(mount_point, exist_ok=True)
    subprocess.run_command(
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


def delete_snapshot(snapshot_date: str) -> None:
    subprocess.run_command(
        [
            "tmutil",
            "deletelocalsnapshots",
            snapshot_date,
        ],
        stdout=[],
    )
