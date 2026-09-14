# 集合竞价榜单截取工具（jihejingjia）

到点自动抓取通达信服务端排序的榜单首页（含北交所全市场），补全细分行业等列后保存 CSV 与 SQLite，带 Web 管理界面。

## 启动

- **Windows**：双击 `start.bat`
- **macOS / Linux**：终端运行 `./run.sh`（首次需 `chmod +x run.sh`）

两个脚本都会自动：检测 Python 3.10+ → 创建本地 `.venv` → 安装 `eltdx[http]` → 启动服务。
然后浏览器打开 http://127.0.0.1:8765

手动方式（已有自己偏好的环境时）：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
.venv/bin/python -m pip install -r requirements.txt           # macOS / Linux
.venv/Scripts/python.exe app.py                               # Windows
.venv/bin/python app.py                                       # macOS / Linux
```

**跨平台说明**：eltdx 官方提供 manylinux x64/ARM64、macOS x64/ARM64 预编译 wheel（cp310-abi3），
无需 Rust 工具链；本工具自身为纯 Python（sqlite3/csv 等标准库 + FastAPI）。
细分行业名称表已内置（`data/tdxzs3_names.csv`）；若机器上装有 Windows 版通达信客户端，
会优先读其 `T0002/hq_cache/tdxzs3.cfg` 获取更新的行业名，没有则用内置表，Mac/Linux 完全可用。

## 功能

- **定时任务**：时刻精确到秒（默认 09:15 开盘换手 / 09:20 开盘金额 / 09:25 封单额，均降序），自由增删改
- **表头自定义**：点候选词增删，拖动调顺序；每页条数 1-80（通达信首屏为 60）
- **服务端排序**：走 7709 协议 0x054b，开盘换手用抓包逆向出的排序编号 0x001e
- **列补全**：细分行业（服务端 tdxhy.cfg + 本地 tdxzs3 行业名表）、开盘换手%（短线指标本地计算）
- **交易日判断**：基准上证指数日 K；非交易日自动跳过；错过触发超 120 秒不补跑
- **预热**：首任务前 90 秒（可配）建连并预下载统计资源，实测触发时快照仅 ~30ms
- **快照浏览**：网页按日期查看/下载 CSV（UTF-8-BOM，Excel 直开）
- **历史数据库**：每次截取自动双写 SQLite（`snapshots.db`），存全字段数值（不受表头配置影响）；网页按日期浏览，内置只读 SQL 查询框（仅 SELECT，自动 LIMIT 2000）；「导入历史CSV」把已有 CSV 文件回灌入库（自动去重、补行序排名）
- **手动运行**：每张任务卡「立即运行」随时补拍

## 文件

```
app.py        FastAPI 服务 + 调度启动
scheduler.py  后台调度线程
capture.py    采集管线 / 列提供者 / 行业映射
db.py         SQLite 持久化（snapshots 表 / 只读查询 / CSV 导入）
static/       前端单页
config.json   配置（界面里改，也可手编）
data/         行业名称静态表（源自通达信 tdxzs3.cfg，随客户端更新可重新生成）
downloads/    tdxhy.cfg 每日缓存
snapshots/    CSV 产出：日期/时刻_任务_排序_方向.csv
snapshots.db  SQLite 数据库（每次截取自动写入）
```

## 数据库

表 `snapshots`：每次截取每个名次一行，含交易日、时刻、任务名、排序字段方向、排名，
以及全字段数值（代码/名称/细分行业/涨幅/封单额亿/开盘金额亿/开盘换手%/现价/昨收/成交额亿/成交量万手/开盘涨幅/涨速/短换手）。
索引：交易日、代码、(交易日,时刻,任务,排名) 唯一。

外部工具直连：用 DB Browser for SQLite / DBeaver / Excel(数据→自SQLite) 打开 `snapshots.db` 即可。

常用 SQL：

```sql
-- 某日开盘换手榜前 10
SELECT rank, code, name, industry, open_turnover_pct, open_amount_yi
FROM snapshots WHERE trade_date='2026-09-15' AND task_name='开盘换手榜' AND rank<=10 ORDER BY rank;

-- 按行业聚合看分布
SELECT industry, COUNT(*) cnt, ROUND(AVG(change_pct),2) avg_pct
FROM snapshots WHERE task_name='开盘换手榜' GROUP BY industry ORDER BY cnt DESC;

-- 多日某股轨迹
SELECT trade_date, capture_time, task_name, rank, open_turnover_pct
FROM snapshots WHERE code='sz000978' ORDER BY trade_date, capture_time;
```

## 注意

- 调度在服务端运行，关浏览器不影响；但 Windows 睡眠会停表，交易时段保持唤醒
- 行业名称表更新：通达信客户端刷新 `T0002/hq_cache/tdxzs3.cfg` 后重跑生成脚本即可
- 仅限个人研究使用（eltdx Research-Only 许可）
