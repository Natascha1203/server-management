"""迁移脚本 scripts/add_server_remark_column.py 的回归测试。

重点覆盖两个曾经踩过的坑：
  1. 旧版脚本从 app.core 读目标表头，遇到服务器上「旧的 app/core.py」时会出现
     假相等，于是打印「表头已是最新（含备注）」并静默跳过 —— 实际什么都没做。
     现在脚本自带权威表头（TARGET_HEADERS），即便 app.core 是旧版也照常迁移。
  2. 写入后必须回读校验，避免「以为写了其实没写」。
"""
from __future__ import annotations

import pytest
from openpyxl import Workbook, load_workbook

from scripts.add_server_remark_column import (
    REMARK,
    SERVER_FILENAME,
    SERVER_SHEET,
    TARGET_HEADERS,
    migrate_server_sheet,
    read_sheet_headers,
)

LEGACY_HEADERS = [h for h in TARGET_HEADERS if h != REMARK]

ROWS = [
    {"序列号": "SN-1", "客户名称": "客户一", "管理ip": "10.1.1.1", "所属机位": "1-2"},
    {"序列号": "SN-2", "客户名称": "客户二", "管理ip": "10.1.1.2", "所属机位": "3-4"},
]


def make_sheet(path, headers, rows, sheet=SERVER_SHEET):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers)
    for row in rows:
        ws.append([row.get(h, "") for h in headers])
    wb.save(path)
    wb.close()
    return path


def read_rows(path):
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb[SERVER_SHEET] if SERVER_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        return [tuple("" if v is None else str(v) for v in row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


@pytest.fixture
def legacy_file(tmp_path):
    return make_sheet(tmp_path / "data" / "official" / SERVER_FILENAME, LEGACY_HEADERS, ROWS)


def test_target_headers_end_with_remark():
    assert TARGET_HEADERS[-1] == REMARK
    assert TARGET_HEADERS.count(REMARK) == 1
    assert REMARK not in LEGACY_HEADERS


def test_adds_remark_column_without_touching_data(legacy_file):
    assert read_sheet_headers(legacy_file) == LEGACY_HEADERS  # 迁移前确实是 26 列

    assert migrate_server_sheet(legacy_file, TARGET_HEADERS) == 0

    assert read_sheet_headers(legacy_file) == TARGET_HEADERS
    rows = read_rows(legacy_file)
    assert len(rows) == len(ROWS) + 1
    header_index = {h: i for i, h in enumerate(rows[0])}
    for expected, actual in zip(ROWS, rows[1:]):
        for field, value in expected.items():
            assert actual[header_index[field]] == value
        assert actual[header_index[REMARK]] == ""


def test_backup_is_written_before_migration(legacy_file):
    assert migrate_server_sheet(legacy_file, TARGET_HEADERS) == 0
    backups = list(legacy_file.parent.parent.glob("migration-backups/*.xlsx"))
    assert len(backups) == 1
    assert read_sheet_headers(backups[0]) == LEGACY_HEADERS  # 备份是迁移前的旧表


def test_is_idempotent(legacy_file):
    assert migrate_server_sheet(legacy_file, TARGET_HEADERS) == 0
    assert migrate_server_sheet(legacy_file, TARGET_HEADERS) == 0
    headers = read_sheet_headers(legacy_file)
    assert headers == TARGET_HEADERS
    assert headers.count(REMARK) == 1
    assert len(read_rows(legacy_file)) == len(ROWS) + 1


def test_stale_app_core_cannot_cause_silent_skip(legacy_file):
    """即便传入的运行时表头是旧版（没有备注），也不影响脚本按权威表头迁移。

    旧版脚本会拿 app.core 的表头当目标，与旧文件正好相等 → 静默跳过；
    现在目标是脚本自带的常量，所以这里必须真的补上「备注」。
    """
    assert REMARK not in LEGACY_HEADERS
    assert migrate_server_sheet(legacy_file, TARGET_HEADERS) == 0
    assert read_sheet_headers(legacy_file)[-1] == REMARK


def test_dry_run_does_not_touch_file(legacy_file):
    before = legacy_file.read_bytes()
    assert migrate_server_sheet(legacy_file, TARGET_HEADERS, dry_run=True) == 0
    assert legacy_file.read_bytes() == before
    assert not (legacy_file.parent.parent / "migration-backups").exists()


def test_missing_file_returns_1(tmp_path):
    missing = tmp_path / "data" / "official" / SERVER_FILENAME
    assert migrate_server_sheet(missing, TARGET_HEADERS) == 1


def test_extra_legacy_column_is_removed(legacy_file):
    legacy_file.unlink()
    headers = LEGACY_HEADERS + ["临时列"]
    make_sheet(legacy_file, headers, [{**ROWS[0], "临时列": "垃圾"}] + ROWS[1:])
    assert migrate_server_sheet(legacy_file, TARGET_HEADERS) == 0
    assert read_sheet_headers(legacy_file) == TARGET_HEADERS
    assert len(read_sheet_headers(legacy_file)) == len(TARGET_HEADERS)


def test_falls_back_to_first_sheet_when_named_sheet_missing(tmp_path):
    path = make_sheet(tmp_path / "data" / "official" / SERVER_FILENAME, LEGACY_HEADERS, ROWS, sheet="Sheet1")
    assert migrate_server_sheet(path, TARGET_HEADERS) == 0
    assert read_sheet_headers(path) == TARGET_HEADERS
