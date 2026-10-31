#!/usr/bin/env python3
"""结构迁移脚本：服务器表加回「负责人」列，并按客户 id 从客户表「负责人」列回填。

背景：此前一次迁移把服务器表的「负责人」列删除（负责人收归客户表）。现需求调整为：
服务器表重新存储「负责人」字段（在「客户名称」列之后），该字段可由前端手动修改，
并提供一个「自动同步负责人」按钮从客户表补充。因此需要把服务器表 xlsx 加回「负责人」列，
并以客户表的「负责人」值作为初始值回填（客户 id 关联）。

迁移内容（仅数据文件结构，应用代码只适配新结构、不做旧字段兼容）：
  1. 服务器表（data/official/服务器信息表.xlsx）
       - 在「客户名称」列之后插入「负责人」列
       - 按「客户id」关联客户表，回填「负责人」初始值
       - 若服务器表已存在「负责人」列，则跳过（幂等）
  2. 客户表（data/official/客户表.xlsx）—— 无需改动（已含「负责人」「客户经理」）
  3. SQLite（data/app.db）—— 无需改动（上下架请求与 changes 中本就不含「负责人」）

历史归档（data/versions/*、data/backups/*、data/migration-backups/*）属于只读快照，不参与运行时读取，
本脚本不改动它们。

用法（在项目根目录执行）：
    python scripts/migrate_server_owner_back.py [--data-dir PATH] [--dry-run]

  --data-dir  数据目录，默认取项目根 app.ini 的 data_dir（相对项目根解析）
  --dry-run   只打印将要执行的改动，不实际写入
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CUSTOMER_FILENAME = "客户表.xlsx"
SERVER_FILENAME = "服务器信息表.xlsx"

OWNER = "负责人"
ANCHOR = "客户名称"


def resolve_data_dir(args) -> Path:
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


def load_rows(path: Path):
    from openpyxl import load_workbook

    wb = load_workbook(path)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    def norm(v):
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()

    if not rows:
        return [], []
    headers = [norm(v) for v in rows[0]]
    data = [[norm(v) for v in r] for r in rows[1:]]
    return headers, data


def save_rows(path: Path, headers, data):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "服务器信息"
    ws.append(headers)
    for row in data:
        ws.append(row)
    wb.save(path)
    wb.close()


def migrate(dry_run: bool, official_dir: Path) -> int:
    customer_path = official_dir / CUSTOMER_FILENAME
    server_path = official_dir / SERVER_FILENAME

    if not server_path.exists():
        print(f"[跳过] 服务器表不存在：{server_path}")
        return 0
    if not customer_path.exists():
        print(f"[跳过] 客户表不存在：{customer_path}")
        return 0

    customer_headers, customer_data = load_rows(customer_path)
    owner_idx_customer = customer_headers.index(OWNER) if OWNER in customer_headers else None
    customer_id_idx_customer = customer_headers.index("客户id") if "客户id" in customer_headers else None

    # 客户 id -> 负责人
    customer_owner_map = {}
    if owner_idx_customer is not None and customer_id_idx_customer is not None:
        for row in customer_data:
            cid = row[customer_id_idx_customer] if customer_id_idx_customer < len(row) else ""
            owner = row[owner_idx_customer] if owner_idx_customer < len(row) else ""
            if cid:
                customer_owner_map[cid] = owner

    server_headers, server_data = load_rows(server_path)

    # 幂等：已含「负责人」列则跳过
    if OWNER in server_headers:
        print(f"[跳过] 服务器表已含「{OWNER}」列，无需迁移：{server_path}")
        return 0

    if ANCHOR not in server_headers:
        print(f"[错误] 服务器表缺少锚点列「{ANCHOR}」，无法确定插入位置：{server_path}")
        return 1

    customer_id_idx = server_headers.index("客户id") if "客户id" in server_headers else None
    anchor_idx = server_headers.index(ANCHOR)

    # 在「客户名称」后插入「负责人」
    new_headers = server_headers[: anchor_idx + 1] + [OWNER] + server_headers[anchor_idx + 1:]
    new_owner_col = anchor_idx + 1

    filled = 0
    new_data = []
    for row in server_data:
        while len(row) < len(server_headers):
            row.append("")
        cid = row[customer_id_idx] if customer_id_idx is not None and customer_id_idx < len(row) else ""
        owner = customer_owner_map.get(cid, "")
        if owner:
            filled += 1
        new_row = row[: new_owner_col] + [owner] + row[new_owner_col:]
        new_data.append(new_row)

    if dry_run:
        print(f"[dry-run] 服务器表：在「{ANCHOR}」后插入「{OWNER}」列，回填 {filled} 条负责人（客户数 {len(customer_owner_map)}）")
    else:
        save_rows(server_path, new_headers, new_data)
        print(f"[已改] 服务器表：在「{ANCHOR}」后插入「{OWNER}」列，回填 {filled} 条负责人 -> {server_path}")

    print("回填映射（客户id -> 负责人）：")
    for cid, owner in customer_owner_map.items():
        print(f"  {cid} -> {owner}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="服务器表加回负责人列并回填")
    parser.add_argument("--data-dir", default="", help="数据目录，默认取 app.ini 的 data_dir")
    parser.add_argument("--ini", default="", help="app.ini 路径（用于定位 data_dir）")
    parser.add_argument("--dry-run", action="store_true", help="只打印改动，不写入")
    args = parser.parse_args()

    data_dir = resolve_data_dir(args)
    official_dir = data_dir / "official"

    print(f"数据目录：{data_dir}")
    print(f"dry-run：{'是' if args.dry_run else '否'}\n")

    code = migrate(args.dry_run, official_dir)
    print("\n完成。历史归档（versions/backups/migration-backups）未改动。")
    return code


if __name__ == "__main__":
    sys.exit(main())
