"""备份快照的扫描与全量恢复。

快照根目录（设置的 `snapshot_dir`，默认 `data/backups`）下可能同时躺着两类东西：

    data_20261103.zip            运维 cron 每天打的包（含 data/official/*.xlsx、app.db、users.json）
    manual/B20261118-120000-0001 系统「手动备份」写出的目录（三张 xlsx + app.db + users.json）
    B20261118-120000-0001        旧版手动备份（还没搬进 manual/ 时留下的），只读

这里只做三件事：扫描成列表、预览单条内容、把选中那条的三张正式表恢复回去。
恢复前一定先自己再备份一份（manual/pre-restore），所以恢复本身是可撤销的。

不恢复 users.json：账号 / 密码 / token 是 STATE["config"] 里的内存缓存，
文件级覆盖不会生效，还会让当前登录态和权限对不上。

app.db 可选恢复（默认恢复）：不换文件，而是在一个事务里按表覆盖，避免半新半旧
和「要重启才生效」的问题；`settings` 和 `versions` 两张表始终保留当前值
（前者含本页配置本身，后者指向磁盘上可能已被轮转删除的快照目录）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from . import storage
from .core import ENTITIES, ROLE_ADMIN, clean_value, now_text

# 恢复快照到 app.db 时按表覆盖的范围；settings / versions 不在其中（见模块说明）
DB_RESTORE_TABLES = ("changes", "rack_requests", "attachments", "trash", "field_options")

# 快照里必须存在的实体（缺一张就不算完整快照）
REQUIRED_ENTITIES = ("customers", "cabinets", "servers")


class SnapshotError(ValueError):
    """快照不可用 / 恢复被拒绝。路由层统一转 400。"""


def _official_filenames():
    return {entity: ENTITIES[entity]["filename"] for entity in REQUIRED_ENTITIES}


def _match_member(names, filename):
    """在 zip 成员名里找某个 xlsx：优先 official/ 下的，其次任意路径结尾匹配。"""
    exact = [name for name in names if not name.endswith("/") and Path(name).name == filename]
    if not exact:
        return ""
    official = [name for name in exact if "/official/" in "/" + name]
    return sorted(official or exact, key=len)[0]


def _dir_files(path):
    """目录快照里的三张 xlsx：优先根目录，其次 official/ 子目录（旧包结构）。"""
    found = {}
    for entity, filename in _official_filenames().items():
        for candidate in (path / filename, path / "official" / filename):
            if candidate.is_file():
                found[entity] = str(candidate)
                break
    return found


def _zip_files(zip_path):
    found = {}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise SnapshotError(f"压缩包无法读取：{exc}") from exc
    for entity, filename in _official_filenames().items():
        member = _match_member(names, filename)
        if member:
            found[entity] = member
    has_db = any(Path(name).name == "app.db" for name in names)
    has_users = any(Path(name).name == "users.json" for name in names)
    return found, has_db, has_users, names


def _relative_name(path, root):
    return path.relative_to(root).as_posix()


def _dir_size(path):
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _fmt_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def scan():
    """扫描快照根目录，返回 `{root, exists, error, items}`。

    只认这几类名字：根目录下的 *.zip（cron）、manual/B*（人工备份）、
    auto/B*（写操作前自动备份）、根目录下的 B*（旧版本遗留）。
    其他文件忽略，避免把无关文件列进来或被误恢复/误删。
    """
    try:
        root = storage.ensure_snapshot_root()
    except ValueError as exc:
        return {"root": str(storage.snapshot_root()), "exists": False, "error": str(exc), "items": []}
    items = []
    subdirs = storage.backup_dirs()
    for entry in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if entry.name.startswith("."):
            continue
        if entry.is_file() and entry.suffix.lower() == ".zip":
            items.append(_describe_zip(entry, root, origin="cron"))
        elif entry.is_dir() and entry.name in subdirs:
            items.extend(_describe_subdir(entry, root, origin=entry.name))
        elif entry.is_dir() and entry.name.startswith("B"):
            items.append(_describe_dir(entry, root, origin="legacy"))
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return {"root": str(root), "exists": True, "error": "", "items": items}


def _describe_subdir(path, root, origin):
    out = []
    for child in sorted(path.iterdir(), key=lambda p: p.name, reverse=True):
        if child.is_dir() and child.name.startswith("B"):
            out.append(_describe_dir(child, root, origin=origin))
    return out


def _describe_zip(path, root, origin):
    item = {
        "name": _relative_name(path, root),
        "kind": "zip",
        "origin": origin,
        "size": path.stat().st_size,
        "mtime": _fmt_time(path.stat().st_mtime),
        "has_db": False,
        "has_users": False,
        "valid": False,
        "errors": [],
        "record_counts": {},
    }
    try:
        found, has_db, has_users, _names = _zip_files(path)
    except SnapshotError as exc:
        item["errors"] = [str(exc)]
        return item
    item["has_db"] = has_db
    item["has_users"] = has_users
    missing = [ENTITIES[entity]["label"] for entity in REQUIRED_ENTITIES if entity not in found]
    if missing:
        item["errors"] = ["缺少" + "、".join(missing)]
        return item
    item["valid"] = True
    return item


def _describe_dir(path, root, origin):
    stat = path.stat()
    item = {
        "name": _relative_name(path, root),
        "kind": "dir",
        "origin": origin,
        "size": _dir_size(path),
        "mtime": _fmt_time(stat.st_mtime),
        "has_db": (path / "app.db").is_file(),
        "has_users": (path / "users.json").is_file(),
        "valid": False,
        "errors": [],
        "record_counts": {},
    }
    found = _dir_files(path)
    missing = [ENTITIES[entity]["label"] for entity in REQUIRED_ENTITIES if entity not in found]
    if missing:
        item["errors"] = ["缺少" + "、".join(missing)]
        return item
    item["valid"] = True
    return item


def _find_entry(name):
    """按名字取快照（名字必须是扫描结果里出现过的，不能拼路径）。"""
    wanted = clean_value(name)
    if not wanted:
        raise SnapshotError("请选择要恢复的快照")
    for item in scan()["items"]:
        if item["name"] == wanted:
            return item
    raise SnapshotError(f"快照 {wanted} 不存在或已不可用")


def delete_snapshot(name):
    """删除一个备份包（zip）或一份备份目录（B*），返回删除后的剩余条数。

    只允许删扫描结果里出现过的名字：这样既挡掉 `../` 之类路径拼接，也不会误删
    快照目录里的无关文件（扫描器只认 zip / manual/B* / auto/B* / B*）。
    """
    item = _find_entry(name)
    root = storage.snapshot_root()
    target = root / item["name"]
    # 再确认一次：符号链接可能把路径指到根目录之外，删之前必须校验真实路径
    try:
        resolved = target.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        raise SnapshotError(f"拒绝删除 {item['name']}：解析后的路径不在快照目录内") from None
    if resolved == root.resolve():
        raise SnapshotError("拒绝删除快照根目录本身")
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        raise SnapshotError(f"删除 {item['name']} 失败：{exc.strerror or exc}") from exc
    # 顺手清掉空掉的 manual/ auto/ 子目录，但根目录保留
    parent = target.parent.resolve()
    if parent != root.resolve() and parent.is_dir():
        try:
            if not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
    remaining = len(scan()["items"])
    return {"snapshot": item["name"], "kind": item["kind"], "remaining": remaining}


def _open_workbook(path_or_file):
    return load_workbook(path_or_file, data_only=True, read_only=True)


def _headers_and_rows(path_or_file):
    workbook = _open_workbook(path_or_file)
    try:
        sheet = workbook[workbook.sheetnames[0]]
        rows = sheet.iter_rows(values_only=True)
        header_row = next(rows, None)
        headers = [clean_value(value) for value in (header_row or [])]
        count = sum(1 for row in rows if any(clean_value(value) for value in row))
        return headers, count
    finally:
        workbook.close()


def _materialize(item, workdir):
    """把快照里的三张 xlsx 落到 workdir，返回 `{entity: path}` 与 db 路径（若有）。"""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    root = storage.snapshot_root()
    source = root / item["name"]
    if not source.exists():
        raise SnapshotError(f"快照 {item['name']} 已不存在")
    extracted = {}
    db_path = ""
    if item["kind"] == "zip":
        try:
            with zipfile.ZipFile(source) as archive:
                names = archive.namelist()
                for entity, filename in _official_filenames().items():
                    member = _match_member(names, filename)
                    if not member:
                        raise SnapshotError(f"快照里缺少{ENTITIES[entity]['label']}")
                    target = workdir / filename
                    with archive.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted[entity] = target
                db_member = next((n for n in names if Path(n).name == "app.db"), "")
                if db_member:
                    db_path = str(workdir / "app.db")
                    with archive.open(db_member) as src, Path(db_path).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        except zipfile.BadZipFile as exc:
            raise SnapshotError(f"压缩包无法读取：{exc}") from exc
    else:
        found = _dir_files(source)
        for entity in REQUIRED_ENTITIES:
            if entity not in found:
                raise SnapshotError(f"快照里缺少{ENTITIES[entity]['label']}")
            target = workdir / ENTITIES[entity]["filename"]
            shutil.copyfile(Path(found[entity]), target)
            extracted[entity] = target
        if item["has_db"]:
            db_path = str(workdir / "app.db")
            shutil.copyfile(source / "app.db", db_path)
    return extracted, db_path


def preview(name):
    """单条快照的详情：三张表的行数、表头差异、是否带 db。"""
    item = _find_entry(name)
    if not item["valid"]:
        raise SnapshotError("；".join(item["errors"]) or "快照不可用")
    workdir = Path(tempfile.mkdtemp(prefix="snap-preview-", dir=storage.STATE["temp_dir"]))
    try:
        files, db_path = _materialize(item, workdir)
        detail = {**item, "tables": [], "missing_columns": {}, "extra_columns": {}, "db_tables": []}
        for entity in REQUIRED_ENTITIES:
            meta = ENTITIES[entity]
            headers, count = _headers_and_rows(files[entity])
            missing = [header for header in meta["headers"] if header not in headers]
            extra = [header for header in headers if header and header not in meta["headers"]]
            detail["tables"].append({
                "entity": entity,
                "label": meta["label"],
                "filename": meta["filename"],
                "records": count,
                "missing": missing,
                "extra": extra,
            })
            if missing:
                detail["missing_columns"][entity] = missing
            if extra:
                detail["extra_columns"][entity] = extra
            item["record_counts"][entity] = count
        if db_path:
            detail["db_tables"] = _db_table_names(db_path)
        detail["restorable"] = not detail["missing_columns"]
        return detail
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _db_table_names(db_path):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return sorted(name for (name,) in rows if not name.startswith("sqlite_"))
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _restore_db(db_path, conn):
    """按表覆盖 app.db（不换文件、不需要重启）。返回实际覆盖的表名。

    先把快照库的整表读进内存再写主库，而不是 ATTACH：ATTACH 在已有写事务里
    容易 DETACH 失败（database is locked），而且快照体积很小，读内存没有成本。
    """
    payload = {}
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        available = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in DB_RESTORE_TABLES:
            if table not in available:
                continue
            snap_cols = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
            payload[table] = (snap_cols, source.execute(f'SELECT * FROM "{table}"').fetchall())
    except sqlite3.Error as exc:
        raise SnapshotError(f"快照里的 app.db 无法读取：{exc}") from exc
    finally:
        source.close()

    touched = []
    for table, (snap_cols, rows) in payload.items():
        main_cols = [row[1] for row in conn.execute(f'PRAGMA main.table_info("{table}")')]
        # 只写两边都有的列：快照缺的列留空（默认值），快照多出来的列忽略
        columns = [column for column in main_cols if column in snap_cols]
        if not columns:
            continue
        index = {name: position for position, name in enumerate(snap_cols)}
        column_sql = ",".join(f'"{column}"' for column in columns)
        conn.execute(f'DELETE FROM main."{table}"')
        conn.executemany(
            f'INSERT INTO main."{table}" ({column_sql}) VALUES ({",".join("?" * len(columns))})',
            [tuple(row[index[column]] for column in columns) for row in rows],
        )
        _sync_autoincrement(conn, table)
        touched.append(table)
    return touched


def _sync_autoincrement(conn, table):
    """按表覆盖后修正 sqlite_sequence，避免自增主键和已恢复的行撞车。"""
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
        if not exists:
            return
        current = conn.execute(f'SELECT COALESCE(MAX(id),0) FROM main."{table}"').fetchone()[0]
        conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
        conn.execute("INSERT INTO sqlite_sequence (name,seq) VALUES (?,?)", (table, current))
    except sqlite3.Error:
        # 该表没有自增主键（或不是 INTEGER PRIMARY KEY），无需处理
        return


def _drop_pending(conn, actor, reason="数据已恢复到快照，申请作废"):
    changes = conn.execute(
        "UPDATE changes SET status='rejected', reject_reason=?, reviewer=?, reviewed_at=?"
        " WHERE status='pending'",
        (reason, actor, storage.now_text()),
    ).rowcount
    requests = conn.execute(
        "UPDATE rack_requests SET status='rejected', stage='rejected', reject_reason=?,"
        " reviewer_id=?, reviewed_at=? WHERE status='pending'",
        (reason, actor, storage.now_text()),
    ).rowcount
    return {"changes": changes, "rack_requests": requests}


def _consistency_report():
    """恢复后的一致性体检：只报告，不自动改数据。"""
    servers = {clean_value(row.get("序列号")) for row in storage.read_records("servers")}
    with storage.db() as conn:
        trash = [clean_value(row["serial_number"]) for row in conn.execute("SELECT serial_number FROM trash")]
        attachments = [
            clean_value(row["serial_number"]) for row in conn.execute("SELECT serial_number FROM attachments")
        ]
    return {
        "trash_back_in_servers": sorted({serial for serial in trash if serial and serial in servers}),
        "attachment_orphans": sorted({serial for serial in attachments if serial and serial not in servers}),
    }


def restore(name, actor="", include_db=True, drop_pending=True):
    """把选中快照的三张正式表写回 data/official，可选连带 app.db。

    顺序是「先自备份 → 再解包校验 → 再原子替换」，任何一步失败都不会改动正式表。
    """
    item = _find_entry(name)
    if not item["valid"]:
        raise SnapshotError("；".join(item["errors"]) or "快照不可用")
    root = storage.snapshot_root()
    if not item["has_db"] and include_db:
        # 没有 db 的快照只能恢复 Excel，静默降级会让调用方以为全恢复了
        include_db = False
    workdir = Path(tempfile.mkdtemp(prefix="snap-restore-", dir=storage.STATE["temp_dir"]))
    backed_up = ""
    try:
        files, db_path = _materialize(item, workdir)
        # ① 表头校验：快照必须覆盖当前正式表的所有列，否则恢复完程序读不到字段
        missing = {}
        for entity in REQUIRED_ENTITIES:
            headers, _count = _headers_and_rows(files[entity])
            gap = [header for header in ENTITIES[entity]["headers"] if header not in headers]
            if gap:
                missing[ENTITIES[entity]["label"]] = gap
        if missing:
            detail = "；".join(f"{label}缺列：{'、'.join(cols)}" for label, cols in missing.items())
            raise SnapshotError(f"快照与当前版本不兼容，未做任何修改（{detail}）")

        # ② 恢复前自备份：这是唯一能「撤销这次恢复」的保险，失败就不许继续
        try:
            backed_up = storage.create_backup("pre-restore").name
        except OSError as exc:
            raise SnapshotError(f"恢复前备份失败，已中止：{exc}") from exc

        # ③ 原子替换：先落到 temp 再 os.replace，保证不会出现半新半旧的文件
        staged = workdir / "staged"
        staged.mkdir(parents=True, exist_ok=True)
        for entity in REQUIRED_ENTITIES:
            meta = ENTITIES[entity]
            temp_target = staged / meta["filename"]
            shutil.copyfile(files[entity], temp_target)
            os.replace(temp_target, storage.STATE["official_dir"] / meta["filename"])
        storage.sync_official_workbook_headers()

        db_tables = []
        dropped = {"changes": 0, "rack_requests": 0}
        try:
            with storage.db() as conn:
                if include_db and db_path:
                    db_tables = _restore_db(db_path, conn)
                if drop_pending:
                    dropped = _drop_pending(conn, actor)
                    storage.sync_pending_excel(conn)
                storage.write_settings({"last_restore_at": storage.now_text()}, conn=conn)
        except (sqlite3.Error, SnapshotError) as exc:
            # 三张表已经换好、db 没能恢复：事务已回滚，说清楚现状，别让人以为整体失败
            raise SnapshotError(
                f"三张正式表已恢复到 {item['name']}，但 app.db 恢复失败：{exc}；"
                f"如需回到恢复前状态，请选快照 {backed_up} 再恢复一次"
            ) from exc
        report = _consistency_report()
        return {
            "snapshot": item["name"],
            "backup_before_restore": backed_up,
            "tables": [
                {
                    "label": ENTITIES[entity]["label"],
                    "filename": ENTITIES[entity]["filename"],
                    "records": len(storage.read_records(entity)),
                }
                for entity in REQUIRED_ENTITIES
            ],
            "db_restored": db_tables,
            "db_skipped": include_db and not db_path,
            "dropped_pending": dropped,
            **report,
            "root": str(root),
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def require_admin(user):
    if user.get("身份") != ROLE_ADMIN:
        raise SnapshotError("只有管理人员可以恢复数据")
