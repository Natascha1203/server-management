"""迁移脚本 scripts/migrate_rack_request_types.py 的回归测试。

覆盖点：
  1. 「是否上架」/「上下架需求」的老数据能转成「请求类型」并并入备注
  2. 幂等：重复执行不改数据、不重复备份
  3. --dry-run 不动库
  4. 备份是「动手之前」的状态
  5. 下架 pending 请求的 stage 被修正为 implementation
  6. 服务器表里的标签能收进 field_options 池
  7. 脚本自带的权威表头不会和 app/core.py 漂移；trash DDL 与 storage 一致
  8. 找不到 app.db 返回 1
"""
from __future__ import annotations

import json
import sqlite3

from scripts.migrate_rack_request_types import (
    RACK_REQUEST_HEADERS,
    TRASH_DDL,
    migrate,
    table_columns,
)

RACK_TABLE_DDL = """CREATE TABLE rack_requests (
    id TEXT PRIMARY KEY,
    serial_number TEXT NOT NULL,
    request_type TEXT NOT NULL,
    request_data TEXT NOT NULL,
    status TEXT NOT NULL,
    submitter_id TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT '',
    stage_data TEXT NOT NULL DEFAULT '{}',
    reviewer_id TEXT,
    reviewed_at TEXT,
    reject_reason TEXT
)"""


def make_db(path, requests):
    """造一个「是否上架」时代的老库。requests 是 (id, serial, type, data_dict, status, stage) 列表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(RACK_TABLE_DDL)
        for request_id, serial, request_type, data, status, stage in requests:
            conn.execute(
                "INSERT INTO rack_requests (id,serial_number,request_type,request_data,status,submitter_id,submitted_at,stage)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (request_id, serial, request_type, json.dumps(data, ensure_ascii=False), status, "biz1", "2026-10-01 10:00:00", stage),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def read_requests(db):
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute("SELECT request_data,request_type,stage,status FROM rack_requests ORDER BY id").fetchall()
    finally:
        conn.close()
    return [(json.loads(row[0]), row[1], row[2], row[3]) for row in rows]


def make_server_workbook(path, rows):
    from openpyxl import Workbook

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "服务器信息"
    headers = ["序列号", "用途分类", "接入行情", "接入柜台"]
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(str(path))
    wb.close()


def test_legacy_flag_becomes_request_type(tmp_path, capsys):
    data = tmp_path / "data"
    db = make_db(data / "app.db", [
        ("r1", "SN-1", "上架", {"序列号": "SN-1", "是否上架": "是", "是否重装系统": "是", "客户id": "1"}, "approved", "completed"),
        ("r2", "SN-2", "下架", {"序列号": "SN-2", "是否上架": "否", "是否重装系统": "是", "客户id": "1"}, "approved", "completed"),
    ])

    assert migrate(db, data) == 0

    rows = read_requests(db)
    assert [row[1] for row in rows] == ["上架", "下架"]
    for payload, request_type, _stage, _status in rows:
        assert list(payload.keys()) == RACK_REQUEST_HEADERS
        assert payload["请求类型"] == request_type
        assert "是否上架" not in payload
        assert "上下架需求" not in payload
    out = capsys.readouterr().out
    assert "[校验通过]" in out


def test_requirement_note_is_merged_into_memo(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db", [
        ("r1", "SN-1", "上架", {"序列号": "SN-1", "是否上架": "是", "上下架需求": "双路电源", "备注": "已有备注"}, "pending", "allocation"),
        ("r2", "SN-2", "上架", {"序列号": "SN-2", "是否上架": "是", "上下架需求": "靠窗"}, "pending", "allocation"),
    ])

    assert migrate(db, data) == 0

    payloads = {row[0]["序列号"]: row[0] for row in read_requests(db)}
    assert payloads["SN-1"]["备注"] == "已有备注\n上下架需求：双路电源"
    assert payloads["SN-2"]["备注"] == "上下架需求：靠窗"


def test_multi_value_fields_are_normalized(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db", [
        ("r1", "SN-1", "上架", {"序列号": "SN-1", "是否上架": "是", "接入行情": "CTP、UDP"}, "pending", "allocation"),
    ])

    assert migrate(db, data) == 0

    payload = read_requests(db)[0][0]
    assert payload["接入行情"] == "CTP, UDP"
    assert payload["接入柜台"] == ""


def test_offline_pending_stage_is_fixed(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db", [
        ("r1", "SN-1", "下架", {"序列号": "SN-1", "是否上架": "否"}, "pending", "allocation"),
    ])

    assert migrate(db, data) == 0

    _payload, request_type, stage, _status = read_requests(db)[0]
    assert request_type == "下架"
    assert stage == "implementation"


def test_is_idempotent(tmp_path, capsys):
    data = tmp_path / "data"
    db = make_db(data / "app.db", [
        ("r1", "SN-1", "上架", {"序列号": "SN-1", "是否上架": "是"}, "pending", "allocation"),
    ])

    assert migrate(db, data) == 0
    first = read_requests(db)
    assert migrate(db, data) == 0
    assert read_requests(db) == first
    out = capsys.readouterr().out
    assert "[跳过]" in out
    assert len(list((data / "migration-backups").glob("app-*.db"))) == 1


def test_dry_run_touches_nothing(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db", [
        ("r1", "SN-1", "上架", {"序列号": "SN-1", "是否上架": "是"}, "pending", "allocation"),
    ])
    before = db.read_bytes()

    assert migrate(db, data, dry_run=True) == 0

    assert db.read_bytes() == before
    assert not (data / "migration-backups").exists()


def test_backup_is_pre_migration_state(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db", [
        ("r1", "SN-1", "上架", {"序列号": "SN-1", "是否上架": "是"}, "pending", "allocation"),
    ])

    assert migrate(db, data) == 0

    backups = list((data / "migration-backups").glob("app-rack-request-types-*.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(str(backups[0]))
    try:
        raw = conn.execute("SELECT request_data FROM rack_requests").fetchone()[0]
    finally:
        conn.close()
    assert "是否上架" in raw  # 备份是动手之前的样子


def test_server_tags_are_collected_into_field_pool(tmp_path):
    data = tmp_path / "data"
    db = make_db(data / "app.db", [
        ("r1", "SN-1", "上架", {"序列号": "SN-1", "是否上架": "是"}, "pending", "allocation"),
    ])
    make_server_workbook(data / "official" / "服务器信息表.xlsx", [
        ["SN-1", "行情交易、结算", "CTP", "CTP、易盛"],
        ["SN-2", "", "CTP", ""],
    ])

    assert migrate(db, data) == 0

    conn = sqlite3.connect(str(db))
    try:
        pool = {
            (row[0], row[1])
            for row in conn.execute("SELECT field,value FROM field_options WHERE entity='servers'").fetchall()
        }
        assert ("用途分类", "行情交易") in pool
        assert ("用途分类", "结算") in pool
        assert ("接入行情", "CTP") in pool
        assert ("接入柜台", "易盛") in pool
        assert conn.execute("SELECT COUNT(*) FROM field_options WHERE value='CTP' AND field='接入行情'").fetchone()[0] == 1
    finally:
        conn.close()


def test_missing_db_returns_1(tmp_path, monkeypatch):
    from scripts import migrate_rack_request_types as script

    monkeypatch.setattr("sys.argv", ["migrate_rack_request_types.py", "--data-dir", str(tmp_path / "nope")])
    assert script.main() == 1


def test_headers_do_not_drift_from_core():
    from app import core

    assert list(core.RACK_REQUEST_HEADERS) == RACK_REQUEST_HEADERS


def test_trash_ddl_does_not_drift_from_storage():
    from app import storage

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(storage.TRASH_SCHEMA)
        expected = [row[1] for row in conn.execute("PRAGMA table_info(trash)")]
        conn.execute("DROP TABLE trash")
        conn.execute(TRASH_DDL)
        actual = [row[1] for row in conn.execute("PRAGMA table_info(trash)")]
    finally:
        conn.close()
    assert actual == expected
