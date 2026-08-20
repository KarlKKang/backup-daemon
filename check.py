import os
from helper import run_command, log

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def main():
    check_subset_file = os.path.join(SCRIPT_DIR, "check_subset")
    with open(check_subset_file, "r+") as f:
        check_subset = f.readline().strip()
        check_subset = check_subset.split(" ")
        numerator = int(check_subset[0])
        denominator = int(check_subset[1])
        f.truncate(0)
        f.seek(0)
        f.write(f"{(numerator + 1) % denominator} {denominator}")
    data_subset = f"{numerator % denominator + 1}/{denominator}"
    run_command(
        [
            "restic",
            "check",
            "-q",
            "--retry-lock",
            "24h",
            "--read-data-subset",
            data_subset,
        ]
    )
    log(f"Restic check completed without errors ({data_subset}).")


if __name__ == "__main__":
    main()
