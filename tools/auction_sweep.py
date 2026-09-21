#!/usr/bin/env python3
"""竞价秒级扫掠：09:15:00-10 与 09:20:00-10 两个窗口每秒抓一次封单额榜前20（0x054b 原始页）。

观察：1) 行字段(bid_vol1/open_amount=匹配量口径)每秒演化；2) 与 0x056a series 的
unmatched 真实队列的对照可事后用 auctions.series 全天历史回放。写
/opt/jihejingjia/auction_sweep.log，不落快照不入库。由 systemd 定时器 09:14:57 触发。
"""
from __future__ import annotations

import datetime
import sys
import time

sys.path.insert(0, "/opt/jihejingjia")
from eltdx import TdxClient

LOG = "/opt/jihejingjia/auction_sweep.log"
HOST = "116.205.171.132:7709"
WINDOWS = [  # (窗口起点, 窗口终点)
    (datetime.time(9, 14, 59, 900000), datetime.time(9, 15, 10, 500000)),
    (datetime.time(9, 19, 59, 900000), datetime.time(9, 20, 10, 500000)),
]


def sweep_once(client: TdxClient, fh) -> None:
    t0 = time.perf_counter()
    page = client.quotes.list_by_category(
        "沪深A股", sort_by=0x001C, start=0, count=20, ascending=False)
    ms = (time.perf_counter() - t0) * 1000
    stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    fh.write(f"=== {stamp} ({ms:.0f}ms) ===\n")
    for r in page.records:
        pre = r.pre_close_price or 0
        bid1 = r.bid1 or 0
        bvol = r.bid_vol1 or 0
        ask1 = r.ask1 or 0
        avol = r.ask_vol1 or 0
        match_amt = bid1 * bvol * 100 / 1e8 if bid1 and bvol else 0.0
        fh.write(
            f"  {r.full_code} pre={pre} bid1={bid1} bvol={bvol} "
            f"ask1={ask1} avol={avol} 匹配额={match_amt:.4f}亿 "
            f"open_amt={(r.open_amount or 0) / 1e8:.4f}亿 last={r.last_price}\n")
    fh.flush()


def main() -> None:
    client = TdxClient(hosts=[HOST], timeout=4)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"\n##### {datetime.date.today()} 双窗口秒级扫掠 {HOST} #####\n")
        now = datetime.datetime.now()
        for start_t, end_t in WINDOWS:
            target = now.replace(hour=start_t.hour, minute=start_t.minute,
                                 second=start_t.second, microsecond=start_t.microsecond)
            end = now.replace(hour=end_t.hour, minute=end_t.minute,
                              second=end_t.second, microsecond=end_t.microsecond)
            while datetime.datetime.now() < end:
                if datetime.datetime.now() < target:
                    time.sleep(0.02)
                    continue
                try:
                    sweep_once(client, fh)
                except Exception as exc:
                    fh.write(f"  ERR {type(exc).__name__}: {exc}\n")
                target += datetime.timedelta(seconds=1)
            time.sleep(0.5)
    client.close()


if __name__ == "__main__":
    main()
