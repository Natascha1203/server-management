#!/usr/bin/env python3
"""导出 SQLite 中的上下架请求为 xlsx。

列格式与「导入上下架表」模板一致（18 列），末尾多一列「审核状态」：
  待审核(机位IP分配) / 待审核(具体实施) / 已驳回 / 已通过

用法示例：
  python scripts/export_rack_requests.py                          # 导出全部状态
  python scripts/export_rack_requests.py --status 待审核           # 只导待审核
  python scripts/export_rack_requests.py --status 已驳回 已通过    # 多选
  python scripts/export_rack_requests.py --status rejected --out D:/r.xlsx
  python scripts/export_rack_requests.py --data-dir ../data       # 指定数据目录

数据目录定位优先级：--data-dir > --ini 指定的配置 > 项目根 app.ini > 项目根 data/
导出的文件多出的「审核状态」列在回传导入时会被自动忽略（只识别 18 个标准列）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 优先复用应用里的表头定义，保证与导入逻辑一致；仅在脱离项目独立运行时用内置兜底
try:
    from app.core import RACK_REQUEST_HEADERS
except Exception:
    RACK_REQUEST_HEADERS = [
        "序列号", "品牌", "型号", "CPU型号", "网卡", "服务器成本(万)",
        "防火墙白名单", "资源申请编号", "是否液冷", "是否上架", "是否重装系统",
        "初始密码", "上下架需求", "备注", "客户id", "客户名称", "所在机房",
    ]

STATUS_ALIASES = {
    "pending": "pending", "待审核": "pending",
    "rejected": "rejected", "已驳回": "rejected",
    "approved": "approved", "已通过": "approved",
}
STATUS_LABEL = {"pending": "待审核", "rejected": "已驳回", "approved": "已通过"}
SHEET_NAME = "上下架请求"


def resolve_data_dir(args: argparse.Namespace) -> Path:
    if args.data_dir:
        return Path(args.data_dir)
    ini = Path(args.ini) if args.ini else PROJECT_ROOT / "app.ini"
    if ini.exists():
        import configparser

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(ini, encoding="utf-8")
        text = parser.get("app", "data_dir", fallback="").strip()
        if text:
            path = Path(text)
            return path if path.is_absolute() else ini.parent / path
    return PROJECT_ROOT / "data"


def status_text(row: sqlite3.Row) -> str:
    if row["status"] == "approved":
        return "已通过"
    if row["status"] == "rejected":
        return "已驳回"
    keys = row.keys()
    stage = row["stage"] if "stage" in keys else ""
    request_type = row["request_type"] if "request_type" in keys else "上架"
    if not stage:  # 兼容无 stage 字段的历史数据
        stage = "allocation" if request_type == "上架" else "implementation"
    if stage == "allocation":
        return "待审核(机位IP分配)"
    if stage == "implementation":
        return "待审核(具体实施)"
    return "待审核"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="导出上下架请求为 xlsx（导入格式 + 审核状态列）")
    parser.add_argument("--status", nargs="+", default=["pending", "rejected", "approved"],
                        help="导出状态：待审核/已驳回/已通过（或 pending/rejected/approved），可多选，默认全部")
    parser.add_argument("--out", default="", help="输出 xlsx 路径，默认导出到当前目录并带时间戳")
    parser.add_argument("--ini", default="", help="app.ini 路径（用于定位 data_dir）")
    parser.add_argument("--data-dir", default="", help="直接指定数据目录（优先于 app.ini）")
    args = parser.parse_args()

    statuses = [STATUS_ALIASES.get(item.strip()) for item in args.status]
    if not statuses or any(s is None for s in statuses):
        parser.error(f"无效的状态参数：{args.status}（可选：待审核/已驳回/已通过 或 pending/rejected/approved）")
    statuses = sorted(set(statuses))

    data_dir = resolve_data_dir(args)
    db_path = data_dir / "app.db"
    if not db_path.exists():
        print(f"错误：数据库不存在：{db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM rack_requests WHERE status IN ({placeholders}) ORDER BY submitted_at, rowid",
            statuses,
        ).fetchall()
    finally:
        conn.close()

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        print("错误：缺少 openpyxl，请先安装：pip install openpyxl")
        return 1

    headers = RACK_REQUEST_HEADERS + ["审核状态"]
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        data = json.loads(row["request_data"] or "{}")
        if not isinstance(data, dict):
            data = {}
        ws.append([str(data.get(header) or "") for header in RACK_REQUEST_HEADERS] + [status_text(row)])
    ws.freeze_panes = "A2"
    for index, header in enumerate(headers, start=1):
        width = max(len(header) * 2, 12)
        if header in ("防火墙白名单", "备注"):
            width = 40
        ws.column_dimensions[ws.cell(row=1, column=index).column_letter].width = width

    out = Path(args.out) if args.out else Path.cwd() / f"上下架请求导出_{datetime.now():%Y%m%d-%H%M%S}.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)

    counts = {STATUS_LABEL[s]: 0 for s in statuses}
    for row in rows:
        counts[STATUS_LABEL[row["status"]]] += 1
    summary = "、".join(f"{k} {v} 条" for k, v in counts.items() if v)
    print(f"已导出 {len(rows)} 条（{summary or '无匹配记录'}） -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
