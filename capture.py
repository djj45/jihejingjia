"""榜单截取核心：服务端排序首页抓取、列补全、CSV 落盘。"""

from __future__ import annotations

import csv
import json
import random
import re
import threading
import time
import urllib.request
from datetime import date, datetime, time as dtime
from pathlib import Path
from types import SimpleNamespace

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

TRADE_CALENDAR_PATH = DOWNLOAD_DIR / "trade_calendar.csv"


class TradeCalendar:
    """深交所官网交易日历（含未来日期），缓存于 downloads/trade_calendar.csv。

    eltdx 的 workdays 以上证指数历史日 K 推导日历，开盘前的“今天”还没有 K 线，
    会被误判为非交易日并缓存一整天，导致当天任务全部不触发；
    这里改用官方日历判断，日历不可用时退化为周一~周五启发式（宁可多跑）。
    数据来源：http://www.szse.cn/api/report/exchange/onepersistenthour/monthList
    """

    def __init__(self) -> None:
        self._days: dict[date, bool] = {}
        self._lock = threading.Lock()
        self._next_fetch_ok = 0.0
        self._load()

    def _load(self) -> None:
        try:
            with open(TRADE_CALENDAR_PATH, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    try:
                        self._days[date.fromisoformat(row["jyrq"])] = row["jybz"] == "1"
                    except (KeyError, ValueError):
                        continue
        except OSError:
            pass

    def _save(self) -> None:
        try:
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            tmp = TRADE_CALENDAR_PATH.with_suffix(".tmp")
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["jyrq", "jybz"])
                writer.writeheader()
                for day in sorted(self._days):
                    writer.writerow({"jyrq": day.isoformat(), "jybz": "1" if self._days[day] else "0"})
            tmp.replace(TRADE_CALENDAR_PATH)
        except OSError:
            pass

    def _fetch_month(self, ym: str) -> bool:
        url = (
            "http://www.szse.cn/api/report/exchange/onepersistenthour/monthList"
            f"?month={ym}&random={random.random()}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            rows = (json.loads(resp.read().decode("utf-8")) or {}).get("data") or []
        for item in rows:
            try:
                self._days[date.fromisoformat(item["jyrq"])] = item["jybz"] == "1"
            except (KeyError, TypeError, ValueError):
                continue
        if rows:
            self._save()
            return True
        return False

    def is_trading_day(self, day: date) -> bool | None:
        """返回 True/False；日历覆盖不到该月且拉取失败时返回 None。"""
        with self._lock:
            covered = any(d.year == day.year and d.month == day.month for d in self._days)
            if not covered and time.monotonic() >= self._next_fetch_ok:
                self._next_fetch_ok = time.monotonic() + 300  # 失败也别打爆官网
                try:
                    covered = self._fetch_month(f"{day:%Y-%m}")
                except Exception:
                    covered = False
            if day in self._days:
                return self._days[day]
            return None


TRADE_CALENDAR = TradeCalendar()


def _raw_dict(raw) -> dict:
    """frozen dataclass 的 raw 转dict，便于 SimpleNamespace 改字段重建。"""
    return {f: getattr(raw, f) for f in raw.__dataclass_fields__} if hasattr(raw, "__dataclass_fields__") else dict(vars(raw))


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


def _fmt_amount_yi(value) -> str:
    """金额(亿)：保留到元级（6位小数）再去尾零。3位小数只有万元精度，
    集合竞价的小额开盘金额（如民德电子 9900 元=0.000099亿）会被吞成 0.000。"""
    if value is None:
        return ""
    try:
        return f"{value / 1e8:.6f}".rstrip("0").rstrip(".") or "0"
    except (TypeError, ValueError):
        return ""


def _limit_ratio_pct(full_code: str, name: str | None) -> float | None:
    """涨跌幅限制%：北交所30、科创/创业20、主板10（2026-07-06起主板ST亦为10%）；
    N/C开头（上市初期）无限制。"""
    upper_name = str(name or "").strip().upper()
    if upper_name.startswith(("N", "C")):
        return None
    symbol = full_code[2:]
    if full_code.startswith("bj"):
        return 30.0
    if full_code.startswith("sh688") or (full_code.startswith("sz") and symbol.startswith("30")):
        return 20.0
    return 10.0


def _auction_view(rank_row):
    """竞价时段行视图（09:15-09:25 未撮合）：last=0 但 bid1/ask1 镜像虚拟撮合价。

    返回 (虚拟价, 涨幅%, 封单额元或None)；非竞价行返回 None。
    封板判定用价格对比：bid1==涨停价 → 买队列 bid_vol1 即封单（09:18 实测
    中材科技 64.56=58.69*1.1，队列 1.13亿→09:25 定格 22.5亿）；跌停对称取卖队列。
    连续竞价时段不能用此判定（那时一侧为 0 才是封板，见 _seal_amount）。
    """
    raw = rank_row.raw
    pre = raw.pre_close_price or rank_row.pre_close
    if raw.last_price or not (raw.bid1 or raw.ask1) or not pre:
        return None
    price = raw.bid1 or raw.ask1
    pct = (price - pre) / pre * 100.0
    seal = None
    ratio = _limit_ratio_pct(rank_row.full_code, rank_row.name)
    if ratio:
        limit_up = round(pre * (1.0 + ratio / 100.0) + 1e-9, 2)
        limit_down = round(pre * (1.0 - ratio / 100.0) + 1e-9, 2)
        if abs(price - limit_up) <= 0.005 and raw.bid_vol1:
            seal = price * raw.bid_vol1 * 100.0  # 涨停封单（买队列）
        elif abs(price - limit_down) <= 0.005 and raw.ask_vol1:
            seal = price * raw.ask_vol1 * 100.0  # 跌停封单（卖队列）
    return price, pct, seal


def _seal_amount(rank_row) -> float | None:
    """封单额（元），与通达信客户端同口径：封板才有值，未封板返回 None（客户端显示 -）。

    0x054b 行情排序协议不下发封单字段，只有买卖一档价量；封板判定看一侧是否为空：
    涨停=卖一为 0（无人卖），封单=买一价×买一量；跌停=买一为 0（无人买），封单=卖一价×卖一量；
    两侧都有申报即未封板。竞价时段（未撮合，last=0）两侧镜像虚拟撮合价，改用价格对比
    涨停/跌停判定（见 _auction_view）。服务端排序键为带符号封单额
    （涨停正/跌停负/未封板沉底），故两个方向榜单前列都必是封板股。
    """
    auction = _auction_view(rank_row)
    if auction is not None:
        return auction[2]
    raw = rank_row.raw
    if not raw.bid1 and not raw.ask1:
        return None  # 停牌/节点无该股数据
    if not raw.ask1 and raw.bid1 and raw.bid_vol1:
        return raw.bid1 * raw.bid_vol1 * 100.0  # 涨停封单（买一）
    if not raw.bid1 and raw.ask1 and raw.ask_vol1:
        return raw.ask1 * raw.ask_vol1 * 100.0  # 跌停封单（卖一）
    return None


def _numeric_values(rank_row, open_turnover_pct) -> dict:
    """DB 字段名 -> 数值，全字段始终计算，不受展示表头配置影响。"""
    raw = rank_row.raw
    pre = raw.pre_close_price or rank_row.pre_close
    seal = _seal_amount(rank_row)
    auction = _auction_view(rank_row)
    last = auction[0] if auction else rank_row.last_price
    change_pct = auction[1] if auction else rank_row.change_pct
    if not auction and not rank_row.last_price:
        last = None  # 节点未回该股行情（如北交所竞价时段）：无价格不给误导值
        change_pct = None
    try:
        open_change = (raw.open_price - pre) / pre * 100 if pre else None
    except TypeError:
        open_change = None
    if auction:
        open_change = auction[1]  # 竞价未开盘，开盘涨幅=虚拟撮合涨幅
    try:
        open_change = (raw.open_price - pre) / pre * 100 if pre else None
    except TypeError:
        open_change = None
    return {
        "rank": rank_row.rank,
        "code": rank_row.full_code,
        "name": rank_row.name,
        "industry": None,  # 由调用方填充
        "change_pct": _num(change_pct),
        "seal_amount_yi": _num(seal and seal / 1e8),
        "open_amount_yi": _num(raw.open_amount and raw.open_amount / 1e8),
        "open_turnover_pct": _num(open_turnover_pct),
        "last_price": _num(last),
        "pre_close": _num(rank_row.pre_close),
        "amount_yi": _num(rank_row.amount and rank_row.amount / 1e8),
        "volume_wan_hand": _num(rank_row.volume_hand and rank_row.volume_hand / 1e4),
        "open_change_pct": _num(open_change),
        "rise_speed": _num(raw.rise_speed),
        "short_turnover": _num(raw.short_turnover),
    }


def _row_values(rank_row, open_turnover_pct) -> dict[str, str]:
    raw = rank_row.raw
    pre = raw.pre_close_price or rank_row.pre_close
    seal = _seal_amount(rank_row)
    auction = _auction_view(rank_row)
    last = auction[0] if auction else rank_row.last_price
    change_pct = auction[1] if auction else rank_row.change_pct
    if not auction and not rank_row.last_price:
        last = None  # 节点未回该股行情（如北交所竞价时段）：无价格不给误导值
        change_pct = None
    values = {
        "排名": str(rank_row.rank),
        "代码": rank_row.full_code,
        "名称": rank_row.name or "",
        "涨幅%": _fmt(change_pct),
        "封单额(亿)": _fmt_amount_yi(seal),
        "开盘金额(亿)": _fmt_amount_yi(raw.open_amount),
        "开盘换手%": _fmt(open_turnover_pct),
        "现价": _fmt(last),
        "昨收": _fmt(rank_row.pre_close),
        "成交额(亿)": _fmt_amount_yi(rank_row.amount),
        "成交量(万手)": _fmt(rank_row.volume_hand and rank_row.volume_hand / 1e4),
        "涨速%": _fmt(raw.rise_speed),
        "短换手%": _fmt(raw.short_turnover),
    }
    if auction:
        values["开盘涨幅%"] = _fmt(auction[1])
        return values
    try:
        values["开盘涨幅%"] = _fmt((raw.open_price - pre) / pre * 100) if pre else ""
    except TypeError:
        values["开盘涨幅%"] = ""
    return values


def _signed_seal(rank_row) -> float | None:
    """带符号封单额（服务端 0x054b 排序口径）：涨停正、跌停负、未封板 None。

    竞价时段服务端排序键与本地队列计算不同源（2026-09-21 实测 915 榜 27/59 逆序，
    0.537 亿被排在第 28 位），封单额任务需本地重排；此函数提供统一的重排键。"""
    raw = rank_row.raw
    if not raw.last_price:
        # 竞价/无数据行一律以 _auction_view 为准（封板才有键）。竞价头几秒部分股票
        # 行情簿单边（如 bid1=0、仅卖侧虚拟价），若落入下方连续时段零侧分支，会把
        # 虚拟价×匹配量冒充封单产生幻影键，未封板行被插进榜单中段（2026-09-22 915
        # 两榜实测：和顺石油 +1.96% 出现在跌停榜第 4 行）。
        auction = _auction_view(rank_row)
        if auction:
            price, _pct, seal = auction
            pre = raw.pre_close_price or rank_row.pre_close
            ratio = _limit_ratio_pct(rank_row.full_code, rank_row.name)
            if seal and ratio:
                limit_down = round(pre * (1.0 - ratio / 100.0) + 1e-9, 2)
                return -seal if abs(price - limit_down) <= 0.005 else seal
        return None
    if not raw.ask1 and raw.bid1 and raw.bid_vol1:
        return raw.bid1 * raw.bid_vol1 * 100.0  # 连续时段涨停封单
    if not raw.bid1 and raw.ask1 and raw.ask_vol1:
        return -(raw.ask1 * raw.ask_vol1 * 100.0)  # 连续时段跌停封单
    return None


def _rerank_by_seal(rank_rows, ascending: bool):
    """封单额榜本地重排：按带符号封单额排（降序=大买封在前，升序=大卖封在前），
    无封单行沉底；rank 重编为页内序号。"""
    def key(r):
        v = _signed_seal(r)
        if v is None:
            return (1, 0.0)
        return (0, v if ascending else -v)

    ordered = sorted(rank_rows, key=key)
    out = []
    for i, r in enumerate(ordered, 1):
        if r.rank != i:
            r = SimpleNamespace(rank=i, full_code=r.full_code, name=r.name, raw=r.raw,
                                pre_close=r.pre_close, last_price=r.last_price,
                                change_pct=r.change_pct, amount=r.amount,
                                volume_hand=r.volume_hand, seal_amount=None)
        out.append(r)
    return out


class CaptureEngine:
    """持有 TdxClient 与各缓存，run_task 可被调度线程或 API 手动触发。"""

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.RLock()
        self._stats = None
        self._stats_day: date | None = None
        self._workday_checked_on: date | None = None
        self._is_workday: bool | None = None
        self._current_host: str | None = None
        self._rotation_cursor = 0

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
                verdict = TRADE_CALENDAR.is_trading_day(date.today())
                if verdict is None:
                    verdict = date.today().weekday() < 5  # 日历不可用时按工作日，宁可多跑
                self._is_workday = verdict
                self._workday_checked_on = date.today()
            return bool(self._is_workday)

    def warmup(self) -> None:
        """预下载 zhb.zip 统计资源与行业映射，并试探一次榜单请求热身连接，避免触发时刻才建连。"""
        with self._lock:
            try:
                self._stats_for_today()
            except Exception:
                pass
            try:
                INDUSTRY.build(self.client)
            except Exception:
                pass
            try:
                self.client.helpers.realtime_rank(category="沪深A股", sort_by="涨幅", count=1, ascending=False)
            except Exception:
                pass

    def _stats_for_today(self):
        """当日 zhb.zip 统计资源（流通Z股本等），进程内缓存一天。"""
        if self._stats is None or self._stats_day != date.today():
            self._stats = self.client.resources.read_stats("zhb.zip")
            self._stats_day = date.today()
        return self._stats

    def _open_turnover_map(self, rank_rows) -> dict[str, float | None]:
        """开盘换手%（与 eltdx shortline open_turnover_z 同式，已实测一致）：
        开盘成交量(手) = 榜单行 open_amount / (open_price*100)；
        换手% = 成交量股数 / 流通Z股本 * 100。
        开盘金额/开盘价取榜单行自身（与排名同一时刻），流通股本取日级统计资源，
        避免 shortline_indicators 逐股日K/财务/全市场扫描的秒级请求链。
        """
        try:
            stats = self._stats_for_today()
        except Exception:
            return {r.full_code: None for r in rank_rows}
        result: dict[str, float | None] = {}
        for r in rank_rows:
            raw = r.raw
            value = None
            try:
                stat_row, _ = stats.row(raw.market_id, raw.code)
                ffs_10k = getattr(stat_row, "free_float_shares_10k", None) if stat_row else None
                if ffs_10k and raw.open_price:
                    open_volume_hand = raw.open_amount / (raw.open_price * 100.0)
                    value = round(open_volume_hand * 100.0 / (ffs_10k * 10000.0) * 100.0, 6)
            except Exception:
                value = None
            result[r.full_code] = value
        return result

    def snapshot_only(self, task: dict, cfg: dict) -> dict:
        """只抓榜单排名（时刻敏感段）；补列/落盘由 finalize_task 完成。

        同一秒挂多个任务时，调度器先对每个任务调用本方法把排名全部抓到手，
        补列的秒级请求链不会再把后面任务的抓取时刻推后。

        竞价时段(09:05-09:30)若榜单呈现“未成形”状态（前列普遍 -100%/无价格，
        即节点不提供竞价排序），自动轮换行情节点重抓，最多换 3 个；
        全部未成形则返回最后一次结果（与无轮换时的行为一致）。
        """
        started = datetime.now()
        in_auction = dtime(9, 5) <= started.time() <= dtime(9, 30)
        max_rotations = 3 if in_auction else 0
        pool = self._rotation_pool()
        fallback: dict | None = None
        for attempt in range(max_rotations + 1):
            try:
                with self._lock:
                    t0 = time.perf_counter()
                    page = self.client.helpers.realtime_rank(
                        category=cfg.get("category", "沪深A股"),
                        sort_by=SORT_OPTIONS[task["sort_by"]],
                        count=int(cfg.get("page_size", 60)),
                        ascending=bool(task.get("ascending", False)),
                    )
                    rank_rows = list(page.rows)
                    snapshot_ms = (time.perf_counter() - t0) * 1000
            except Exception:
                if attempt >= max_rotations:
                    raise
                rank_rows, snapshot_ms = None, 0.0
            if rank_rows is not None:
                snap = {"started": started, "rank_rows": rank_rows, "snapshot_ms": snapshot_ms, "rotations": attempt}
                if not in_auction or not self._looks_unformed(rank_rows):
                    return snap
                fallback = snap  # 记住最近一次结果作为兜底
            if attempt < max_rotations:
                self._rotation_cursor = (self._rotation_cursor + 1) % len(pool)
                self._swap_client(pool[self._rotation_cursor])
        return fallback

    def _swap_client(self, host: str | None) -> None:
        """丢弃当前连接，改用指定行情节点（None=自动选择）重建客户端。"""
        from eltdx import TdxClient

        with self._lock:
            old, self._client = self._client, None
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            self._client = TdxClient(hosts=[host], timeout=5) if host else TdxClient(timeout=5)
            self._current_host = host

    @staticmethod
    def _looks_unformed(rank_rows) -> bool:
        """竞价窗口判断榜单是否"未成形"：前 10 名里 ≥5 行缺数据。

        好行 = 有昨收 且 (有现价 或 有买一价)。竞价时段 last=0 属正常（未撮合），
        虚拟撮合价在 bid1 上；真正缺数据的行（节点不给竞价簿记）三者皆空。
        """
        top = rank_rows[:10]
        if not top:
            return False
        bad = sum(
            1 for r in top
            if not ((r.raw.pre_close_price or r.pre_close) and (r.last_price or r.raw.bid1))
        )
        return bad >= 5

    def _rotation_pool(self) -> tuple[str, ...]:
        """轮换节点池：经典行情节点优先（若有缓存文件），云镜像兜底。"""
        try:
            pinned = [
                line.strip()
                for line in (DOWNLOAD_DIR / "classic_hosts.txt").read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]
        except OSError:
            pinned = []
        if pinned:
            from eltdx.hosts import DEFAULT_HOSTS

            return tuple(pinned) + tuple(DEFAULT_HOSTS)
        from eltdx.hosts import DEFAULT_HOSTS

        return DEFAULT_HOSTS

    def _auction_seal_patch(self, rank_rows, started: datetime):
        """竞价行封单真源补全：0x056a 集合竞价过程快照。

        0x054b 竞价行的 bid_vol1/open_amount 是虚拟撮合的匹配量/匹配额（实测华锡有色
        09:15 匹配量 701手 vs 真实未匹配队列 205323手=10.26亿），不是封单；服务端排序
        键才是真实封单。对竞价封板候选行取 series 里 ≤抓取时刻的最近点，构造等效
        连续行情行（涨停: 买一=价×队列/卖侧清零；跌停对称），后续 _auction_view/
        零侧封单逻辑自动正确。窗口外不触发。"""
        if not (dtime(9, 14, 50) <= started.time() <= dtime(9, 25, 10)):
            return {}
        cutoff = (started - started.replace(hour=9, minute=15, second=0, microsecond=0)).seconds
        stale = []
        for r in rank_rows:
            raw = r.raw
            if raw.last_price or not (raw.bid1 or raw.ask1):
                continue  # 非竞价行（虚拟撮合价可能在任一侧，含头几秒的单边簿）
            pre = raw.pre_close_price or r.pre_close
            ratio = _limit_ratio_pct(r.full_code, r.name)
            if not pre or not ratio:
                continue
            up = round(pre * (1.0 + ratio / 100.0) + 1e-9, 2)
            down = round(pre * (1.0 - ratio / 100.0) + 1e-9, 2)
            vprice = raw.bid1 or raw.ask1
            if abs(vprice - up) <= 0.005 or abs(vprice - down) <= 0.005:
                stale.append(r)
        if not stale:
            return {}
        out: dict = {}
        for r in stale:
            try:
                series = self.client.auctions.series(r.full_code)
            except Exception:
                continue
            pts = [p for p in (series.points or []) if p.time_seconds - 33300 <= cutoff]
            if not pts:
                continue
            p = pts[-1]
            price = p.price_milli / 1000.0
            vol = p.unmatched_volume or 0
            if not price or not vol:
                continue
            raw = r.raw
            if (p.unmatched_direction_raw or 0) < 0:  # 卖侧队列：跌停
                new_raw = SimpleNamespace(**{**_raw_dict(raw), "bid1": 0.0, "bid_vol1": 0,
                                             "ask1": price, "ask_vol1": vol})
            else:  # 买侧队列：涨停
                new_raw = SimpleNamespace(**{**_raw_dict(raw), "bid1": price, "bid_vol1": vol,
                                             "ask1": 0.0, "ask_vol1": 0})
            out[r.full_code] = SimpleNamespace(
                rank=r.rank, full_code=r.full_code, name=r.name, raw=new_raw,
                pre_close=r.pre_close, last_price=r.last_price, change_pct=r.change_pct,
                amount=r.amount, volume_hand=r.volume_hand, seal_amount=None)
        return out

    def _augment_seal_rows(self, rank_rows, task: dict, cfg: dict, started: datetime):
        """竞价封单榜补漏：涨幅榜（价格源）发现封板股。

        服务端 0x054b 封单排序对个股有逐股放行滞后（2026-09-22 实测：综艺股份等
        09:15:08 才进入排序页，新华传媒队列 09:15:03 已 94.7亿 却直到 09:19:59
        才上榜），纯镜像排序页会整行漏股；桌面客户端正确是因为本地簿重排。这里按
        任务方向取涨幅榜头部（降序=涨停在前，升序=跌停在前），把不在封单页里的
        封板候选并入，封单真源仍由 _auction_seal_patch 取 0x056a。返回(行, 漏股代码)。"""
        if not (dtime(9, 14, 50) <= started.time() <= dtime(9, 25, 10)):
            return rank_rows, []
        try:
            page = self.client.helpers.realtime_rank(
                category=cfg.get("category", "沪深A股"),
                sort_by=SORT_OPTIONS["涨幅"],
                count=100,
                ascending=bool(task.get("ascending", False)),
            )
            extra = list(page.rows)
        except Exception:
            return rank_rows, []  # 补漏失败不影响主榜单
        known = {r.full_code for r in rank_rows}
        add = []
        for r in extra:
            if r.full_code in known:
                continue
            raw = r.raw
            if raw.last_price or not (raw.bid1 or raw.ask1):
                continue  # 非竞价行（虚拟撮合价可能在任一侧，含头几秒的单边簿）
            pre = raw.pre_close_price or r.pre_close
            ratio = _limit_ratio_pct(r.full_code, r.name)
            if not pre or not ratio:
                continue
            up = round(pre * (1.0 + ratio / 100.0) + 1e-9, 2)
            down = round(pre * (1.0 - ratio / 100.0) + 1e-9, 2)
            vprice = raw.bid1 or raw.ask1
            if abs(vprice - up) <= 0.005 or abs(vprice - down) <= 0.005:
                add.append(r)
                known.add(r.full_code)
        return rank_rows + add, [r.full_code for r in add]

    def _refresh_stale_rows(self, rank_rows):
        """0x054b 榜单页里无价格的行（典型：北交所股，节点首页不给行情簿）用 0x0547
        逐码刷新补齐，与桌面客户端显示同口径（桌面滚到可见窗口即显示涨幅）。
        返回 code -> 合成 rank_row；刷新不到的保留原行。"""
        stale = [r for r in rank_rows if not r.last_price and not r.raw.bid1]
        if not stale:
            return {}
        out: dict = {}
        for i in range(0, len(stale), 80):
            chunk = stale[i : i + 80]
            try:
                recs = self.client.quotes.refresh([r.full_code for r in chunk], cursors={})
                recs = recs.records if hasattr(recs, "records") else recs
                by = {rec.full_code: rec for rec in recs}
            except Exception:
                continue
            for r in chunk:
                rec = by.get(r.full_code)
                if rec is None or not rec.last_price:
                    continue
                buys = list(rec.buy_levels or [])
                sells = list(rec.sell_levels or [])
                pre = float(rec.last_close_price or 0) or r.pre_close
                last = float(rec.last_price)
                raw = SimpleNamespace(
                    pre_close_price=pre,
                    last_price=last,
                    open_price=float(rec.open_price or 0),
                    bid1=float(buys[0].price) if buys else 0.0,
                    bid_vol1=int(buys[0].volume) if buys else 0,
                    ask1=float(sells[0].price) if sells else 0.0,
                    ask_vol1=int(sells[0].volume) if sells else 0,
                    open_amount=float(rec.open_amount_yuan or 0),
                    amount=float(rec.amount or 0),
                    rise_speed=0,
                    short_turnover=0,
                )
                out[r.full_code] = SimpleNamespace(
                    rank=r.rank,
                    full_code=r.full_code,
                    name=r.name,
                    raw=raw,
                    pre_close=pre,
                    last_price=last,
                    change_pct=(last - pre) / pre * 100 if pre else None,
                    amount=raw.amount,
                    volume_hand=int(rec.total_hand or 0),
                    seal_amount=None,
                )
        return out

    def finalize_task(self, task: dict, cfg: dict, snap: dict) -> dict:
        """对 snapshot_only 的结果补列并落盘（CSV + SQLite），返回摘要。"""
        started = snap["started"]
        rank_rows = snap["rank_rows"]
        columns = task.get("columns") or cfg.get("columns") or DEFAULT_COLUMNS
        with self._lock:
            client = self.client
            t1 = time.perf_counter()
            augmented: list[str] = []
            if task.get("sort_by") == "封单额":
                rank_rows, augmented = self._augment_seal_rows(rank_rows, task, cfg, started)
            seal_patch = self._auction_seal_patch(rank_rows, started)
            if seal_patch:
                rank_rows = [seal_patch.get(r.full_code, r) for r in rank_rows]
            refreshed = self._refresh_stale_rows(rank_rows)
            if refreshed:
                rank_rows = [refreshed.get(r.full_code, r) for r in rank_rows]
            if task.get("sort_by") == "封单额":
                rank_rows = _rerank_by_seal(rank_rows, bool(task.get("ascending")))
                page_size = int(cfg.get("page_size", 60))
                if len(rank_rows) > page_size:  # 补漏并入后裁回页大小（尾部无封单行沉底）
                    rank_rows = rank_rows[:page_size]
            turnover_map = self._open_turnover_map(rank_rows)
            enrich_ms = (time.perf_counter() - t1) * 1000

            try:
                INDUSTRY.build(client)
            except Exception:
                pass

        table_rows: list[dict[str, str]] = []
        numeric_rows: list[dict] = []
        for rank_row in rank_rows:
            values = _row_values(rank_row, turnover_map.get(rank_row.full_code))
            values["细分行业"] = INDUSTRY.get(rank_row.full_code)
            table_rows.append({col: values.get(col, "") for col in columns})
            numeric = _numeric_values(rank_row, turnover_map.get(rank_row.full_code))
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
            "rotations": snap.get("rotations", 0),
            "augmented": augmented,
        }

    def run_task(self, task: dict, cfg: dict) -> dict:
        """一步到位（手动「立即运行」用）；调度路径走 snapshot_only + finalize_task 两阶段。"""
        return self.finalize_task(task, cfg, self.snapshot_only(task, cfg))


ENGINE = CaptureEngine()
