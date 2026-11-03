"""Read-only check of carrier details using the deployed /babki generator."""
import json
import re

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
    print(json.dumps({"babki_carrier_details_verified": True, "carrier_categories": categories}))


if __name__ == "__main__":
    verify()
