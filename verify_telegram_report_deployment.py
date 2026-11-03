"""Read-only check of carrier details using the deployed /babki generator."""
import json
import re
import subprocess
from pathlib import Path

from telegram_reporter import DETAIL_CATS, build_babki_report


def verify():
    assert {"Перевозчик", "Перевозчики"} <= DETAIL_CATS, "Carrier detail policy is outdated"
    lines = build_babki_report().splitlines()
    categories = 0
    for index, line in enumerate(lines):
        if not re.match(r"^  (Перевозчик|Перевозчики) — ", line):
            continue
        categories += 1
        assert index + 1 < len(lines), "Carrier category has no detail"
        detail = lines[index + 1]
        assert detail.startswith("    ") and " — " in detail, "Carrier category has no company breakdown"
    # Only structural evidence is logged; no financial rows or company names.
    pid = subprocess.check_output(
        ["systemctl", "show", "zavod", "-p", "MainPID", "--value"], text=True
    ).strip()
    running_directory = Path("/proc", pid, "cwd").resolve()
    deployed_directory = Path(__file__).resolve().parent
    logs = subprocess.check_output(
        ["journalctl", "-u", "zavod", "-n", "500", "--no-pager", "--output=cat"],
        text=True,
    ).splitlines()
    print(json.dumps({
        "babki_carrier_details_verified": True,
        "carrier_categories": categories,
        "service_uses_deployed_directory": running_directory == deployed_directory,
        "telegram_connectivity_errors": sum(
            any(term in line.lower() for term in ("network is unreachable", "connecterror", "connecttimeout"))
            for line in logs
        ),
        "telegram_poll_conflicts": sum("409" in line or "Conflict" in line for line in logs),
    }))


if __name__ == "__main__":
    verify()
