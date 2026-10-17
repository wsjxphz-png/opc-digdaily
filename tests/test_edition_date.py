#!/usr/bin/env python3
"""逻辑出刊日回归测试（不碰网络）。

背景：workflow 名义 18:00 北京时间，但 GitHub 常延迟到次日凌晨。若直接用
datetime.now(CST).date()，凌晨 run 会把内容、push_marker、archive 都记到次日，
导致当天晚上的 run 误判「今天已推送」而整轮跳过。这里锁死 12:00 分界。
"""
import os
import sys
import tempfile
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("OPC_STORAGE_DIR", tempfile.mkdtemp(prefix="opc_edition_test_"))

from main import CST, edition_date, edition_day  # noqa: E402

CASES = [
    (datetime(2026, 10, 3, 0, 10, tzinfo=CST), "2026-10-02"),
    (datetime(2026, 10, 3, 3, 40, tzinfo=CST), "2026-10-02"),
    (datetime(2026, 10, 3, 11, 59, tzinfo=CST), "2026-10-02"),
    (datetime(2026, 10, 3, 12, 0, tzinfo=CST), "2026-10-03"),
    (datetime(2026, 10, 3, 22, 37, tzinfo=CST), "2026-10-03"),
    (datetime(2026, 1, 1, 3, 0, tzinfo=CST), "2025-12-31"),
]


def test_edition_date_cases():
    for now, expected in CASES:
        assert edition_date(now) == expected
        assert edition_day(now).isoformat() == expected


def test_env_override_freezes_edition_date():
    os.environ["OPC_EDITION_DATE"] = "2026-10-01"
    try:
        assert edition_date() == "2026-10-01"
    finally:
        os.environ.pop("OPC_EDITION_DATE", None)


def main():
    results = []
    for now, expected in CASES:
        got = edition_date(now)
        results.append((f"{now.isoformat()} → {expected}", got == expected, f"got {got}"))
        got_day = edition_day(now).isoformat()
        results.append((f"edition_day {now.isoformat()} → {expected}", got_day == expected,
                        f"got {got_day}"))
    os.environ["OPC_EDITION_DATE"] = "2026-10-01"
    try:
        got = edition_date()
        results.append(("env override → 2026-10-01", got == "2026-10-01", f"got {got}"))
    finally:
        os.environ.pop("OPC_EDITION_DATE", None)

    for name, ok, detail in results:
        print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{detail}]" if detail else ""))
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
