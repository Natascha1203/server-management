#!/usr/bin/env python3
"""数据库结构迁移脚本：为「服务器资料（附件）」创建 attachments 表与 data/archive 目录。

背景
    v1 新增「服务器资料」功能：上下架请求 / 机位IP分配 / 具体实施 / 服务器信息表
    共用同一份按「序列号」归属的资料档案。元数据存 app.db 的 attachments 表，
    文件本体落在 data/archive/<序列号>/ 下。

迁移内容（只动 app.db 与目录，不动任何 Excel）
    1. <data_dir>/app.db
         - CREATE TABLE IF NOT EXISTS attachments (...)        （结构见 ATTACHMENTS_DDL）
         - CREATE INDEX IF NOT EXISTS idx_attachments_serial   （按序列号查资料的索引）
         - 若表已存在但缺 request_id / source 列，自动 ALTER TABLE 补齐（幂等）
    2. <data_dir>/archive/  新建目录（服务进程要能写才传得上文件）
    3. 不动 official/*.xlsx、versions/、backups/ —— 资料不是表格列，
       导出 / 模板 / 版本快照天生不含它，所以这次没有任何 Excel 表头变更。

这一步其实不是必须手跑 —— 应用启动时 init_storage() 会自动建表、建目录，
部署新代码后重启服务即可自迁移。本脚本用于：
    - 想先改库、暂不重启（或滚动发布前先就位）
    - 想在动库之前留一份带时间戳的 app.db 备份
    - 想先确认线上库里到底是什么状态（--dry-run）

三个容易踩的坑（本脚本已针对性处理）
    a) 目标结构写死在 ATTACHMENTS_DDL 里，不 import app.storage。
       服务器上的 app/storage.py 可能还是旧版本（根本没有 attachments 定义），
       若从代码里读目标结构，会「读到旧结构 → 认为无需迁移 → 静默跳过」。
       脚本只把 app/storage.py 的定义读出来做交叉校验并告警。
    b) 备份用 sqlite3 的 backup API，而不是 cp。服务运行中直接拷 app.db
       可能拿到半写状态（有 -wal / -shm 时更明显），backup API 拿到的一致快照。
    c) archive 目录的属主。用 root 跑脚本会建出 root 属主的目录，
       服务若以 www-data / nobody 运行就写不进去。脚本会打印目录属主，
       并支持 --owner 指定（需要相应权限）。

用法（在项目根目录执行；建议用跑服务的那个 python）
    python scripts/add_attachments_table.py                      # 迁移默认数据目录
    python scripts/add_attachments_table.py --dry-run            # 只看现状与将做的改动
    python scripts/add_attachments_table.py --find               # 只搜 app.db，不改任何东西
    python scripts/add_attachments_table.py --data-dir /srv/sm/data
    python scripts/add_attachments_table.py --ini /srv/sm/app.ini
    sudo python scripts/add_attachments_table.py --owner www-data:www-data

退出码
    0 成功（或已是最新 / 仅 dry-run）
    1 找不到 app.db
    2 写入后回读校验失败
    3 环境不支持（sqlite 不可用等）
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_FILENAME = "app.db"
ARCHIVE_DIRNAME = "archive"
INDEX_NAME = "idx_attachments_serial"

# 本脚本的权威目标结构（与 app/storage.py 的 ATTACHMENTS_SCHEMA 保持一致）。
# 刻意不从 app.storage 读：服务器上的代码可能是旧版本，会导致静默跳过。
ATTACHMENTS_DDL = """CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    serial_number TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    uploader TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT ''
)"""

EXPECTED_COLUMNS = [
    "id",
    "serial_number",
    "filename",
    "stored_name",
    "size",
    "uploader",
    "uploaded_at",
    "request_id",
    "source",
]

# 早期落地的库可能只建了前 7 列，这里按需补（ALTER 只能追加到末尾，顺序刚好对得上）
OPTIONAL_COLUMNS = [
    ("request_id", "request_id TEXT NOT NULL DEFAULT ''"),
    ("source", "source TEXT NOT NULL DEFAULT ''"),
]

INDEX_DDL = f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON attachments(serial_number)"


# ------------------------------------------------------------------ 环境与路径


def resolve_data_dir(args) -> Path:
    if args.data_dir:
        return Path(args.data_dir).expanduser()
    ini = Path(args.ini).expanduser() if args.ini else PROJECT_ROOT / "app.ini"
    if ini.exists():
        import configparser

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read(ini, encoding="utf-8")
            text = parser.get("app", "data_dir", fallback="").strip()
        except Exception as exc:  # 配置写坏时不要让脚本直接崩
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


def find_candidates(roots, limit=10):
    hits, seen = [], set()
    for root in roots:
        if not root or not root.exists():
            continue
        for item in root.rglob(DB_FILENAME):
            resolved = item.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            hits.append(resolved)
            if len(hits) >= limit:
                return sorted(hits)
    return sorted(hits)


def check_ini(ini_path: Path) -> None:
    """提示上传体积上限（app.ini 的 max_upload_mb），缺失即走默认 64MB。"""
    if not ini_path.exists():
        print("[配置] 没找到 app.ini，跳过上传大小检查（代码默认 64MB）\n")
        return
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(ini_path, encoding="utf-8")
        value = parser.get("app", "max_upload_mb", fallback="").strip()
    except Exception as exc:
        print(f"[配置] 解析 {ini_path} 失败（{type(exc).__name__}: {exc}）\n")
        return
    if value:
        print(f"[配置] {ini_path}：max_upload_mb = {value}（单次上传上限）")
    else:
        print(f"[配置] {ini_path}：未配置 max_upload_mb，使用默认 64（MB）")
        print("       要改的话在 [app] 段加一行：max_upload_mb = 64")
    print()


# ------------------------------------------------------------ 与运行时代码交叉校验


def runtime_columns():
    """从 app/storage.py 源码里抠出 ATTACHMENTS_SCHEMA 的列名，仅用于交叉校验。"""
    path = PROJECT_ROOT / "app" / "storage.py"
    if not path.exists():
        return None, f"找不到 {path}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"读取 {path} 失败（{exc}）"
    match = re.search(r'ATTACHMENTS_SCHEMA\s*=\s*"""(.*?)"""', text, re.S)
    if not match:
        return None, "app/storage.py 里没有 ATTACHMENTS_SCHEMA（服务器上可能还是旧版本代码）"
    columns = []
    for raw in match.group(1).splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith(")") or line.upper().startswith("CREATE"):
            continue
        columns.append(line.split()[0])
    return columns, ""


def cross_check() -> None:
    columns, why = runtime_columns()
    if columns is None:
        print(f"[提示] 跳过与运行时代码的交叉校验：{why}")
        print("       本脚本仍按自带的目标结构执行。\n")
        return
    if columns == EXPECTED_COLUMNS:
        print(f"[校验] 与 app/storage.py 的 ATTACHMENTS_SCHEMA 一致（{len(columns)} 列）\n")
        return
    print("[警告] 脚本目标结构与 app/storage.py 的定义不一致：")
    print(f"       脚本（{len(EXPECTED_COLUMNS)} 列）：{EXPECTED_COLUMNS}")
    print(f"       代码（{len(columns)} 列）：{columns}")
    print("       ↑ 请确认线上 app/storage.py 是否已更新为最新代码。")
    print("         本脚本按『脚本』的目标结构执行。\n")


# ------------------------------------------------------------------------ 库操作


def table_names(conn):
    """业务表清单；sqlite_sequence 这类内部表不展示，免得干扰判断。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def table_columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def table_indexes(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()]


def backup_database(db_path: Path, data_dir: Path) -> Path:
    backups_dir = data_dir / "migration-backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backups_dir / f"app-{stamp}.db"
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


def apply_owner(path: Path, spec: str) -> None:
    if not hasattr(os, "chown"):
        print(f"  [警告] 当前平台不支持 chown，忽略 --owner {spec}")
        return
    try:
        import grp
        import pwd
    except ImportError:
        print(f"  [警告] 当前平台缺少 pwd/grp 模块，忽略 --owner {spec}")
        return
    user, _, group = spec.partition(":")
    user = user.strip()
    group = group.strip()
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        print(f"  [警告] --owner 里的用户不存在：{user}")
        return
    gid = -1
    if group:
        try:
            gid = grp.getgrnam(group).gr_gid
        except KeyError:
            print(f"  [警告] --owner 里的用户组不存在：{group}")
            return
    try:
        os.chown(path, uid, gid)
        print(f"  已设置属主：{spec}")
    except OSError as exc:
        print(f"  [警告] chown 失败（{exc}）—— 通常需要 root 权限")


def describe_owner(path: Path) -> None:
    try:
        stat = path.stat()
    except OSError:
        return
    mode = oct(stat.st_mode & 0o777)
    try:
        import grp
        import pwd

        user = pwd.getpwuid(stat.st_uid).pw_name
        group = grp.getgrgid(stat.st_gid).gr_name
    except Exception:
        print(f"  archive 权限：{mode}")
        return
    print(f"  archive 属主：{user}:{group}（权限 {mode}）")
    print(f"  提示：服务进程要对 {path} 有写权限，否则上传会失败；"
          f"若服务以其他用户运行，用 --owner <用户>:<组> 或 chown 修正")


def check_other_schemas(conn, tables) -> None:
    """顺带体检：库是不是来自更早的版本。

    这些列的补齐由应用启动时的 ensure_changes_schema / ensure_rack_requests_schema 负责，
    本脚本不去代劳（改动面越小越安全），只在发现时给出明确提示。
    """
    notes = []
    if "rack_requests" in tables:
        columns = set(table_columns(conn, "rack_requests"))
        missing = [name for name in ("stage", "stage_data") if name not in columns]
        if missing:
            notes.append(f"rack_requests 缺列 {', '.join(missing)}（两阶段审批所需）")
    if "changes" in tables and "old_data" in set(table_columns(conn, "changes")):
        notes.append("changes 表仍是带 old_data 的旧结构")
    if not notes:
        return
    print("[提示] 这个库来自更早的版本：")
    for note in notes:
        print(f"       - {note}")
    print("       这些由新代码启动时的 ensure_*_schema 自动补齐，本脚本不处理；")
    print("       部署最新代码后先启动一次服务即可。\n")


def ensure_archive(archive_dir: Path, dry_run: bool, owner: str) -> bool:
    """确保 archive 目录存在，返回它最终是否可用。"""
    if archive_dir.is_dir():
        print(f"  archive 目录已存在：{archive_dir}")
        if owner:
            apply_owner(archive_dir, owner)
        describe_owner(archive_dir)
        return True
    if dry_run:
        print(f"  [dry-run] 将创建目录：{archive_dir}（权限 755）")
        return False
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(archive_dir, 0o755)
    except OSError:
        pass
    print(f"  已创建目录：{archive_dir}（权限 755）")
    if owner:
        apply_owner(archive_dir, owner)
    describe_owner(archive_dir)
    return True


def migrate(db_path: Path, data_dir: Path, dry_run: bool = False, owner: str = "") -> int:
    archive_dir = data_dir / ARCHIVE_DIRNAME

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = table_names(conn)
        exists = "attachments" in tables
        before_columns = table_columns(conn, "attachments") if exists else []
        before_indexes = table_indexes(conn, "attachments") if exists else []
        missing_columns = [name for name, _ in OPTIONAL_COLUMNS if exists and name not in before_columns]
        has_index = INDEX_NAME in before_indexes

        print("当前状态：")
        print(f"  库里的表（{len(tables)}）：{', '.join(tables) or '（空库）'}")
        if exists:
            print(f"  attachments 列（{len(before_columns)}）：{', '.join(before_columns)}")
            print(f"  attachments 索引：{', '.join(before_indexes) or '（无）'}")
            if missing_columns:
                print(f"  缺列：{', '.join(missing_columns)}")
        else:
            print("  attachments：不存在")
        print(f"  archive 目录：{archive_dir}（存在：{archive_dir.is_dir()}）")
        print()

        check_other_schemas(conn, tables)

        db_need = (not exists) or bool(missing_columns) or (exists and not has_index)
        dir_need = not archive_dir.is_dir()

        if not db_need and not dir_need:
            print("[跳过] attachments 表、索引与 archive 目录均已就绪，无需迁移")
            describe_owner(archive_dir)
            return 0

        if db_need:
            print("将执行：")
            if not exists:
                print("  CREATE TABLE attachments（9 列）")
            for name, _ in OPTIONAL_COLUMNS:
                if exists and name not in before_columns:
                    print(f"  ALTER TABLE attachments ADD COLUMN {name}")
            if not has_index:
                print(f"  CREATE INDEX {INDEX_NAME} ON attachments(serial_number)")

        if dry_run:
            if dir_need:
                print(f"  CREATE DIRECTORY {archive_dir}（权限 755）")
            print("[dry-run] 未写入任何内容")
            return 0

        if db_need:
            backup = backup_database(db_path, data_dir)
            print(f"\n  已备份数据库 → {backup}")
            with conn:
                conn.execute(ATTACHMENTS_DDL)
                for name, column_sql in OPTIONAL_COLUMNS:
                    if name not in table_columns(conn, "attachments"):
                        conn.execute(f"ALTER TABLE attachments ADD COLUMN {column_sql}")
                        print(f"  已补列：{name}")
                conn.execute(INDEX_DDL)
                print("  已写入 attachments 表与索引")

        ensure_archive(archive_dir, dry_run=False, owner=owner)

        # 回读校验，避免「以为写了其实没写」
        actual_columns = table_columns(conn, "attachments")
        actual_indexes = table_indexes(conn, "attachments")

    finally:
        conn.close()

    print()
    problems = []
    if set(actual_columns) != set(EXPECTED_COLUMNS) or len(actual_columns) != len(EXPECTED_COLUMNS):
        problems.append(f"列不符：期望 {EXPECTED_COLUMNS}，实际 {actual_columns}")
    if INDEX_NAME not in actual_indexes:
        problems.append(f"索引 {INDEX_NAME} 不存在（实际 {actual_indexes}）")
    if not archive_dir.is_dir():
        problems.append(f"目录 {archive_dir} 不存在")

    if problems:
        print("[校验失败]")
        for item in problems:
            print(f"  - {item}")
        print("  文件可能被其他进程占用，或没有写权限\n")
        return 2

    print(f"[校验通过] 重新读取 attachments：{len(actual_columns)} 列"
          f"（{', '.join(actual_columns)}），索引 {INDEX_NAME} 就绪")
    if actual_columns != EXPECTED_COLUMNS:
        print(f"[提示] 列顺序与脚本定义不同（实际 {actual_columns}），按列名读写不受影响")
    print("           下一步：部署最新代码并重启服务，登录后即可在上下架请求/审核页看到「资料」\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="为服务器资料（附件）创建 attachments 表与 archive 目录")
    parser.add_argument("--data-dir", default="", help="数据目录，默认取 app.ini 的 data_dir")
    parser.add_argument("--ini", default="", help="配置文件路径，默认项目根 app.ini")
    parser.add_argument("--find", action="store_true", help="只搜索项目内的 app.db，不修改任何文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印现状与将执行的改动，不写入")
    parser.add_argument("--owner", default="", help="创建/修正 archive 目录属主，格式 用户:组（需 root）")
    args = parser.parse_args()

    print(f"Python：{sys.executable}")
    print(f"SQLite：{sqlite3.sqlite_version}\n")

    data_dir = resolve_data_dir(args)
    db_path = data_dir / DB_FILENAME
    ini_path = Path(args.ini).expanduser() if args.ini else PROJECT_ROOT / "app.ini"

    print(f"数据目录：{data_dir}")
    print("数据库文件：")
    if not describe(db_path):
        print("  文件不存在")
    print()

    if args.find:
        found = find_candidates([data_dir, PROJECT_ROOT])
        print(f"在 {data_dir} 与项目根下找到 {len(found)} 个「{DB_FILENAME}」：")
        for item in found:
            stat = item.stat()
            mark = "（本次目标）" if item.resolve() == db_path.resolve() else ""
            print(f"  {item}{mark}  {stat.st_size} 字节  "
                  f"{datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
        print("\n用 --data-dir / --ini 指定正确位置后重跑（不带 --find）")
        return 0

    if not db_path.exists():
        print(f"[错误] 找不到数据库文件：{db_path}")
        candidates = find_candidates([data_dir, PROJECT_ROOT])
        if candidates:
            print("       项目里找到这些 app.db，请用 --data-dir 或 --ini 指定：")
            for item in candidates:
                print(f"         {item}")
        else:
            print("       用 --find 搜索项目内的 app.db，或用 --data-dir 指定数据目录")
        return 1

    check_ini(ini_path)
    cross_check()

    try:
        return migrate(db_path, data_dir, dry_run=args.dry_run, owner=args.owner)
    except sqlite3.Error as exc:
        print(f"[错误] SQLite 操作失败：{exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
