#!/usr/bin/env python3
"""竞价时段多节点对比探测。

在 09:14:30 前后由 systemd-run 触发，每分钟对“自动选择 + 前 12 个候选节点”
各抓一次封单额榜前 3 行，记录成形情况到 auction_probe.log，
用于确认哪些行情节点提供竞价排序数据（2026-09-15 排查用）。
"""
from __future__ import annotations

import datetime
import time as _time

from eltdx import TdxClient
from eltdx.hosts import DEFAULT_HOSTS

HOSTS = ["auto"] + list(DEFAULT_HOSTS)[:12]
LOG = "/opt/jihejingjia/auction_probe.log"


def probe(host: str) -> str:
    try:
        client = TdxClient(hosts=[host] if host != "auto" else None, timeout=4)
        page = client.helpers.realtime_rank(category="沪深A股", sort_by="封单额", count=3, ascending=False)
        rows = list(page.rows)
        client.close()
        if not rows:
            return "EMPTY"
        first = rows[0]
        return "%s %s pct=%.2f seal=%.3f亿" % (
            first.full_code, first.name, first.change_pct, (first.seal_amount or 0) / 1e8
        )
    except Exception as exc:
        return "ERR %s: %s" % (type(exc).__name__, str(exc)[:60])


def main() -> None:
    end = datetime.datetime.now().replace(hour=9, minute=26, second=0, microsecond=0)
    while datetime.datetime.now() < end:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("=== %s ===\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            for host in HOSTS:
                fh.write("  %-22s %s\n" % (host, probe(host)))
                fh.flush()
        _time.sleep(55)


if __name__ == "__main__":
    main()
