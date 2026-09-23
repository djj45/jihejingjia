"""拍卖竞价榜单截取工具 - Web 界面。启动: python app.py -> http://127.0.0.1:8765"""

from __future__ import annotations

import csv
import json
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import capture
import db
from capture import (
    COLUMN_SOURCES,
    DEFAULT_CONFIG,
    ENGINE,
    SNAPSHOT_DIR,
    SORT_OPTIONS,
    validate_config,
)
from scheduler import Scheduler

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
_config_lock = threading.Lock()


def load_config() -> dict:
    with _config_lock:
        if not CONFIG_PATH.exists():
            CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return json.loads(json.dumps(DEFAULT_CONFIG))
        merged = {**DEFAULT_CONFIG, **data}
        if not data.get("tasks"):
            merged["tasks"] = DEFAULT_CONFIG["tasks"]
        return merged


def save_config(cfg: dict) -> None:
    with _config_lock:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


SCHEDULER = Scheduler(ENGINE, load_config)


@asynccontextmanager
async def lifespan(_: FastAPI):
    SCHEDULER.start()
    yield
    SCHEDULER.stop()
    ENGINE.close()


app = FastAPI(title="竞价榜单截取", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class ConfigPayload(BaseModel):
    category: str
    page_size: int
    columns: list[str]
    warmup_seconds: int
    scheduler_enabled: bool
    tasks: list[dict]
    auto_image: bool = False
    image_side_by_side: bool = False


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "sort_options": list(SORT_OPTIONS.keys()),
        "available_columns": list(COLUMN_SOURCES.keys()),
        "column_sources": COLUMN_SOURCES,
        "today": date.today().strftime("%Y-%m-%d"),
    }


@app.get("/api/config")
def get_config():
    return load_config()


@app.put("/api/config")
def put_config(payload: ConfigPayload):
    cfg = payload.model_dump()
    errors = validate_config(cfg)
    if errors:
        raise HTTPException(status_code=422, detail="；".join(errors))
    save_config(cfg)
    return {"ok": True}


@app.get("/api/status")
def status():
    cfg = load_config()
    nxt = SCHEDULER.next_fire_datetime()
    return {
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scheduler_enabled": bool(cfg.get("scheduler_enabled", True)),
        "is_workday": getattr(ENGINE, "_is_workday", None),
        "next_task": SCHEDULER.next_task(),
        "next_fire_at": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None,
        "next_fire_ts": nxt.timestamp() if nxt else None,
        "history": list(SCHEDULER.history)[:60],
    }


@app.post("/api/scheduler/toggle")
def toggle_scheduler():
    cfg = load_config()
    cfg["scheduler_enabled"] = not cfg.get("scheduler_enabled", True)
    save_config(cfg)
    return {"ok": True, "scheduler_enabled": cfg["scheduler_enabled"]}


class RunPayload(BaseModel):
    task: dict | None = None
    index: int | None = None


@app.post("/api/run")
def run_now(payload: RunPayload):
    cfg = load_config()
    if payload.task is not None:
        task = payload.task
    elif payload.index is not None:
        tasks = cfg.get("tasks", [])
        if not (0 <= payload.index < len(tasks)):
            raise HTTPException(status_code=404, detail="task index out of range")
        task = tasks[payload.index]
    else:
        raise HTTPException(status_code=422, detail="需要 task 或 index")
    if task.get("sort_by") not in SORT_OPTIONS:
        raise HTTPException(status_code=422, detail=f"sort_by 无效: {task.get('sort_by')!r}")
    try:
        result = ENGINE.run_task(task, cfg)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    SCHEDULER._log("info", f"[{task.get('name', '手动')}] 手动运行完成 -> {result['file_name']}")
    return result


@app.get("/api/snapshots")
def list_snapshots(day: str | None = None):
    target = day or date.today().strftime("%Y%m%d")
    # 新结构 snapshots/年/月/日/{csv,png}；旧结构 snapshots/日 平铺（兼容未迁移的历史日）
    candidates = [
        SNAPSHOT_DIR / target[:4] / target[4:6] / target,
        SNAPSHOT_DIR / target,
    ]
    entries: list[Path] = []
    for d in candidates:
        if not d.is_dir():
            continue
        entries += list(d.glob("*.csv")) + list(d.glob("*.png"))
        for sub in ("csv", "png"):
            s = d / sub
            if s.is_dir():
                entries += list(s.glob("*.csv")) + list(s.glob("*.png"))
    files = sorted(
        ({"name": p.name, "path": str(p), "size": p.stat().st_size,
          "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%H:%M:%S"),
          "type": "png" if p.suffix.lower() == ".png" else "csv"}
         for p in entries),
        key=lambda item: item["name"],
        reverse=True,
    )
    return {"date": target, "files": files}


@app.get("/api/snapshots/content")
def snapshot_content(path: str):
    target = Path(path).resolve()
    if not str(target).startswith(str(SNAPSHOT_DIR.resolve())) or not target.is_file():
        raise HTTPException(status_code=403, detail="invalid path")
    with open(target, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return {"columns": [], "rows": []}
    return {"columns": rows[0], "rows": rows[1:]}


@app.get("/api/snapshots/download")
def snapshot_download(path: str):
    target = Path(path).resolve()
    if not str(target).startswith(str(SNAPSHOT_DIR.resolve())) or not target.is_file():
        raise HTTPException(status_code=403, detail="invalid path")
    media = "image/png" if target.suffix.lower() == ".png" else "text/csv"
    return FileResponse(target, filename=target.name, media_type=media)


# ---------- 历史数据库 ----------


@app.get("/api/db/dates")
def db_dates():
    return {"dates": db.list_dates()}


@app.get("/api/db/runs")
def db_runs(day: str):
    return {"runs": db.list_runs(day)}


@app.get("/api/db/rows")
def db_rows(day: str, time: str, task: str):
    rows = db.fetch_rows(day, time, task)
    if not rows:
        raise HTTPException(status_code=404, detail="no rows")
    columns = list(rows[0].keys())
    return {"columns": columns, "rows": [[("" if r.get(c) is None else r.get(c)) for c in columns] for r in rows]}


class SqlPayload(BaseModel):
    sql: str


@app.post("/api/db/query")
def db_query(payload: SqlPayload):
    try:
        return db.run_sql(payload.sql)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}")


@app.post("/api/db/import")
def db_import():
    return db.import_csvs()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
