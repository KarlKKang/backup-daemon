import os
from datetime import datetime
from helper import run_command, log

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def backup(force_run: bool):
    args = [
        "restic",
        "backup",
        "-q",
        "--retry-lock",
        "1m",
        "--use-fs-snapshot",
        "--iexclude-file",
        os.path.join(SCRIPT_DIR, "exclude.txt"),
    ]
    if force_run:
        args.append("--force")
    args.append("C:")
    args.append("D:")
    run_command(args)


def cleanup():
    run_command(
        [
            "restic",
            "forget",
            "-q",
            "--retry-lock",
            "1m",
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


def main():
    force_run_file = os.path.join(SCRIPT_DIR, "force_run")
    if not os.path.exists(force_run_file):
        open(force_run_file, "a+").close()
    with open(force_run_file, "r") as f:
        last_force_run = f.readline().strip()
    current_month = datetime.now().strftime("%Y-%m")
    force_run = current_month != last_force_run
    backup(force_run)
    if force_run:
        with open(force_run_file, "w") as f:
            f.write(current_month)
    cleanup()
    log(
        "Restic backup completed successfully{}.".format(
            " (forced run)" if force_run else ""
        )
    )


if __name__ == "__main__":
    main()
