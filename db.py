"""SQLite 持久化：每次截取写入 snapshots 表，支持按日浏览与只读 SQL 查询。"""

from __future__ import annotations

import csv
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "snapshots.db"
SNAPSHOT_DIR = BASE_DIR / "snapshots"
_lock = threading.Lock()

# 榜单列表头 -> 数据库字段（数值型存储，便于 SQL 聚合排序）
DB_FIELDS: dict[str, str] = {
    "排名": "rank",
    "代码": "code",
    "名称": "name",
    "细分行业": "industry",
    "涨幅%": "change_pct",
    "封单额(亿)": "seal_amount_yi",
    "开盘金额(亿)": "open_amount_yi",
    "开盘换手%": "open_turnover_pct",
    "现价": "last_price",
    "昨收": "pre_close",
    "成交额(亿)": "amount_yi",
    "成交量(万手)": "volume_wan_hand",
    "开盘涨幅%": "open_change_pct",
    "涨速%": "rise_speed",
    "短换手%": "short_turnover",
}
TEXT_FIELDS = {"rank", "code", "name", "industry"}  # rank 存整数，code/name/industry 文本

COLUMNS_SQL = ",\n  ".join(
    f"{field} {'INTEGER' if field == 'rank' else 'TEXT' if field in TEXT_FIELDS else 'REAL'}"
    for field in DB_FIELDS.values()
)
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trade_date TEXT NOT NULL,
  capture_time TEXT NOT NULL,
  task_name TEXT NOT NULL,
  sort_by TEXT NOT NULL,
  direction TEXT NOT NULL,
  created_at TEXT,
  {COLUMNS_SQL}
);
CREATE INDEX IF NOT EXISTS idx_snap_date ON snapshots(trade_date);
CREATE INDEX IF NOT EXISTS idx_snap_code ON snapshots(code);
CREATE UNIQUE INDEX IF NOT EXISTS idx_snap_run_rank ON snapshots(trade_date, capture_time, task_name, rank);
CREATE TABLE IF NOT EXISTS imported_files (
  file_name TEXT PRIMARY KEY
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_run(meta: dict, rows: list[dict]) -> int:
    """meta: trade_date/capture_time/task_name/sort_by/direction；rows: DB 字段名字典列表。"""
    fields = list(DB_FIELDS.values())
    with _lock, _connect() as conn:
        cur = conn.executemany(
            f"INSERT INTO snapshots (trade_date, capture_time, task_name, sort_by, direction, created_at, {', '.join(fields)})"
            f" VALUES (?, ?, ?, ?, ?, ?, {', '.join('?' * len(fields))})",
            [
                (
                    meta["trade_date"], meta["capture_time"], meta["task_name"],
                    meta["sort_by"], meta["direction"], datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    *(row.get(f) for f in fields),
                )
                for row in rows
            ],
        )
        return cur.lastrowid or 0


def run_exists(trade_date: str, capture_time: str, task_name: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM snapshots WHERE trade_date=? AND capture_time=? AND task_name=? LIMIT 1",
            (trade_date, capture_time, task_name),
        ).fetchone()
        return row is not None


def list_dates() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT trade_date, COUNT(*) runs, COUNT(DISTINCT capture_time) times,"
            " COUNT(DISTINCT code) stocks FROM snapshots GROUP BY trade_date ORDER BY trade_date DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_runs(day: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT trade_date, capture_time, task_name, sort_by, direction, COUNT(*) cnt,"
            " MIN(rank) r0, MAX(rank) r1"
            " FROM snapshots WHERE trade_date=? GROUP BY capture_time, task_name ORDER BY capture_time",
            (day,),
        ).fetchall()
        return [dict(r) for r in rows]


def fetch_rows(day: str, time_: str, task: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM snapshots WHERE trade_date=? AND capture_time=? AND task_name=? ORDER BY rank",
            (day, time_, task),
        ).fetchall()
        return [dict(r) for r in rows]


_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex)\b", re.I)


def run_sql(sql: str) -> dict:
    """只读 SELECT 查询，自动补 LIMIT，最多返回 2000 行。"""
    text = sql.strip().rstrip(";").strip()
    if not text.lower().startswith("select"):
        raise ValueError("仅允许 SELECT 查询")
    if _FORBIDDEN.search(text):
        raise ValueError("查询中含有禁止的关键字")
    if "limit" not in text.lower():
        text += " LIMIT 2000"
    with _connect() as conn:
        cur = conn.execute(text)
        columns = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchmany(2000)]
    return {"columns": columns, "rows": rows}


def import_csvs() -> dict:
    """把 snapshots/ 目录下尚未导入的 CSV 历史文件导入数据库。"""
    stats = {"imported_runs": 0, "imported_rows": 0, "skipped": 0, "files": []}
    if not SNAPSHOT_DIR.is_dir():
        return stats
    fname_re = re.compile(r"^(?:(\d{8})_)?(\d{6})_(.+?)_(.+?)_(升序|降序)\.csv$")
    for csv_path in sorted(SNAPSHOT_DIR.glob("*/*.csv")):
        if fname_re.match(csv_path.name) is None:
            continue
        with _lock, _connect() as conn:
            if conn.execute("SELECT 1 FROM imported_files WHERE file_name=?", (csv_path.name,)).fetchone():
                stats["skipped"] += 1
                continue
        day = m.group(1) if (m := fname_re.match(csv_path.name)) and m.group(1) else csv_path.parent.name
        hhmmss = m.group(2)
        trade_date = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        capture_time = f"{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}"
        task_name = m.group(3)
        if run_exists(trade_date, capture_time, task_name):
            with _lock, _connect() as conn:
                conn.execute("INSERT OR IGNORE INTO imported_files VALUES (?)", (csv_path.name,))
            stats["skipped"] += 1
            continue
        rows = []
        seq = 0
        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                seq += 1
                row = {}
                for col, field in DB_FIELDS.items():
                    value = (r.get(col) or "").strip()
                    if field in ("code", "name", "industry"):
                        row[field] = value or None
                    elif field == "rank":
                        row[field] = int(value) if value.isdigit() else seq  # CSV 未含排名列时按行序补
                    elif value == "":
                        row[field] = None
                    else:
                        try:
                            row[field] = round(float(value), 6)
                        except ValueError:
                            row[field] = None
                rows.append(row)
        if rows:
            insert_run(
                {"trade_date": trade_date, "capture_time": capture_time, "task_name": task_name,
                 "sort_by": m.group(4), "direction": "asc" if m.group(5) == "升序" else "desc"},
                rows,
            )
            stats["imported_runs"] += 1
            stats["imported_rows"] += len(rows)
            stats["files"].append(csv_path.name)
        with _lock, _connect() as conn:
            conn.execute("INSERT OR IGNORE INTO imported_files VALUES (?)", (csv_path.name,))
    return stats
