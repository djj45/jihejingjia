"""后台调度线程：按配置时刻精确触发榜单截取。"""

from __future__ import annotations

import threading
import time
import traceback
from collections import deque
from datetime import date, datetime, timedelta

from capture import CaptureEngine


class Scheduler:
    def __init__(self, engine: CaptureEngine, config_loader) -> None:
        self._engine = engine
        self._config_loader = config_loader  # () -> dict，每次触发前重读，改配置即时生效
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired: set[tuple[str, str]] = set()  # (task_signature, YYYYMMDD)
        self._warmed: date | None = None
        self._checked_workday: date | None = None
        self._workday = True
        self.history: deque[dict] = deque(maxlen=200)
        self._lock = threading.Lock()

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="auction-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _log(self, level: str, message: str, **extra) -> None:
        entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "message": message, **extra}
        with self._lock:
            self.history.appendleft(entry)

    # ---- 状态查询 ----

    def next_task(self) -> dict | None:
        cfg = self._config_loader()
        if not cfg.get("scheduler_enabled", True):
            return None
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        pending = []
        for task in cfg.get("tasks", []):
            h, m, s = (int(x) for x in task["time"].split(":"))
            due = now.replace(hour=h, minute=m, second=s, microsecond=0)
            sig = (f"{task['time']}|{task.get('name')}|{task.get('sort_by')}|{task.get('ascending')}", today)
            if sig in self._fired:
                continue
            if due < now and (now - due) > timedelta(seconds=120):
                continue  # 错过超过宽限期的不再补
            pending.append((due, task, sig))
        pending.sort(key=lambda item: item[0])
        return pending[0][1] if pending else None

    def next_fire_datetime(self) -> datetime | None:
        cfg = self._config_loader()
        if not cfg.get("scheduler_enabled", True):
            return None
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        best = None
        for task in cfg.get("tasks", []):
            h, m, s = (int(x) for x in task["time"].split(":"))
            due = now.replace(hour=h, minute=m, second=s, microsecond=0)
            sig = (f"{task['time']}|{task.get('name')}|{task.get('sort_by')}|{task.get('ascending')}", today)
            if sig in self._fired:
                continue
            if due < now and (now - due) > timedelta(seconds=120):
                continue
            if best is None or due < best:
                best = due
        return best

    # ---- 主循环 ----

    def _run(self) -> None:
        self._log("info", "调度器已启动")
        while not self._stop.wait(0.2):
            try:
                self._tick()
            except Exception as exc:  # 调度循环自身不能挂
                self._log("error", f"调度循环异常: {exc}\n{traceback.format_exc(limit=3)}")

    def _tick(self) -> None:
        cfg = self._config_loader()
        if not cfg.get("scheduler_enabled", True):
            return
        now = datetime.now()
        today = now.date()

        if self._checked_workday != today:
            self._workday = self._engine.today_is_workday()
            self._checked_workday = today
            self._log("info", f"今日{'是' if self._workday else '不是'}交易日")
        if not self._workday:
            return

        pending = []
        for index, task in enumerate(cfg.get("tasks", [])):
            h, m, s = (int(x) for x in task["time"].split(":"))
            due = now.replace(hour=h, minute=m, second=s, microsecond=0)
            sig = (f"{task['time']}|{task.get('name')}|{task.get('sort_by')}|{task.get('ascending')}", today.strftime("%Y%m%d"))
            if sig in self._fired:
                continue
            pending.append((due, task, sig, index))

        due_now = [item for item in pending if item[0] <= now]
        if not due_now:
            upcoming = [item for item in pending if item[0] > now]
            if upcoming:
                warmup_at = min(item[0] for item in upcoming) - timedelta(seconds=int(cfg.get("warmup_seconds", 90)))
                if now >= warmup_at and self._warmed != today:
                    self._log("info", "预热：建立连接并下载统计资源/行业映射")
                    self._engine.warmup()
                    self._warmed = today
            return

        pending_fin: list[tuple[dict, dict]] = []
        for due, task, sig, _index in sorted(due_now, key=lambda item: item[0]):  # 同刻时 dict 不可比，仅按时刻排序
            if (now - due) > timedelta(seconds=120):
                self._fired.add(sig)
                self._log("warn", f"[{task.get('name')}] 错过触发超过 120 秒，跳过（{task['time']}）")
                continue
            self._fired.add(sig)
            name = task.get("name", task["time"])
            self._log("info", f"[{name}] 触发（{task['time']}，{task['sort_by']}，{'升序' if task.get('ascending') else '降序'}）")
            try:
                pending_fin.append((task, self._engine.snapshot_only(task, cfg)))
            except Exception as exc:
                self._log("error", f"[{name}] 快照失败: {type(exc).__name__}: {exc}")
        # 同刻多任务：先把所有任务的榜单快照抓完（每个 ~几十 ms），
        # 再做补列/落盘，避免 1.4s 的补列请求链把后面任务的抓取时刻推后。
        for task, snap in pending_fin:
            self._finalize(task, cfg, snap)

    def _finalize(self, task: dict, cfg: dict, snap: dict) -> None:
        name = task.get("name", task["time"])
        started = time.perf_counter()
        try:
            result = self._engine.finalize_task(task, cfg, snap)
            rot = result.get("rotations") or 0
            aug = result.get("augmented") or []
            aug_text = ""
            if aug:
                codes = ",".join(aug[:4]) + ("…" if len(aug) > 4 else "")
                aug_text = f"，扫漏{len(aug)}只({codes})"
            if result.get("group_image"):
                aug_text += "，出图+并排"
            elif result.get("image"):
                aug_text += "，已出图"
            self._log(
                "success",
                f"[{name}] 完成：{result['count']} 行 -> {result['file_name']}"
                f"（快照 {result['snapshot_ms']}ms + 补列 {result['enrich_ms']}ms，全程 {round((time.perf_counter() - started) * 1000)}ms"
                f"{f'，换节点{rot}次' if rot else ''}{aug_text}）",
                file=result["file_name"],
            )
        except Exception as exc:
            self._log("error", f"[{name}] 失败: {exc}\n{traceback.format_exc(limit=3)}")
