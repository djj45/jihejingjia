#!/usr/bin/env python3
"""竞价时段协议探测 v3：找出竞价(09:15-09:25)能返回成形行情的请求。

三条腿（都用 eltdx 原生，NativeEngine 直连指定节点）：
  A. 0x054b 服务端排序：sort=封单额(0x1c) 降序 vs sort=代码(0x00) 第0页
  B. 0x0547 逐码增量刷新：昨日封板股样本（竞价时应显示虚拟价+买卖档）
  C. 0x053e 旧版批量行情：同样本
每轮把各腿前几行的 价格/昨收/额/买一卖一 写日志，成形=价格>0 且昨收>0。
"""
from __future__ import annotations

import datetime
import sys
import time

sys.path.insert(0, "/opt/jihejingjia")
from eltdx import TdxClient

LOG = "/opt/jihejingjia/auction_probe3.log"
NODES = ["116.205.171.132:7709", "111.230.186.52:7709", "180.153.18.170:7709"]
SAMPLE = ["sh600105", "sh600206", "sz002281", "sz000001"]  # 永鼎/有研新材/光迅/平安


def formed_ratio(recs) -> tuple[int, int, float]:
    """成形=现价>0。竞价时段节点半成形的特征就是 last=0（对应涨幅-100%）。"""
    total = ok = 0
    for r in recs:
        total += 1
        if getattr(r, "last_price", 0):
            ok += 1
    return ok, total, (ok / total if total else 0.0)


def fmt(r):
    last = getattr(r, "last_price", None)
    pre = getattr(r, "pre_close_price", None) or getattr(r, "pre_close", None)
    amt = getattr(r, "amount", None)
    bid1 = getattr(r, "bid1", None)
    bvol1 = getattr(r, "bid_vol1", None)
    ask1 = getattr(r, "ask1", None)
    avol1 = getattr(r, "ask_vol1", None)
    code = getattr(r, "full_code", None) or getattr(r, "code", "?")
    amt_yi = f"{amt/1e8:.3f}亿" if isinstance(amt, (int, float)) and amt else amt
    return f"{code} last={last} pre={pre} amt={amt_yi} bid1={bid1}x{bvol1} ask1={ask1}x{avol1}"


def probe_legs(client, fh, tag: str) -> None:
    # A1: 0x054b sort=封单额 降序
    try:
        page = client.quotes.list_by_category("沪深A股", sort_by=0x001C, start=0, count=20, ascending=False)
        ok, total, ratio = formed_ratio(page.records)
        fh.write(f"  [{tag}] A1 054b封单额排序: 成形{ok}/{total} | {fmt(page.records[0]) if page.records else '无'}\n")
    except Exception as exc:
        fh.write(f"  [{tag}] A1 ERR {type(exc).__name__}: {str(exc)[:80]}\n")
    # A2: 0x054b sort=代码 第0页
    try:
        page = client.quotes.list_by_category("沪深A股", sort_by=0x0000, start=0, count=20, ascending=False)
        ok, total, ratio = formed_ratio(page.records)
        head = " | ".join(fmt(r) for r in page.records[:2])
        fh.write(f"  [{tag}] A2 054b代码排序: 成形{ok}/{total} | {head}\n")
    except Exception as exc:
        fh.write(f"  [{tag}] A2 ERR {type(exc).__name__}: {str(exc)[:80]}\n")
    # B: 0x0547 逐码刷新
    try:
        recs = client.quotes.refresh(SAMPLE, cursors={})
        recs = recs.records if hasattr(recs, "records") else recs
        ok, total, _ = formed_ratio(recs)
        head = " | ".join(fmt(r) for r in list(recs)[:2])
        fh.write(f"  [{tag}] B  0547逐码刷新: 成形{ok}/{total} | {head}\n")
    except Exception as exc:
        fh.write(f"  [{tag}] B  ERR {type(exc).__name__}: {str(exc)[:80]}\n")
    # C: 0x053e 旧版批量行情
    try:
        recs = client.quotes.legacy(SAMPLE)
        recs = recs.records if hasattr(recs, "records") else recs
        ok, total, _ = formed_ratio(recs)
        head = " | ".join(fmt(r) for r in list(recs)[:2])
        fh.write(f"  [{tag}] C  053e旧版行情: 成形{ok}/{total} | {head}\n")
    except Exception as exc:
        fh.write(f"  [{tag}] C  ERR {type(exc).__name__}: {str(exc)[:80]}\n")


def main() -> None:
    end = datetime.datetime.now().replace(hour=9, minute=26, second=0, microsecond=0)
    while datetime.datetime.now() < end:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"=== {datetime.date.today()} {stamp} ===\n")
            for host in NODES:
                try:
                    client = TdxClient(hosts=[host], timeout=4)
                    probe_legs(client, fh, host.split(":")[0])
                    client.close()
                except Exception as exc:
                    fh.write(f"  [{host}] CONN ERR {type(exc).__name__}: {str(exc)[:60]}\n")
            fh.flush()
        time.sleep(45)


if __name__ == "__main__":
    main()
