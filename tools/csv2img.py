"""把快照 CSV 渲染成通达信配色 PNG（黑底/黄代码名称/红绿涨跌封单/蓝开盘金额/白其他）。

用法：py tools/csv2img.py snapshots/20260916/*.csv [--font-size 16] [--scale 2] [--out 目录]
输出：CSV 同名 .png（默认写在 CSV 旁边）。

- --scale 渲染放大倍数（默认 2），高分辨率放大看不糊
- 代码列自动去掉 sh/sz/bj 前缀
- (亿) 结尾的金额列转中文单位：1亿 / 5300万 / 5300，表头同步去掉 (亿) 后缀
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (0, 0, 0)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 160, 255)
WHITE = (255, 255, 255)
GRAY = (128, 128, 128)
LINE = (56, 56, 56)

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]

NUMERIC_COLS = {
    "涨幅%", "封单额(亿)", "开盘金额(亿)", "开盘换手%", "现价", "昨收",
    "成交额(亿)", "成交量(万手)", "开盘涨幅%", "涨速%", "短换手%", "排名",
}

NAME_RE = re.compile(r"^(\d{8})_(\d{6})_(.+?)_(.+?)_(升序|降序)$")


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def parse_num(text: str) -> float | None:
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def sign_color(value: float | None):
    if value is None or value == 0:
        return WHITE
    return RED if value > 0 else GREEN


def strip_prefix(code: str) -> str:
    return code[2:] if code.lower().startswith(("sh", "sz", "bj")) else code


def fmt_amount(text: str) -> str:
    """亿单位小数 -> 通达信式有效数字 + 中文单位：1~99亿 3 位（3.14亿/35.8亿），
    100亿及以上 4 位（199.9亿），万/元级 4 位（4000万/300.5万/30.27万/5300）。"""
    v = parse_num(text)
    if v is None:
        return text
    yuan = v * 1e8
    if yuan >= 1e8:
        num, unit = yuan / 1e8, "亿"
        sig = 3 if num < 100 else 4
    elif yuan >= 1e4:
        num, unit, sig = yuan / 1e4, "万", 4
    else:
        num, unit, sig = yuan, "", 4
    s = f"{num:.{sig}g}"
    if "e" in s or "E" in s:  # 大数防科学计数
        s = f"{num:.0f}"
    elif "." in s:
        s = s.rstrip("0").rstrip(".")
    return s + unit


def display_header(col: str) -> str:
    return col[:-3] if col.endswith("(亿)") else col


def cell_text(col: str, text: str) -> str:
    if col == "代码":
        return strip_prefix(text)
    if not text and col in NUMERIC_COLS:
        return "-"  # 无数据，通达信同款
    if text and col.endswith("(亿)"):
        return fmt_amount(text)
    return text


def cell_color(col: str, text: str, change_pct: float | None):
    if col in ("代码", "名称"):
        return YELLOW
    if col == "涨幅%":
        return sign_color(parse_num(text))
    if col == "封单额(亿)":
        if not text:
            return GRAY
        return sign_color(change_pct)  # 涨停封单红 / 跌停封单绿
    if col == "开盘金额(亿)":
        return BLUE
    if not text and col in NUMERIC_COLS:
        return GRAY
    return WHITE


def title_of(path: Path) -> tuple[str, str | None]:
    """文件名 -> (任务名, 排序高亮字段)。匹配 日期_时刻_任务_排序_方向 命名。"""
    m = NAME_RE.match(path.stem)
    if not m:
        return path.stem, None
    day, tm, task, sort_field, _direction = m.groups()
    title = f"{day[:4]}-{day[4:6]}-{day[6:]} {tm[:2]}:{tm[2:4]}:{tm[4:6]}  {task}"
    return title, sort_field


def render(csv_path: Path, out_dir: Path | None, font_size: int, scale: int) -> Path:
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r]
    if len(rows) < 2:
        raise SystemExit(f"{csv_path}: 空表格")
    columns, data = rows[0], rows[1:]

    data_font = load_font(font_size * scale)
    title_font = load_font((font_size + 5) * scale)
    ascent, descent = data_font.getmetrics()
    row_h = ascent + descent + 8 * scale
    pad_x, margin = 9 * scale, 14 * scale

    # 先取每行涨幅，封单额配色要用
    change_col = columns.index("涨幅%") if "涨幅%" in columns else -1
    changes = [parse_num(r[change_col]) if change_col >= 0 else None for r in data]

    tmp_img = Image.new("RGB", (8, 8))
    tmp = ImageDraw.Draw(tmp_img)

    def width_of(text: str, font) -> int:
        return max(8, int(tmp.textlength(text, font=font) + 0.5))

    title, sort_field = title_of(csv_path)
    headers = [display_header(c) for c in columns]
    col_w = [width_of(h, data_font) for h in headers]  # 表头本身参与列宽，防止溢出到邻列
    for row in data:
        for i, (col, raw) in enumerate(zip(columns, row)):
            col_w[i] = max(col_w[i], width_of(cell_text(col, raw), data_font))
    table_w = sum(w + pad_x * 2 for w in col_w)
    img_w = max(table_w + margin * 2, width_of(title, title_font) + margin * 2)
    title_h = row_h + 14 * scale
    header_h = row_h + 6 * scale
    img_h = margin + title_h + header_h + row_h * len(data) + margin

    img = Image.new("RGB", (img_w, img_h), BG)
    d = ImageDraw.Draw(img)

    d.text((margin, margin), title, font=title_font, fill=WHITE)
    y = margin + title_h
    d.line([(margin - 4 * scale, y - 3 * scale), (img_w - margin + 4 * scale, y - 3 * scale)], fill=LINE, width=scale)

    x = margin
    for i, (col, head) in enumerate(zip(columns, headers)):
        # 被排序字段的表头高亮成黄色，模仿通达信排序列；数值列表头右对齐与数据对齐
        head_fill = YELLOW if sort_field and sort_field in head else WHITE
        if col in NUMERIC_COLS:
            w = tmp.textlength(head, font=data_font)
            d.text((x + pad_x + col_w[i] - w, y), head, font=data_font, fill=head_fill)
        else:
            d.text((x + pad_x, y), head, font=data_font, fill=head_fill)
        x += col_w[i] + pad_x * 2
    y += row_h
    d.line([(margin - 4 * scale, y - 3 * scale), (img_w - margin + 4 * scale, y - 3 * scale)], fill=LINE, width=scale)

    for r, row in enumerate(data):
        x = margin
        change = changes[r]
        for i, (col, raw) in enumerate(zip(columns, row)):
            text = cell_text(col, raw)
            fill = cell_color(col, raw, change)
            w = tmp.textlength(text, font=data_font)
            cx = x + pad_x if col not in NUMERIC_COLS else x + pad_x + col_w[i] - w
            d.text((cx, y), text, font=data_font, fill=fill)
            x += col_w[i] + pad_x * 2
        y += row_h

    out_path = (out_dir or csv_path.parent) / (csv_path.stem + ".png")
    img.save(out_path)
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="快照 CSV -> 通达信配色 PNG")
    ap.add_argument("csv", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="输出目录（默认 CSV 旁）")
    ap.add_argument("--font-size", type=int, default=16, help="基准字号（实际再乘 scale）")
    ap.add_argument("--scale", type=int, default=2, help="渲染放大倍数，默认 2")
    args = ap.parse_args()
    for path in args.csv:
        if not path.is_file():
            print(f"跳过（不存在）: {path}", file=sys.stderr)
            continue
        print(render(path, args.out, args.font_size, args.scale))


if __name__ == "__main__":
    main()
