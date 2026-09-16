#!/usr/bin/env python3
"""竞价时段多节点对比探测 v2。

候选 = downloads/classic_hosts.txt 里的经典行情节点(前 15 个) + auto + 云镜像对照。
在 09:14:30 前后由 systemd-run 触发，每 55 秒对每个节点抓一次封单额榜前 3 行，
写入 auction_probe.log，用于确认哪些节点提供竞价排序数据。
"""
from __future__ import annotations

import datetime
import time as _time
from pathlib import Path

from eltdx import TdxClient
from eltdx.hosts import DEFAULT_HOSTS

CANDIDATES_FILE = Path("/opt/jihejingjia/downloads/classic_hosts.txt")
LOG = "/opt/jihejingjia/auction_probe.log"


def load_hosts() -> list[str]:
    classic: list[str] = []
    try:
        classic = [
            line.strip()
            for line in CANDIDATES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ][:15]
    except OSError:
        pass
    return classic + ["auto(云镜像)"] + list(DEFAULT_HOSTS)[:3]


def probe(host: str) -> str:
    target = None if host.startswith("auto") else host
    try:
        client = TdxClient(hosts=[target] if target else None, timeout=4)
        page = client.helpers.realtime_rank(category="沪深A股", sort_by="封单额", count=3, ascending=False)
        rows = list(page.rows)
        client.close()
        if not rows:
            return "EMPTY"
        first = rows[0]
        formed = sum(
            1 for r in rows
            if r.change_pct is not None and r.change_pct > -90 and r.last_price
        )
        return "%s %s pct=%.2f seal=%.3f亿 成形%d/3" % (
            first.full_code, first.name, first.change_pct, (first.seal_amount or 0) / 1e8, formed
        )
    except Exception as exc:
        return "ERR %s: %s" % (type(exc).__name__, str(exc)[:60])


def main() -> None:
    hosts = load_hosts()
    end = datetime.datetime.now().replace(hour=9, minute=26, second=0, microsecond=0)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("### 探针v2候选(%d): %s\n" % (len(hosts), ", ".join(hosts)))
    while datetime.datetime.now() < end:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("=== %s ===\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            for host in hosts:
                fh.write("  %-22s %s\n" % (host, probe(host)))
                fh.flush()
        _time.sleep(55)


if __name__ == "__main__":
    main()
