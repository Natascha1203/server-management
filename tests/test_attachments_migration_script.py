"""迁移脚本 scripts/add_attachments_table.py 的回归测试。

覆盖点：
  1. 空库 / 老库都能建出 attachments 表、索引与 archive 目录
  2. 幂等：重复执行不重复建表、不丢数据、不重复备份
  3. 只有 7 列的早期表能自动补齐 request_id / source，且旧行不丢
  4. 写库前用 backup API 落快照，快照内容是迁移前状态
  5. --dry-run 不改文件、不建目录
  6. 找不到 app.db 返回 1
  7. 脚本自带的权威列定义不会和 app/storage.py 的 ATTACHMENTS_SCHEMA 漂移
"""
from __future__ import annotations

import sqlite3

import pytest

from scripts.add_attachments_table import (
    ARCHIVE_DIRNAME,
    EXPECTED_COLUMNS,
    INDEX_NAME,
    migrate,
    table_columns,
    table_indexes,
)

LEGACY_COLUMNS = [
    "id",
    "serial_number",
    "filename",
    "stored_name",
    "size",
    "uploader",
    "uploaded_at",
]


def make_db(path, tables=("changes", "rack_requests", "settings")):
    """造一个「线上老库」：有业务表，没有 attachments。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        for name in tables:
            conn.execute(f"CREATE TABLE {name} (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    return path


def make_legacy_attachment_db(path, rows=None):
    """造一个早期结构的附件库：只有 7 列，没有索引。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """CREATE TABLE attachments (
                id TEXT PRIMARY KEY,
                serial_number TEXT NOT NULL,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0,
                uploader TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )"""
        )
        for row in rows or []:
            conn.execute(
                "INSERT INTO attachments (id,serial_number,filename,stored_name,size,uploader,uploaded_at)"
                " VALUES (?,?,?,?,?,?,?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()
    return path


def test_creates_table_index_and_archive_dir(tmp_path, capsys):
    data = tmp_path / "data"
    db = make_db(data / "app.db")

    assert migrate(db, data) == 0

    conn = sqlite3.connect(str(db))
    try:
        assert table_columns(conn, "attachments") == EXPECTED_COLUMNS
        assert INDEX_NAME in table_indexes(conn, "attachments")
    finally:
        conn.close()
    assert (data / ARCHIVE_DIRNAME).is_dir()

    out = capsys.readouterr().out
    assert "[校验通过]" in out
    assert "CREATE TABLE attachments" in out


def test_other_tables_are_left_alone(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db")
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO settings (id) VALUES ('keep-me')")
    conn.commit()
    conn.close()

    assert migrate(db, data) == 0

    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT id FROM settings").fetchall() == [("keep-me",)]
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0
    finally:
        conn.close()


def test_backup_snapshot_is_pre_migration_state(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db")

    assert migrate(db, data) == 0

    backups = list((data / "migration-backups").glob("app-*.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(str(backups[0]))
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    assert "attachments" not in names  # 备份是「动手之前」的样子
    assert "settings" in names


def test_is_idempotent_and_keeps_data(tmp_path, capsys):
    data = tmp_path / "data"
    db = make_db(data / "app.db")

    assert migrate(db, data) == 0
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO attachments (id,serial_number,filename,stored_name,size,uploader,uploaded_at)"
        " VALUES ('a1','SN-1','说明.txt','SN-1/a1_说明.txt',12,'admin','2026-10-30 10:00:00')"
    )
    conn.commit()
    conn.close()

    assert migrate(db, data) == 0

    conn = sqlite3.connect(str(db))
    try:
        assert table_columns(conn, "attachments") == EXPECTED_COLUMNS
        assert conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
    finally:
        conn.close()

    out = capsys.readouterr().out
    assert "[跳过]" in out
    assert len(list((data / "migration-backups").glob("app-*.db"))) == 1  # 第二次不再备份


def test_fills_missing_columns_on_legacy_table(tmp_path):
    data = tmp_path / "data"
    db = make_legacy_attachment_db(
        data / "app.db",
        rows=[("a1", "SN-9", "x.pdf", "SN-9/a1_x.pdf", 12, "admin", "2026-10-01 10:00:00")],
    )

    assert migrate(db, data) == 0

    conn = sqlite3.connect(str(db))
    try:
        assert table_columns(conn, "attachments") == EXPECTED_COLUMNS
        assert INDEX_NAME in table_indexes(conn, "attachments")
        rows = conn.execute("SELECT * FROM attachments").fetchall()
    finally:
        conn.close()
    assert rows == [
        ("a1", "SN-9", "x.pdf", "SN-9/a1_x.pdf", 12, "admin", "2026-10-01 10:00:00", "", "")
    ]


def test_dry_run_touches_nothing(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db")
    before = db.read_bytes()

    assert migrate(db, data, dry_run=True) == 0

    assert db.read_bytes() == before
    assert not (data / ARCHIVE_DIRNAME).exists()
    assert not (data / "migration-backups").exists()


def test_existing_archive_dir_is_reused(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db")
    (data / ARCHIVE_DIRNAME).mkdir(parents=True)
    (data / ARCHIVE_DIRNAME / "SN-1").mkdir()

    assert migrate(db, data) == 0

    assert (data / ARCHIVE_DIRNAME / "SN-1").is_dir()  # 不该清空已有资料目录


def test_warns_about_older_schema_but_leaves_it_alone(tmp_path, capsys):
    """库来自更早版本时给提示，但补列由应用启动负责，脚本不代劳。"""
    data = tmp_path / "data"
    db = make_db(data / "app.db")  # rack_requests 只有 id 一列

    assert migrate(db, data) == 0

    out = capsys.readouterr().out
    assert "rack_requests 缺列 stage" in out
    conn = sqlite3.connect(str(db))
    try:
        assert [row[1] for row in conn.execute("PRAGMA table_info(rack_requests)")] == ["id"]
    finally:
        conn.close()


def test_missing_db_returns_1(tmp_path, monkeypatch):
    from scripts import add_attachments_table as script

    monkeypatch.setattr("sys.argv", ["add_attachments_table.py", "--data-dir", str(tmp_path / "nope")])
    assert script.main() == 1


def test_schema_does_not_drift_from_storage_module():
    """脚本自带权威结构是为了防旧代码干扰；但它必须与运行时定义一致。"""
    from app import storage

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(storage.ATTACHMENTS_SCHEMA)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(attachments)")]
    finally:
        conn.close()
    assert columns == EXPECTED_COLUMNS


def test_runtime_columns_parser_reads_storage_file():
    from scripts.add_attachments_table import runtime_columns

    columns, why = runtime_columns()
    assert why == ""
    assert columns == EXPECTED_COLUMNS
