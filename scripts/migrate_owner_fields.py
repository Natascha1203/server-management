#!/usr/bin/env python3
"""结构迁移脚本：客户表「联系人→负责人」并新增「客户经理」，服务器表/上下架请求去除「负责人」。

本脚本负责改动数据文件（xlsx 与 SQLite）的字段结构，应用代码只适配新结构、不再兼容旧字段。

迁移内容：
  1. 客户表（data/official/客户表.xlsx）
       - 表头「联系人」重命名为「负责人」
       - 末尾新增「客户经理」列（留空）
       - 回填「负责人」：将服务器表里原有的「负责人」值，按客户 id 回填到对应客户的「负责人」列
         （同一客户关联多台服务器时取第一条非空值；客户表已有负责人值时不再覆盖）
  2. 服务器表（data/official/服务器信息表.xlsx）
       - 删除「负责人」列
  3. SQLite（data/app.db）
       - rack_requests.request_data / stage_data：删除 JSON 中的「负责人」键
       - changes.new_data：删除 JSON 中的「负责人」键（含历史待审批/已审记录）

历史归档（data/versions/*、data/backups/*、data/migration-backups/*）属于只读快照，不参与运行时读取，
本脚本不改动它们。

用法（在项目根目录执行）：
    python scripts/migrate_owner_fields.py [--data-dir PATH] [--dry-run]

  --data-dir  数据目录，默认取项目根 app.ini 的 data_dir（相对项目根解析）
  --dry-run   只打印将要执行的改动，不实际写入
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CUSTOMER_FILENAME = "客户表.xlsx"
SERVER_FILENAME = "服务器信息表.xlsx"
DB_FILENAME = "app.db"

OLD_CONTACT = "联系人"
NEW_OWNER = "负责人"
NEW_MANAGER = "客户经理"


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
    """读取 xlsx 为 [表头 list, [数据行 list, ...]]，单元格值统一 str/'' 。"""
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
    ws.title = "客户表" if "客户表" in path.name else "服务器信息"
    ws.append(headers)
    for row in data:
        ws.append(row)
    wb.save(path)
    wb.close()


def migrate_customer_xlsx(path: Path, server_owner_map, dry_run: bool):
    """客户表：联系人→负责人、新增客户经理列、回填负责人。返回改动说明列表。"""
    notes = []
    if not path.exists():
        notes.append(f"[跳过] 客户表不存在：{path}")
        return notes
    headers, data = load_rows(path)

    if OLD_CONTACT not in headers:
        notes.append(f"[跳过] 客户表已无「{OLD_CONTACT}」列：{path}")
        return notes

    contact_idx = headers.index(OLD_CONTACT)
    headers[contact_idx] = NEW_OWNER
    if NEW_MANAGER not in headers:
        headers.append(NEW_MANAGER)

    filled = 0
    for row in data:
        while len(row) < len(headers):
            row.append("")
        customer_id = row[0] if row else ""
        owner_idx = headers.index(NEW_OWNER)
        if not row[owner_idx] and customer_id and customer_id in server_owner_map:
            row[owner_idx] = server_owner_map[customer_id]
            filled += 1

    if dry_run:
        notes.append(f"[dry-run] 客户表：联系人→负责人，新增客户经理列，回填 {filled} 个客户负责人")
    else:
        save_rows(path, headers, data)
        notes.append(f"[已改] 客户表：联系人→负责人，新增客户经理列，回填 {filled} 个客户负责人 -> {path}")
    return notes


def migrate_server_xlsx(path: Path, server_owner_map, dry_run: bool):
    """服务器表：删除负责人列，返回改动说明。"""
    notes = []
    if not path.exists():
        notes.append(f"[跳过] 服务器表不存在：{path}")
        return notes
    headers, data = load_rows(path)

    if NEW_OWNER not in headers:
        notes.append(f"[跳过] 服务器表已无「{NEW_OWNER}」列：{path}")
        return notes

    owner_idx = headers.index(NEW_OWNER)
    # 提取客户id列用于回填（服务器表客户id列名固定为「客户id」）
    customer_id_idx = headers.index("客户id") if "客户id" in headers else None

    removed_owner_count = 0
    for row in data:
        owner_value = row[owner_idx] if owner_idx < len(row) else ""
        if owner_value and customer_id_idx is not None:
            cid = row[customer_id_idx] if customer_id_idx < len(row) else ""
            if cid and cid not in server_owner_map:
                server_owner_map[cid] = owner_value
                removed_owner_count += 1

    new_headers = headers[:owner_idx] + headers[owner_idx + 1:]
    new_data = [row[:owner_idx] + row[owner_idx + 1:] for row in data]

    if dry_run:
        notes.append(f"[dry-run] 服务器表：删除「负责人」列（提取 {removed_owner_count} 个客户的负责人用于回填）")
    else:
        save_rows(path, new_headers, new_data)
        notes.append(f"[已改] 服务器表：删除「负责人」列 -> {path}")
    return notes


def migrate_sqlite(db_path: Path, dry_run: bool):
    """SQLite：删除 rack_requests 与 changes 中 JSON 字段的「负责人」键。返回改动说明。"""
    notes = []
    if not db_path.exists():
        notes.append(f"[跳过] 数据库不存在：{db_path}")
        return notes

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    def strip_owner(value):
        if not value:
            return value, False
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            return value, False
        if not isinstance(data, dict):
            return value, False
        if NEW_OWNER not in data:
            return value, False
        data.pop(NEW_OWNER, None)
        return json.dumps(data, ensure_ascii=False), True

    # 1) rack_requests
    rack_rows = conn.execute("SELECT id, request_data, stage_data FROM rack_requests").fetchall()
    rack_changed = 0
    for r in rack_rows:
        new_rd, rd_changed = strip_owner(r["request_data"])
        new_sd, sd_changed = strip_owner(r["stage_data"])
        if rd_changed or sd_changed:
            if not dry_run:
                conn.execute(
                    "UPDATE rack_requests SET request_data=?, stage_data=? WHERE id=?",
                    (new_rd, new_sd, r["id"]),
                )
            rack_changed += 1

    # 2) changes
    change_rows = conn.execute("SELECT id, new_data FROM changes").fetchall()
    change_changed = 0
    for c in change_rows:
        new_nd, nd_changed = strip_owner(c["new_data"])
        if nd_changed:
            if not dry_run:
                conn.execute("UPDATE changes SET new_data=? WHERE id=?", (new_nd, c["id"]))
            change_changed += 1

    if dry_run:
        notes.append(f"[dry-run] SQLite：rack_requests 待清理 {rack_changed} 条、changes 待清理 {change_changed} 条「负责人」字段")
    else:
        conn.commit()
        notes.append(f"[已改] SQLite：rack_requests 清理 {rack_changed} 条、changes 清理 {change_changed} 条「负责人」字段 -> {db_path}")
    conn.close()
    return notes


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="客户/服务器负责人字段结构迁移")
    parser.add_argument("--data-dir", default="", help="数据目录，默认取 app.ini 的 data_dir")
    parser.add_argument("--ini", default="", help="app.ini 路径（用于定位 data_dir）")
    parser.add_argument("--dry-run", action="store_true", help="只打印改动，不写入")
    args = parser.parse_args()

    data_dir = resolve_data_dir(args)
    official_dir = data_dir / "official"

    print(f"数据目录：{data_dir}")
    print(f"dry-run：{'是' if args.dry_run else '否'}\n")

    server_owner_map = {}  # 客户id -> 负责人（服务器表旧值回填来源）

    # 顺序：先服务器表（提取旧负责人），再客户表（回填），最后 SQLite（清理 JSON）
    notes = []
    notes += migrate_server_xlsx(official_dir / SERVER_FILENAME, server_owner_map, args.dry_run)
    notes += migrate_customer_xlsx(official_dir / CUSTOMER_FILENAME, server_owner_map, args.dry_run)
    notes += migrate_sqlite(data_dir / DB_FILENAME, args.dry_run)

    print("回填映射（客户id -> 负责人）：")
    for cid, owner in server_owner_map.items():
        print(f"  {cid} -> {owner}")
    print()
    for note in notes:
        print(note)

    print("\n完成。历史归档（versions/backups/migration-backups）未改动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
