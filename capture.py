"""榜单截取核心：服务端排序首页抓取、列补全、CSV 落盘。"""

from __future__ import annotations

import csv
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DOWNLOAD_DIR = BASE_DIR / "downloads"
SNAPSHOT_DIR = BASE_DIR / "snapshots"

# 排序字段：值为传给 realtime_rank 的 sort_by（eltdx 别名或原始编号）
SORT_OPTIONS: dict[str, int | str] = {
    "开盘换手": 0x001E,
    "开盘金额": "开盘金额",
    "封单额": "封单额",
    "成交额": "成交额",
    "涨幅": "涨幅",
    "现价": "现价",
    "涨速": "涨速",
    "短换手": "短换手",
    "量涨速": "量涨速",
    "开盘抢筹": "开盘抢筹",
    "2分钟金额": "2分钟金额",
    "开盘涨幅": "开盘涨幅",
    "最高涨幅": "最高涨幅",
    "最低涨幅": "最低涨幅",
    "回撤": "回撤",
    "攻击": "攻击",
    "代码": "代码",
}

# 可选表头（顺序即默认顺序）；source: rank=榜单行本身, shortline=短线指标补全, industry=行业映射
COLUMN_SOURCES: dict[str, str] = {
    "代码": "rank",
    "名称": "rank",
    "细分行业": "industry",
    "涨幅%": "rank",
    "封单额(亿)": "rank",
    "开盘金额(亿)": "rank",
    "开盘换手%": "shortline",
    "现价": "rank",
    "昨收": "rank",
    "成交额(亿)": "rank",
    "成交量(万手)": "rank",
    "开盘涨幅%": "rank",
    "涨速%": "rank",
    "短换手%": "rank",
    "排名": "rank",
}

DEFAULT_COLUMNS = ["代码", "名称", "细分行业", "涨幅%", "封单额(亿)", "开盘金额(亿)", "开盘换手%"]

DEFAULT_CONFIG: dict = {
    "category": "沪深A股",
    "page_size": 60,
    "columns": DEFAULT_COLUMNS,
    "warmup_seconds": 90,
    "scheduler_enabled": True,
    "tasks": [
        {"name": "开盘换手榜", "time": "09:15:00", "sort_by": "开盘换手", "ascending": False},
        {"name": "开盘金额榜", "time": "09:20:00", "sort_by": "开盘金额", "ascending": False},
        {"name": "封单额榜", "time": "09:25:00", "sort_by": "封单额", "ascending": False},
    ],
}

TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")


def validate_config(cfg: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["config must be an object"]
    tasks = cfg.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return ["tasks 必须是非空数组"]
    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"tasks[{i}] 必须是对象")
            continue
        name = str(task.get("name") or "").strip()
        tm = str(task.get("time") or "").strip()
        parts = tm.split(":")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            tm = f"{int(parts[0]):02d}:{int(parts[1]):02d}:00"  # 部分浏览器 time 控件只回 HH:MM
        task["name"] = name
        task["time"] = tm
        if not name:
            errors.append(f"tasks[{i}].name 不能为空")
        if not TIME_RE.match(tm):
            errors.append(f"tasks[{i}].time 必须是 HH:MM:SS（当前: {tm!r}）")
        else:
            h, m, s = (int(x) for x in tm.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
                errors.append(f"tasks[{i}].time 超出范围: {tm}")
        if task.get("sort_by") not in SORT_OPTIONS:
            errors.append(f"tasks[{i}].sort_by 无效: {task.get('sort_by')!r}")
    columns = cfg.get("columns")
    if not isinstance(columns, list) or not columns:
        errors.append("columns 必须是非空数组")
    else:
        for col in columns:
            if col not in COLUMN_SOURCES:
                errors.append(f"未知表头: {col!r}")
    size = cfg.get("page_size", 60)
    if not isinstance(size, int) or not (1 <= size <= 80):
        errors.append("page_size 必须是 1-80 的整数")
    warmup = cfg.get("warmup_seconds", 90)
    if not isinstance(warmup, int) or not (0 <= warmup <= 3600):
        errors.append("warmup_seconds 必须是 0-3600 的整数")
    if cfg.get("category") != "沪深A股":
        errors.append("category 目前仅支持 沪深A股")
    return errors


def sanitize_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text.strip()) or "task"


class IndustryMap:
    """股票代码 -> 通达信细分行业名。tdxhy.cfg(服务端,每日) + tdxzs3 名称表(静态)。"""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._built_on: date | None = None
        self._lock = threading.Lock()

    def _load_name_table(self) -> dict[str, str]:
        names: dict[str, str] = {}
        sources = [
            Path(r"C:/new_tdx/T0002/hq_cache/tdxzs3.cfg"),
            DATA_DIR / "tdxzs3_names.csv",
        ]
        for src in sources:
            try:
                if src.suffix == ".csv":
                    with open(src, encoding="utf-8") as fh:
                        for code, name in csv.reader(fh):
                            names[code.upper()] = name
                    break
                for line in src.read_bytes().decode("gbk", errors="replace").splitlines():
                    parts = line.split("|")
                    if len(parts) >= 6 and parts[5].upper().startswith("T"):
                        names[parts[5].upper()] = parts[0]
                if names:
                    break
            except OSError:
                continue
        return names

    def _download_tdxhy(self, client) -> bytes:
        today = date.today()
        cached = DOWNLOAD_DIR / f"tdxhy_{today:%Y%m%d}.cfg"
        if cached.exists():
            return cached.read_bytes()
        data = client.resources.download_file("tdxhy.cfg")
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
        return data

    def build(self, client) -> int:
        with self._lock:
            if self._built_on == date.today() and self._map:
                return len(self._map)
            names = self._load_name_table()
            mapping: dict[str, str] = {}
            for line in self._download_tdxhy(client).decode("gbk", errors="replace").splitlines():
                parts = line.split("|")
                if len(parts) >= 3 and parts[2].upper().startswith("T"):
                    market = {"0": "sz", "1": "sh", "2": "bj"}.get(parts[0])
                    if market:
                        mapping[f"{market}{parts[1]}"] = names.get(parts[2].upper(), "")
            self._map = mapping
            self._built_on = date.today()
            return len(mapping)

    def get(self, full_code: str) -> str:
        return self._map.get(full_code, "")


INDUSTRY = IndustryMap()


def _fmt(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return ""


def _num(value, digits: int = 6):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _numeric_values(rank_row, shortline_row) -> dict:
    """DB 字段名 -> 数值，全字段始终计算，不受展示表头配置影响。"""
    raw = rank_row.raw
    pre = raw.pre_close_price or rank_row.pre_close
    try:
        open_change = (raw.open_price - pre) / pre * 100 if pre else None
    except TypeError:
        open_change = None
    return {
        "rank": rank_row.rank,
        "code": rank_row.full_code,
        "name": rank_row.name,
        "industry": None,  # 由调用方填充
        "change_pct": _num(rank_row.change_pct),
        "seal_amount_yi": _num(rank_row.seal_amount and rank_row.seal_amount / 1e8),
        "open_amount_yi": _num(raw.open_amount and raw.open_amount / 1e8),
        "open_turnover_pct": _num(getattr(shortline_row, "open_turnover_z", None)),
        "last_price": _num(rank_row.last_price),
        "pre_close": _num(rank_row.pre_close),
        "amount_yi": _num(rank_row.amount and rank_row.amount / 1e8),
        "volume_wan_hand": _num(rank_row.volume_hand and rank_row.volume_hand / 1e4),
        "open_change_pct": _num(open_change),
        "rise_speed": _num(raw.rise_speed),
        "short_turnover": _num(raw.short_turnover),
    }


def _row_values(rank_row, shortline_row) -> dict[str, str]:
    raw = rank_row.raw
    pre = raw.pre_close_price or rank_row.pre_close
    values = {
        "排名": str(rank_row.rank),
        "代码": rank_row.full_code,
        "名称": rank_row.name or "",
        "涨幅%": _fmt(rank_row.change_pct),
        "封单额(亿)": _fmt(rank_row.seal_amount and rank_row.seal_amount / 1e8, 3),
        "开盘金额(亿)": _fmt(raw.open_amount and raw.open_amount / 1e8, 3),
        "开盘换手%": _fmt(getattr(shortline_row, "open_turnover_z", None)),
        "现价": _fmt(rank_row.last_price),
        "昨收": _fmt(rank_row.pre_close),
        "成交额(亿)": _fmt(rank_row.amount and rank_row.amount / 1e8, 3),
        "成交量(万手)": _fmt(rank_row.volume_hand and rank_row.volume_hand / 1e4),
        "涨速%": _fmt(raw.rise_speed),
        "短换手%": _fmt(raw.short_turnover),
    }
    try:
        values["开盘涨幅%"] = _fmt((raw.open_price - pre) / pre * 100) if pre else ""
    except TypeError:
        values["开盘涨幅%"] = ""
    return values


class CaptureEngine:
    """持有 TdxClient 与各缓存，run_task 可被调度线程或 API 手动触发。"""

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.RLock()
        self._shortline_warmed_on: date | None = None
        self._workday_checked_on: date | None = None
        self._is_workday: bool | None = None

    @property
    def client(self):
        with self._lock:
            if self._client is None:
                from eltdx import TdxClient

                self._client = TdxClient(timeout=5)
            return self._client

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def today_is_workday(self) -> bool:
        with self._lock:
            if self._workday_checked_on != date.today():
                try:
                    self._is_workday = self.client.workdays.today_is_workday()
                except Exception:
                    self._is_workday = True  # 判断失败时按工作日处理，宁可多跑
                self._workday_checked_on = date.today()
            return bool(self._is_workday)

    def warmup(self) -> None:
        """触发 zhb.zip 统计资源下载与行业映射构建，避免触发时刻才下载。"""
        with self._lock:
            try:
                if self._shortline_warmed_on != date.today():
                    self.client.helpers.shortline_indicators(["sz000001"])
                    self._shortline_warmed_on = date.today()
            except Exception:
                pass
            try:
                INDUSTRY.build(self.client)
            except Exception:
                pass

    def snapshot_only(self, task: dict, cfg: dict) -> dict:
        """只抓榜单排名（时刻敏感段）；补列/落盘由 finalize_task 完成。

        同一秒挂多个任务时，调度器先对每个任务调用本方法把排名全部抓到手，
        补列的秒级请求链不会再把后面任务的抓取时刻推后。
        """
        started = datetime.now()
        with self._lock:
            client = self.client
            t0 = time.perf_counter()
            page = client.helpers.realtime_rank(
                category=cfg.get("category", "沪深A股"),
                sort_by=SORT_OPTIONS[task["sort_by"]],
                count=int(cfg.get("page_size", 60)),
                ascending=bool(task.get("ascending", False)),
            )
            rank_rows = list(page.rows)
            snapshot_ms = (time.perf_counter() - t0) * 1000
        return {"started": started, "rank_rows": rank_rows, "snapshot_ms": snapshot_ms}

    def finalize_task(self, task: dict, cfg: dict, snap: dict) -> dict:
        """对 snapshot_only 的结果补列并落盘（CSV + SQLite），返回摘要。"""
        started = snap["started"]
        rank_rows = snap["rank_rows"]
        columns = task.get("columns") or cfg.get("columns") or DEFAULT_COLUMNS
        with self._lock:
            client = self.client
            t1 = time.perf_counter()
            try:
                table = client.helpers.shortline_indicators([r.full_code for r in rank_rows])
                shortline_map = {row.full_code: row for row in table.rows}
            except Exception:
                shortline_map = {}
            enrich_ms = (time.perf_counter() - t1) * 1000

            try:
                INDUSTRY.build(client)
            except Exception:
                pass

        table_rows: list[dict[str, str]] = []
        numeric_rows: list[dict] = []
        for rank_row in rank_rows:
            values = _row_values(rank_row, shortline_map.get(rank_row.full_code))
            values["细分行业"] = INDUSTRY.get(rank_row.full_code)
            table_rows.append({col: values.get(col, "") for col in columns})
            numeric = _numeric_values(rank_row, shortline_map.get(rank_row.full_code))
            numeric["industry"] = values["细分行业"] or None
            numeric_rows.append(numeric)

        day_dir = SNAPSHOT_DIR / f"{started:%Y%m%d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        direction = "升序" if task.get("ascending") else "降序"
        fname = f"{started:%Y%m%d}_{started:%H%M%S}_{sanitize_filename(task.get('name', 'task'))}_{sanitize_filename(task['sort_by'])}_{direction}.csv"
        path = day_dir / fname
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows(table_rows)

        db_error = None
        try:
            db.insert_run(
                {
                    "trade_date": started.strftime("%Y-%m-%d"),
                    "capture_time": started.strftime("%H:%M:%S"),
                    "task_name": str(task.get("name", "task")),
                    "sort_by": str(task["sort_by"]),
                    "direction": "asc" if task.get("ascending") else "desc",
                },
                numeric_rows,
            )
        except Exception as exc:  # 库写失败不影响 CSV 产出
            db_error = f"{type(exc).__name__}: {exc}"

        return {
            "file": str(path),
            "file_name": fname,
            "columns": columns,
            "rows": table_rows,
            "count": len(table_rows),
            "captured_at": started.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "snapshot_ms": round(snap["snapshot_ms"]),
            "enrich_ms": round(enrich_ms),
            "total_ms": round(snap["snapshot_ms"] + (time.perf_counter() - t1) * 1000),
            "db_written": len(numeric_rows) if db_error is None else 0,
            "db_error": db_error,
        }

    def run_task(self, task: dict, cfg: dict) -> dict:
        """一步到位（手动「立即运行」用）；调度路径走 snapshot_only + finalize_task 两阶段。"""
        return self.finalize_task(task, cfg, self.snapshot_only(task, cfg))


ENGINE = CaptureEngine()
