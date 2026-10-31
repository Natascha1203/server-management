#!/usr/bin/env python3
"""结构迁移脚本：服务器表新增「备注」列。

背景
    服务器信息表需要一列「备注」，其默认值应能直接沿用上下架请求里填写的「备注」
    （请求经机位IP分配 / 具体实施落库为服务器记录时自动带入），同时也可在服务器编辑弹窗、
    机位IP分配表单里手工修改。

迁移内容（仅数据文件结构；应用代码只适配新结构，不做旧数据兼容）
    1. 服务器表（<data_dir>/official/服务器信息表.xlsx）
         - 表头末尾追加「备注」列，已有行该列留空
         - 已存在「备注」列则跳过（幂等）
    2. 其余 xlsx / SQLite（<data_dir>/app.db）—— 无需改动
         - 上下架请求表头（RACK_REQUEST_HEADERS）本就有「备注」，请求侧不受影响
    3. 历史归档（versions/、backups/、migration-backups/）属于只读快照，不参与运行时读取，
       本脚本不改动它们

两个容易踩的坑（本脚本已针对性处理）
    a) 应用在每次启动时都会执行 storage.sync_official_workbook_headers()，
       用 app/core.py 里 ENTITIES 的表头「强制重写」官方表。
       如果服务器上的 app/core.py 还是旧版本（表头里没有「备注」），
       那么即使本脚本写入了「备注」，重启服务后也会被它删掉 —— 请先部署最新代码。
    b) 若脚本从 app.core 读取目标表头，遇到上面这种旧代码时，会出现
       「现有表头 == 目标表头」的假相等，从而打印「表头已是最新（含备注）」并静默跳过，
       实际什么都没做。本脚本**自带权威表头**，不再依赖 app.core，
       只把 app.core 的值拿来做交叉校验并给出告警。

用法（在项目根目录执行）
    python scripts/add_server_remark_column.py                  # 迁移默认数据目录
    python scripts/add_server_remark_column.py --dry-run        # 只打印将要执行的改动
    python scripts/add_server_remark_column.py --find           # 只搜索项目内所有 服务器信息表.xlsx
    python scripts/add_server_remark_column.py --target /abs/path/服务器信息表.xlsx
    python scripts/add_server_remark_column.py --data-dir /srv/sm/data

退出码
    0 成功（或已是最新/仅 dry-run）；1 找不到文件或表为空；2 写入后校验失败；3 环境缺 openpyxl
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SERVER_FILENAME = "服务器信息表.xlsx"
SERVER_SHEET = "服务器信息"
REMARK = "备注"

# 本脚本的权威目标表头（含「备注」）。
# 刻意不从这里读 app.core：服务器上的 app/core.py 可能是旧版本，会导致静默跳过。
TARGET_HEADERS = [
    "序列号", "品牌", "型号", "客户id", "客户名称", "负责人", "归属单位", "CPU型号", "网卡",
    "管理ip", "交易ip", "带外ip", "管理密码", "带外密码", "服务器成本(万)", "用途分类", "用途",
    "防火墙白名单", "资源申请编号", "是否液冷", "所在机房", "所属机柜", "所属机位", "上架时间",
    "接入行情", "接入柜台", "备注",
]


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def load_runtime_headers():
    """读取 app.core 里的服务器表头，仅用于交叉校验；不可用时返回 None。"""
    try:
        from app.core import ENTITIES

        return list(ENTITIES["servers"]["headers"])
    except Exception as exc:  # pragma: no cover - 仅在环境不完整时触发
        print(f"[提示] 未能导入 app.core（{type(exc).__name__}: {exc}），跳过与运行时代码的交叉校验\n")
        return None


def cross_check(runtime):
    if runtime is None:
        return
    if runtime == TARGET_HEADERS:
        print(f"[校验] 与 app.core.ENTITIES['servers']['headers'] 一致（{len(runtime)} 列）\n")
        return
    print("[警告] 本脚本的目标表头与服务器上 app/core.py 的表头不一致：")
    print(f"       脚本目标（{len(TARGET_HEADERS)} 列）：{TARGET_HEADERS}")
    print(f"       运行时值（{len(runtime)} 列）：{runtime}")
    if REMARK not in runtime:
        print("       ↑ 运行时表头里没有「备注」——服务器上的 app/core.py 还是旧版本。")
        print("         应用启动时会用这份旧表头重写官方表（sync_official_workbook_headers），")
        print("         所以即便本脚本写入了「备注」，重启服务后也会被删掉。请先部署最新代码。")
    print("       本脚本仍按上面的『脚本目标』执行。\n")


def resolve_data_dir(args) -> Path:
    if args.data_dir:
        return Path(args.data_dir).expanduser()
    ini = Path(args.ini).expanduser() if args.ini else PROJECT_ROOT / "app.ini"
    if ini.exists():
        import configparser

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read(ini, encoding="utf-8")
        text = parser.get("app", "data_dir", fallback="").strip()
        if text:
            path = Path(text).expanduser()
            return path if path.is_absolute() else ini.resolve().parent / path
        print(f"[警告] {ini} 未配置 app.data_dir，回退到 {PROJECT_ROOT / 'data'}")
    else:
        print(f"[警告] 找不到配置文件 {ini}，回退到 {PROJECT_ROOT / 'data'}")
    return PROJECT_ROOT / "data"


def read_sheet_headers(path: Path):
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb[SERVER_SHEET] if SERVER_SHEET in wb.sheetnames else wb[wb.sheetnames[0]]
        for row in ws.iter_rows(max_row=1, values_only=True):
            return [clean(v) for v in row]
        return []
    finally:
        wb.close()


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
        for item in root.rglob(SERVER_FILENAME):
            resolved = item.resolve()
            if resolved in seen or resolved.parent.parent.name in {"versions", "backups"}:
                continue
            seen.add(resolved)
            hits.append(resolved)
            if len(hits) >= limit:
                return sorted(hits)
    return sorted(hits)


def migrate_server_sheet(path: Path, headers, dry_run: bool = False) -> int:
    from openpyxl import load_workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if not path.exists():
        print(f"[错误] 找不到文件：{path}")
        candidates = find_candidates([path.parent.parent, PROJECT_ROOT])
        if candidates:
            print("       项目里找到这些同名文件，请用 --target 指定：")
            for item in candidates:
                print(f"         {item}")
        else:
            print("       用 --find 搜索项目内全部同名文件，或用 --data-dir / --target 指定位置")
        return 1

    wb = load_workbook(path)
    try:
        if SERVER_SHEET in wb.sheetnames:
            ws = wb[SERVER_SHEET]
        else:
            ws = wb[wb.sheetnames[0]]
            print(f"[提示] 未找到工作表「{SERVER_SHEET}」，改用第一张表「{ws.title}」")

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            print(f"[错误] {path.name} 是空表（没有表头行）")
            return 1

        old_headers = [clean(v) for v in rows[0]]
        while old_headers and old_headers[-1] == "":
            old_headers.pop()
        data_rows = [values for values in rows[1:] if any(clean(v) for v in values)]

        missing = [h for h in headers if h not in old_headers]
        extra = [h for h in old_headers if h not in headers]

        if old_headers == list(headers):
            print(f"[跳过] {path.name} 表头已是最新（{len(headers)} 列，末列「{headers[-1]}」），无需迁移")
            return 0

        print(f"  现有表头（{len(old_headers)} 列）：{old_headers}")
        print(f"  目标表头（{len(headers)} 列）：{list(headers)}")
        print(f"  需新增列：{missing or '（无）'}")
        if extra:
            print(f"  目标中不存在的旧列：{extra}（其内容会被丢弃，请先确认）")
        print(f"  现有数据行：{len(data_rows)} 行")

        if dry_run:
            print("[dry-run] 未写入文件")
            return 0

        # 备份原文件
        backups_dir = path.parent.parent / "migration-backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = backups_dir / f"{path.stem}-{stamp}{path.suffix}"
        backup.write_bytes(path.read_bytes())
        print(f"  已备份原文件 → {backup}")

        # 逐格写入：已存在的单元格只改值，样式保留；新增列才会创建单元格
        for col, header in enumerate(headers, 1):
            cell = ws.cell(1, col)
            cell.value = header
            cell.font = Font(bold=True)
        for r_idx, values in enumerate(data_rows, 2):
            source = {h: v for h, v in zip(old_headers, values) if h}
            for col, header in enumerate(headers, 1):
                ws.cell(r_idx, col).value = clean(source.get(header, ""))

        # 清掉目标范围之外的历史残留，否则应用启动时会认为表头不一致而整表重写
        if ws.max_row > len(data_rows) + 1:
            ws.delete_rows(len(data_rows) + 2, ws.max_row - (len(data_rows) + 1))
        if ws.max_column > len(headers):
            ws.delete_cols(len(headers) + 1, ws.max_column - len(headers))
        ws.column_dimensions[get_column_letter(len(headers))].width = 30

        wb.save(path)
        print(f"  已写入：表头 {len(headers)} 列，数据 {len(data_rows)} 行")
    finally:
        wb.close()

    # 回读校验，避免「以为写了其实没写」
    actual = read_sheet_headers(path)
    if actual == list(headers):
        print(f"[校验通过] 重新读取 {path.name}：{len(actual)} 列，末列「{actual[-1]}」\n")
        return 0
    print(f"[校验失败] 重新读取到的表头是：{actual}")
    print("           文件可能被其他进程占用/覆盖，或写入未生效\n")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="服务器表新增「备注」列")
    parser.add_argument("--data-dir", default="", help="数据目录，默认取 app.ini 的 data_dir")
    parser.add_argument("--ini", default="", help="配置文件路径，默认项目根 app.ini")
    parser.add_argument("--target", default="", help="直接指定 服务器信息表.xlsx 路径（跳过 data_dir 推导）")
    parser.add_argument("--find", action="store_true", help="只搜索项目内全部 服务器信息表.xlsx，不修改任何文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的改动，不写入")
    args = parser.parse_args()

    try:
        import openpyxl
    except ImportError as exc:
        print(f"[错误] 当前 Python 环境缺少 openpyxl：{exc}")
        print(f"       当前解释器：{sys.executable}")
        print("       请改用运行本服务的解释器执行本脚本（例如 conda 环境里的 python）")
        return 3

    print(f"Python：{sys.executable}")
    print(f"openpyxl：{openpyxl.__version__}\n")

    data_dir = resolve_data_dir(args)
    official_dir = data_dir / "official"
    print(f"数据目录：{data_dir}")
    print(f"官方表目录：{official_dir}（存在：{official_dir.exists()}）\n")

    if args.find:
        found = find_candidates([data_dir, PROJECT_ROOT])
        print(f"在 {data_dir} 与项目根下找到 {len(found)} 个「{SERVER_FILENAME}」：")
        for item in found:
            stat = item.stat()
            mark = "（官方表）" if item.parent == official_dir else ""
            print(f"  {item}{mark}  {stat.st_size} 字节  "
                  f"{datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")
        print("\n（versions/、backups/ 下的历史快照已略过）")
        return 0

    cross_check(load_runtime_headers())

    if args.target:
        path = Path(args.target).expanduser()
        print("目标文件：")
        describe(path)
    else:
        path = official_dir / SERVER_FILENAME
        print("目标文件：")
        if not describe(path):
            print(f"  文件不存在")
    print()

    if not path.exists():
        candidates = find_candidates([data_dir, PROJECT_ROOT])
        if candidates:
            print("项目里找到这些同名文件，请用 --target 指定：")
            for item in candidates:
                print(f"  {item}")
        else:
            print("用 --find 搜索项目内全部同名文件，或用 --data-dir / --target 指定位置")
        return 1

    return migrate_server_sheet(path, TARGET_HEADERS, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
