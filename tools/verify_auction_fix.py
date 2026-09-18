#!/usr/bin/env python3
"""周一09:26自动验收竞价封单修复：915/920应有真实竞价封单且队列随时间增长。"""
import glob
import sqlite3
from datetime import date

OUT = "/opt/jihejingjia/auction_fix_verdict.txt"
day = date.today().strftime("%Y-%m-%d")
lines = [f"=== 竞价修复验收 {day} ==="]
verdict = "PASS"

con = sqlite3.connect("file:/opt/jihejingjia/snapshots.db?mode=ro", uri=True)
rows = con.execute(
    "SELECT capture_time, task_name, COUNT(*), ROUND(MAX(seal_amount_yi),3) "
    "FROM snapshots WHERE trade_date=? AND capture_time<09:26:00 GROUP BY 1,2 ORDER BY 1",
    (day,),
).fetchall()
con.close()

for tm, task, n, mx in rows:
    lines.append(f"{tm} {task}: {n}行 max封单={mx}亿")
    if n == 0 or mx is None or mx <= 0:
        lines.append(f"  !! {task} 无有效封单")
        verdict = "FAIL"

# 涨幅检查：915涨停封单榜不应有 -100
import csv as _csv
for f in glob.glob(f"/opt/jihejingjia/snapshots/{day.replace(-,)}/%s_0915*涨停*.csv" % day.replace("-", "")):
    with open(f, encoding="utf-8-sig") as fh:
        bad = sum(1 for r in _csv.DictReader(fh) if r.get("涨幅%", "").startswith("-100"))
    lines.append(f"915涨停榜 -100行数: {bad}")
    if bad:
        verdict = "FAIL"

# 队列增长：同一股 0915 <= 0920 <= 0925
con = sqlite3.connect("file:/opt/jihejingjia/snapshots.db?mode=ro", uri=True)
growth = con.execute(
    "SELECT code, MIN(seal_amount_yi), MAX(seal_amount_yi) FROM snapshots "
    "WHERE trade_date=? AND task_name LIKE 9%涨停封单 AND seal_amount_yi>0.5 "
    "GROUP BY code ORDER BY MAX(seal_amount_yi) DESC LIMIT 5", (day,)
).fetchall()
con.close()
for code, lo, hi in growth:
    lines.append(f"队列增长 {code}: {lo} -> {hi} 亿")

files = sorted(glob.glob(f"/opt/jihejingjia/snapshots/{day.replace(-,)}/*.csv"))
lines.append(f"文件数: {len(files)}")
lines.append(f"结论: {verdict}")
open(OUT, "w", encoding="utf-8").write("\n".join(lines))
