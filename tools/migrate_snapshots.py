#!/usr/bin/env python3
"""把旧快照结构 snapshots/YYYYMMDD/*.{csv,png} 迁移到 snapshots/YYYY/MM/YYYYMMDD/{csv,png}/。

幂等：已是新结构的目录自动跳过。用法：py tools/migrate_snapshots.py [--dry]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = BASE / "snapshots"


def migrate(dry: bool) -> None:
    moved = 0
    for day_dir in sorted(SNAPSHOT_DIR.iterdir()):
        if not day_dir.is_dir() or not (day_dir.name.isdigit() and len(day_dir.name) == 8):
            continue  # 年份目录（新结构）或无关文件
        day = day_dir.name
        new_day = SNAPSHOT_DIR / day[:4] / day[4:6] / day
        files = [p for p in day_dir.iterdir() if p.is_file()]
        if not files:
            print(f"跳过空目录 {day}")
            continue
        if dry:
            print(f"[dry] {day}: {len(files)} 个文件 -> {new_day}/{{csv,png}}")
            continue
        (new_day / "csv").mkdir(parents=True, exist_ok=True)
        (new_day / "png").mkdir(parents=True, exist_ok=True)
        for p in files:
            dest = new_day / ("png" if p.suffix.lower() == ".png" else "csv") / p.name
            shutil.move(str(p), str(dest))
            moved += 1
        if not any(day_dir.iterdir()):
            day_dir.rmdir()
        print(f"{day} -> {new_day}")
    print(f"完成，移动 {moved} 个文件")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印不移动")
    args = ap.parse_args()
    if SNAPSHOT_DIR.is_dir():
        migrate(args.dry)
    else:
        print(f"快照目录不存在: {SNAPSHOT_DIR}", file=sys.stderr)
