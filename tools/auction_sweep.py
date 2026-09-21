#!/usr/bin/env python3
"""竞价秒级扫掠：09:15:00-09:15:10 每秒抓一次封单额榜前20（0x054b 原始页）。

观察三件事：1) 队列封单(bid1×bid_vol1)每秒如何增长（华锡有色类小值之谜）；
2) open_amount 字段竞价时是否恒等于队列封单；3) 服务端排序何时稳定。
写 /opt/jihejingjia/auction_sweep.log，不落快照不入库。
"""
from __future__ import annotations

import datetime
import sys
import time

sys.path.insert(0, "/opt/jihejingjia")
from eltdx import TdxClient

LOG = "/opt/jihejingjia/auction_sweep.log"
HOST = "116.205.171.132:7709"


def main() -> None:
    client = TdxClient(hosts=[HOST], timeout=4)
    target = datetime.datetime.now().replace(hour=9, minute=14, second=59, microsecond=900000)
    end = datetime.datetime.now().replace(hour=9, minute=15, second=10, microsecond=500000)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(f"\n##### {datetime.date.today()} 秒级扫掠 {HOST} #####\n")
        while datetime.datetime.now() < end:
            now = datetime.datetime.now()
            if now < target:
                time.sleep(0.02)
                continue
            t0 = time.perf_counter()
            try:
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
                    qseal = bid1 * bvol * 100 / 1e8 if bid1 and bvol else 0.0
                    fh.write(
                        f"  {r.full_code} pre={pre} bid1={bid1} bvol={bvol} "
                        f"ask1={ask1} avol={avol} 队列={qseal:.4f}亿 "
                        f"open_amt={(r.open_amount or 0) / 1e8:.4f}亿 last={r.last_price}\n")
                fh.flush()
            except Exception as exc:
                fh.write(f"  ERR {type(exc).__name__}: {exc}\n")
            target += datetime.timedelta(seconds=1)
    client.close()


if __name__ == "__main__":
    main()
