#!/usr/bin/env python3
"""数据迁移脚本：把「是否上架」时代的上下架请求数据适配到「请求类型」模型。

背景
    请求表原来用「是否上架」（是/否）表达意图，现在改成「请求类型」（上架/下架/变更）：
      上架 → 机位IP分配 → 具体实施（新增服务器记录）
      下架 → 直接进具体实施（完成后整行进回收站）
      变更 → 直接进具体实施（改完直接写正式表）
    同时「上下架需求」输入框被取消（统一走备注），请求表新增「接入行情」「接入柜台」，
    服务器表的「用途分类」改为多值标签。

迁移内容（只动 app.db 与 field_options，不动任何 Excel 表头）
    1. rack_requests.request_data（JSON 文本）
         - 「是否上架」→「请求类型」（是=上架，否=下架；以 request_type 列为准）
         - 「上下架需求」并入「备注」（形如「上下架需求：xxx」），然后删除该键
         - 补齐「接入行情」「接入柜台」（缺失即空）
         - 按新表头顺序重建 JSON，多余旧键丢弃
         - request_type 列同步写回「请求类型」
    2. field_options 表
         - 建表（若不存在）
         - 把服务器表里「用途分类 / 接入行情 / 接入柜台」已出现的标签收进候选池
           （source=data，幂等 INSERT OR IGNORE）
    3. trash 表
         - 建表（回收站；下架实施完成后服务器整行进这里）

刻意不做的事
    - 不给新代码留「兼容旧字段」的分支：旧数据由本脚本一次性适配干净，
      之后 normalize_rack_request 只认「请求类型」，缺字段就是缺字段（校验会报错）。
    - 不改 official/*.xlsx 的任何表头：请求表本来就不在 Excel 里，
      「用途分类」列早就存在，只是值开始按多值解析。

幂等与安全
    - 每条请求只处理一次：处理完就没有「是否上架」「上下架需求」了
    - 默认先备份 app.db（sqlite backup API），再在单事务里写入
    - 写入后回读校验，任何一处不符退出码 2

用法（在项目根目录执行；用跑服务的那个 python）
    python scripts/migrate_rack_request_types.py              # 迁移默认数据目录
    python scripts/migrate_rack_request_types.py --dry-run    # 只看将做的改动
    python scripts/migrate_rack_request_types.py --data-dir /home/cyn/server-management/server-management-v1/data
    python scripts/migrate_rack_request_types.py --ini /srv/sm/app.ini

退出码
    0 成功（或已是最新 / 仅 dry-run）
    1 找不到 app.db
    2 写入后回读校验失败
    3 环境不支持（sqlite/openpyxl 不可用等）
"""
from __future__ import annotations

import argparse
import ast
import configparser
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_FILENAME = "app.db"
SERVER_WORKBOOK = Path("official") / "服务器信息表.xlsx"

# ---------------------------------------------------------------- 权威目标结构
# 刻意不从 app/core.py 执行读取：服务器上的代码可能还是旧版本。
# 下面这份表头是本脚本的权威定义，app/core.py 只做交叉校验（见 cross_check_headers）。
REQUEST_TYPES = ("上架", "下架", "变更")
RACK_REQUEST_HEADERS = [
    "序列号", "请求类型", "品牌", "型号", "CPU型号", "网卡", "服务器成本(万)",
    "防火墙白名单", "资源申请编号", "是否液冷", "接入行情", "接入柜台",
    "是否重装系统", "初始密码", "备注", "客户id", "客户名称", "所在机房",
]
MULTI_VALUE_FIELDS = ("用途分类", "接入行情", "接入柜台")
RACK_REQUEST_MULTI_VALUE_FIELDS = ("接入行情", "接入柜台")
TAG_SEPARATOR_PATTERN = r"[,，、;；\r\n]+"
TAG_JOINER = ", "
TAG_MAX_LENGTH = 40

LEGACY_REQUEST_TYPE_KEY = "是否上架"
LEGACY_NOTE_KEY = "上下架需求"

TRASH_DDL = """CREATE TABLE IF NOT EXISTS trash (
    id TEXT PRIMARY KEY,
    serial_number TEXT NOT NULL,
    data TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    deleted_at TEXT NOT NULL,
    deleted_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT ''
)"""

FIELD_OPTIONS_DDL = """CREATE TABLE IF NOT EXISTS field_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT NOT NULL,
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
)"""

FIELD_OPTIONS_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS idx_field_options_unique ON field_options(entity, field, value)"


# ------------------------------------------------------------------ 环境与路径


def resolve_data_dir(args) -> Path:
    if args.data_dir:
        return Path(args.data_dir).expanduser()
    ini = Path(args.ini).expanduser() if args.ini else PROJECT_ROOT / "app.ini"
    if ini.exists():
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read(ini, encoding="utf-8")
            text = parser.get("app", "data_dir", fallback="").strip()
        except Exception as exc:
            print(f"[警告] 解析 {ini} 失败（{type(exc).__name__}: {exc}），回退到 {PROJECT_ROOT / 'data'}")
            return PROJECT_ROOT / "data"
        if text:
            path = Path(text).expanduser()
            return path if path.is_absolute() else ini.resolve().parent / path
        print(f"[警告] {ini} 未配置 app.data_dir，回退到 {PROJECT_ROOT / 'data'}")
    else:
        print(f"[警告] 找不到配置文件 {ini}，回退到 {PROJECT_ROOT / 'data'}")
    return PROJECT_ROOT / "data"


def describe(path: Path) -> bool:
    if not path.exists():
        return False
    stat = path.stat()
    stamp = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    print(f"  路径：{path.resolve()}")
    print(f"  大小：{stat.st_size} 字节    修改时间：{stamp}")
    return True


# ------------------------------------------------------------ 与运行时代码交叉校验


def runtime_headers() -> tuple[list[str] | None, str]:
    """用 ast 读 app/core.py 里的 RACK_REQUEST_HEADERS 字面量（不执行代码）。"""
    path = PROJECT_ROOT / "app" / "core.py"
    if not path.exists():
        return None, f"找不到 {path}"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return None, f"读取/解析 {path} 失败（{exc}）"
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target]
            value = node.value
        else:
            continue
        if not any(t.id == "RACK_REQUEST_HEADERS" for t in targets):
            continue
        try:
            data = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None, "RACK_REQUEST_HEADERS 不是纯字面量，无法静态读取"
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            return None, "RACK_REQUEST_HEADERS 不是字符串列表"
        return data, ""
    return None, "app/core.py 里没有 RACK_REQUEST_HEADERS（服务器上可能还是旧版本代码）"


def cross_check_headers() -> None:
    headers, why = runtime_headers()
    if headers is None:
        print(f"[提示] 跳过与运行时代码的交叉校验：{why}")
        print("       本脚本仍按自带的目标表头执行。\n")
        return
    if headers == RACK_REQUEST_HEADERS:
        print(f"[校验] 与 app/core.py 的 RACK_REQUEST_HEADERS 一致（{len(headers)} 列）\n")
        return
    print("[警告] 脚本目标表头与 app/core.py 的定义不一致：")
    print(f"       脚本（{len(RACK_REQUEST_HEADERS)} 列）：{RACK_REQUEST_HEADERS}")
    print(f"       代码（{len(headers)} 列）：{headers}")
    print("       ↑ 请确认线上 app/core.py 是否已更新为最新代码。")
    print("         本脚本按『脚本』的目标表头执行。\n")


# ------------------------------------------------------------------------ 工具


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def split_tags(value) -> list[str]:
    import re

    if isinstance(value, (list, tuple, set)):
        raw = [clean_text(item) for item in value]
    else:
        raw = [clean_text(item) for item in re.split(TAG_SEPARATOR_PATTERN, clean_text(value))]
    tags: list[str] = []
    for tag in raw:
        tag = " ".join(tag.split())
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def join_tags(value) -> str:
    return TAG_JOINER.join(split_tags(value))


def initial_stage(request_type: str) -> str:
    return "allocation" if request_type == "上架" else "implementation"


def allowed_stages(request_type: str) -> set[str]:
    stages = {"implementation", "completed", "rejected"}
    if request_type == "上架":
        stages.add("allocation")
    return stages


def migrate_request_data(raw: str, request_type_column: str) -> tuple[dict, str, list[str]]:
    """返回 (新 request_data, 请求类型, 变更说明)。"""
    notes: list[str] = []
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
        notes.append("request_data 不是合法 JSON，已按空数据重建")
    if not isinstance(data, dict):
        data = {}
        notes.append("request_data 不是对象，已按空数据重建")

    request_type = clean_text(data.get("请求类型"))
    legacy_flag = clean_text(data.get(LEGACY_REQUEST_TYPE_KEY))
    if request_type not in REQUEST_TYPES:
        if request_type_column in REQUEST_TYPES:
            request_type = request_type_column
        elif legacy_flag in {"是", "否"}:
            request_type = "上架" if legacy_flag == "是" else "下架"
        else:
            request_type = "上架"
            notes.append("既无请求类型也无历史标记，按上架处理")
    if LEGACY_REQUEST_TYPE_KEY in data:
        notes.append(f"是否上架={legacy_flag or '空'} → 请求类型={request_type}")

    note_text = clean_text(data.get(LEGACY_NOTE_KEY))
    memo = clean_text(data.get("备注"))
    if note_text:
        merged = f"{memo}\n上下架需求：{note_text}" if memo else f"上下架需求：{note_text}"
        data["备注"] = merged
        notes.append("上下架需求已并入备注")

    rebuilt: dict[str, str] = {}
    for header in RACK_REQUEST_HEADERS:
        if header == "请求类型":
            rebuilt[header] = request_type
        elif header in RACK_REQUEST_MULTI_VALUE_FIELDS:
            rebuilt[header] = join_tags(data.get(header))
        else:
            rebuilt[header] = clean_text(data.get(header))

    dropped = [key for key in data if key not in RACK_REQUEST_HEADERS]
    if dropped:
        notes.append(f"丢弃旧字段：{'、'.join(dropped)}")
    return rebuilt, request_type, notes


def truncate_tags(field: str, tags: list[str], notes: list[str]) -> list[str]:
    kept = []
    for tag in tags:
        if len(tag) > TAG_MAX_LENGTH:
            notes.append(f"{field} 标签「{tag[:12]}…」超过 {TAG_MAX_LENGTH} 字，已跳过收池")
            continue
        kept.append(tag)
    return kept


# ---------------------------------------------------------------- 服务器表标签


def server_tags(server_path: Path) -> tuple[dict[str, list[str]], str]:
    """从服务器信息表读出多值字段出现过的标签（只读，不写 Excel）。"""
    if not server_path.exists():
        return {}, f"找不到 {server_path}"
    try:
        from openpyxl import load_workbook
    except ImportError:
        return {}, "openpyxl 不可用"
    try:
        wb = load_workbook(server_path, read_only=True, data_only=True)
    except Exception as exc:
        return {}, f"读取 {server_path} 失败（{exc}）"
    try:
        ws = wb[wb.sheetnames[0]]
        rows = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            return {}, "服务器信息表是空表"
        headers = [clean_text(value) for value in header_row]
        index = {name: pos for pos, name in enumerate(headers) if name}
        missing = [field for field in MULTI_VALUE_FIELDS if field not in index]
        if missing:
            return {}, f"服务器信息表缺少列：{'、'.join(missing)}"
        collected = {field: [] for field in MULTI_VALUE_FIELDS}
        for values in rows:
            for field in MULTI_VALUE_FIELDS:
                pos = index[field]
                if pos >= len(values):
                    continue
                for tag in split_tags(values[pos]):
                    if tag not in collected[field]:
                        collected[field].append(tag)
        return collected, ""
    finally:
        wb.close()


# ------------------------------------------------------------------------ 库操作


def table_names(conn) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def table_columns(conn, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def backup_database(db_path: Path, data_dir: Path) -> Path:
    backups_dir = data_dir / "migration-backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backups_dir / f"app-rack-request-types-{stamp}.db"
    source = sqlite3.connect(str(db_path))
    try:
        dest = sqlite3.connect(str(target))
        try:
            with dest:
                source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()
    return target


def summarize_requests(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id,serial_number,request_type,request_data,status,stage FROM rack_requests ORDER BY submitted_at,rowid"
    ).fetchall()
    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "serial_number": row["serial_number"],
            "request_type": clean_text(row["request_type"]),
            "request_data": row["request_data"],
            "status": row["status"],
            "stage": clean_text(row["stage"]),
        })
    return result


def migrate(db_path: Path, data_dir: Path, dry_run: bool = False) -> int:
    server_path = data_dir / SERVER_WORKBOOK
    print("服务器信息表：")
    if not describe(server_path):
        print("  文件不存在（跳过标签收池）")
    print()

    tags, tags_problem = server_tags(server_path)
    if tags_problem:
        print(f"[提示] 标签收池跳过：{tags_problem}")
    else:
        counts = "、".join(f"{field} {len(items)} 个" for field, items in tags.items())
        print(f"[数据] 服务器表里的标签：{counts}\n")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = table_names(conn)
        print(f"当前表（{len(tables)}）：{', '.join(tables) or '（空库）'}")
        if "rack_requests" not in tables:
            print("[错误] 库里没有 rack_requests 表，这不是本脚本负责的库")
            return 1
        columns = set(table_columns(conn, "rack_requests"))
        for required in ("id", "serial_number", "request_type", "request_data", "status"):
            if required not in columns:
                print(f"[错误] rack_requests 缺列 {required}，请先升级到最新结构")
                return 1
        has_stage = "stage" in columns

        requests = summarize_requests(conn)
        plans = []
        for item in requests:
            rebuilt, request_type, notes = migrate_request_data(item["request_data"], item["request_type"])
            stage = item["stage"] if has_stage else ""
            new_stage = stage
            if item["status"] == "pending" and (not stage or stage not in allowed_stages(request_type)):
                new_stage = initial_stage(request_type)
                notes.append(f"阶段 {stage or '空'} → {new_stage}")
            plans.append({
                **item,
                "new_data": rebuilt,
                "new_type": request_type,
                "new_stage": new_stage,
                "notes": notes,
            })

        touched = [plan for plan in plans if plan["notes"]]
        print(f"请求共 {len(plans)} 条，需要改写 {len(touched)} 条")
        for plan in touched[:20]:
            print(f"  - {plan['serial_number']} [{plan['status']}/{plan['new_stage']}] "
                  f"{plan['new_type']}：{'；'.join(plan['notes'])}")
        if len(touched) > 20:
            print(f"  …… 其余 {len(touched) - 20} 条同类处理")
        print()

        trash_exists = "trash" in tables
        field_options_exists = "field_options" in tables
        pending_tags = 0
        if not tags_problem:
            existing_pool = set()
            if field_options_exists:
                for row in conn.execute("SELECT field,value FROM field_options WHERE entity='servers'").fetchall():
                    existing_pool.add((row[0], row[1]))
            for field, items in tags.items():
                for tag in items:
                    if len(tag) <= TAG_MAX_LENGTH and (field, tag) not in existing_pool:
                        pending_tags += 1

        if not touched and trash_exists and field_options_exists and not pending_tags:
            print("[跳过] 请求数据、trash 表与标签池均已就绪，无需迁移")
            return 0

        if dry_run:
            print("[dry-run] 将执行：")
            if not trash_exists:
                print("  CREATE TABLE trash")
            if not field_options_exists:
                print("  CREATE TABLE field_options + 唯一索引")
            if touched:
                print(f"  UPDATE rack_requests × {len(touched)}（request_data / request_type / stage）")
            if pending_tags:
                print(f"  INSERT OR IGNORE field_options × {pending_tags}（source=data）")
            print("[dry-run] 未写入任何内容")
            return 0

        backup = backup_database(db_path, data_dir)
        print(f"已备份数据库 → {backup}\n")

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        added_tags = 0
        with conn:
            conn.execute(TRASH_DDL)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trash_serial ON trash(serial_number)")
            conn.execute(FIELD_OPTIONS_DDL)
            conn.execute(FIELD_OPTIONS_INDEX)

            for plan in touched:
                conn.execute(
                    """UPDATE rack_requests SET request_data=?, request_type=?, stage=?
                       WHERE id=?""",
                    (
                        json.dumps(plan["new_data"], ensure_ascii=False),
                        plan["new_type"],
                        plan["new_stage"],
                        plan["id"],
                    ),
                )

            if not tags_problem:
                for field, items in tags.items():
                    for tag in truncate_tags(field, items, []):
                        cursor = conn.execute(
                            """INSERT OR IGNORE INTO field_options
                               (entity,field,value,source,created_at,created_by) VALUES (?,?,?,?,?,?)""",
                            ("servers", field, tag, "data", stamp, "migration"),
                        )
                        added_tags += cursor.rowcount or 0
    finally:
        conn.close()

    # ------------------------------------------------------------ 回读校验
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    problems: list[str] = []
    try:
        tables = table_names(conn)
        if "trash" not in tables:
            problems.append("trash 表不存在")
        if "field_options" not in tables:
            problems.append("field_options 表不存在")
        rows = summarize_requests(conn)
        for row in rows:
            try:
                data = json.loads(row["request_data"])
            except (TypeError, ValueError):
                problems.append(f"{row['serial_number']}：request_data 不是合法 JSON")
                continue
            if not isinstance(data, dict):
                problems.append(f"{row['serial_number']}：request_data 不是对象")
                continue
            if LEGACY_REQUEST_TYPE_KEY in data:
                problems.append(f"{row['serial_number']}：仍残留「{LEGACY_REQUEST_TYPE_KEY}」")
            if LEGACY_NOTE_KEY in data:
                problems.append(f"{row['serial_number']}：仍残留「{LEGACY_NOTE_KEY}」")
            if list(data.keys()) != RACK_REQUEST_HEADERS:
                problems.append(f"{row['serial_number']}：字段集合/顺序与目标表头不一致")
            if data.get("请求类型") != row["request_type"]:
                problems.append(f"{row['serial_number']}：request_type 列与请求类型不一致")
        pool = set()
        for row in conn.execute("SELECT field,value FROM field_options WHERE entity='servers'").fetchall():
            pool.add((row["field"], row["value"]))
        if not tags_problem:
            for field, items in tags.items():
                for tag in items:
                    if len(tag) <= TAG_MAX_LENGTH and (field, tag) not in pool:
                        problems.append(f"标签池缺少 {field}「{tag}」")
    finally:
        conn.close()

    print()
    if problems:
        print("[校验失败]")
        for item in problems[:20]:
            print(f"  - {item}")
        if len(problems) > 20:
            print(f"  …… 共 {len(problems)} 项")
        print("  可用 migration-backups 里的备份回滚\n")
        return 2

    print(f"[校验通过] 请求 {len(plans)} 条全部符合新表头；trash 表就绪；新增标签 {added_tags} 个")
    if touched:
        print(f"           改写 {len(touched)} 条请求（含上下架需求并入备注的旧记录）")
    print("           下一步：部署最新代码并重启服务\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把上下架请求数据从「是否上架」迁移到「请求类型」模型")
    parser.add_argument("--data-dir", default="", help="数据目录，默认取 app.ini 的 data_dir")
    parser.add_argument("--ini", default="", help="配置文件路径，默认项目根 app.ini")
    parser.add_argument("--dry-run", action="store_true", help="只打印现状与将执行的改动，不写入")
    args = parser.parse_args()

    print(f"Python：{sys.executable}")
    print(f"SQLite：{sqlite3.sqlite_version}")
    try:
        import openpyxl

        print(f"openpyxl：{openpyxl.__version__}\n")
    except ImportError:
        print("openpyxl：（不可用，标签收池会跳过）\n")

    data_dir = resolve_data_dir(args)
    db_path = data_dir / DB_FILENAME
    print(f"数据目录：{data_dir}")
    print("数据库文件：")
    if not describe(db_path):
        print("  文件不存在")
        print("       用 --data-dir 指定正确数据目录")
        return 1
    print()

    cross_check_headers()

    try:
        return migrate(db_path, data_dir, dry_run=args.dry_run)
    except sqlite3.Error as exc:
        print(f"[错误] SQLite 操作失败：{exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
