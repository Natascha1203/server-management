"""把「期权交易与柜台设备配置表」整理成服务器表可导入的 xlsx。

源表（单 sheet）结构：第 2 行是表头，14 列，第 3 行起是设备行；A 列名为「设备品牌」
但实际填的是 CPU 型号，B 列是整机型号（个别行两列写反了，这里按语义归位）。

产出：
  <out>/服务器表导入-期权交易与柜台设备.xlsx
      sheet「服务器信息」= 23 行可直接导入（缺序列号的那台单独放 sheet「待补录」，
      因为序列号是主键、留空会让整批导入被拒）

客户「上海分公司自有」在生产客户表里已存在（客户id 00000001），所以默认不再生成补录文件；
确实需要新建客户时才加 --write-customer。

用法：
  # 推荐：带 --snapshot（从生产库导出的 customers/cabinets/servers 三个接口的 JSON），
  # 生成时就把客户、机柜、U 位占用都按生产实况校验一遍
  python scripts/build_server_import_from_option_devices.py \
      --source "C:/Users/admin/Documents/期权交易与柜台设备配置表.xlsx" \
      --outdir "C:/Users/admin/Documents" \
      --customer-id 00000001 --customer-name 上海分公司自有 \
      --snapshot data/temp/remote_snapshot.json
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

SERVER_HEADERS = [
    "序列号", "品牌", "型号", "客户id", "客户名称", "负责人", "归属单位", "CPU型号", "网卡",
    "管理ip", "交易ip", "带外ip", "管理密码", "带外密码", "服务器成本(万)", "用途分类", "用途",
    "防火墙白名单", "资源申请编号", "是否液冷", "所在机房", "所属机柜", "所属机位", "上架时间",
    "接入行情", "接入柜台", "备注",
]
CUSTOMER_HEADERS = ["客户id", "名称", "资产", "月产出", "业务单位", "期货业务单位", "负责人", "客户经理"]

ROOM = "王港机房"
IP_RE = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:/\d{1,2})?")

# 源表「用途分类」带了 A/B 组别前缀，进标签池前去掉（组别记进备注）
CATEGORY_MAP = {"A期权柜台": "期权柜台", "B现货柜台": "现货柜台"}


def cell_text(value):
    if value is None:
        return ""
    return str(value).replace("\r", " ").strip()


def split_ip(text):
    """从「192.168.101.211/24 SF2-1 / bond 主备模式」里拆出 IP 与其余标注。"""
    text = cell_text(text).replace("\n", " / ")
    match = IP_RE.search(text)
    if not match:
        return "", re.sub(r"\s+", " ", text).strip(" /")
    rest = re.sub(r"\s+", " ", (text[: match.start()] + " " + text[match.end():])).strip()
    rest = re.sub(r"^[\s/]+|[\s/]+$", "", rest)
    return match.group(0), rest


def build_rows(source_path: Path, customer_id: str, customer_name: str):
    wb = openpyxl.load_workbook(source_path, data_only=True)
    ws = wb[wb.sheetnames[0]]
    headers = [cell_text(ws.cell(row=2, column=c).value) for c in range(1, 15)]

    rows, waiting = [], []
    for r in range(3, ws.max_row + 1):
        raw = [cell_text(ws.cell(row=r, column=c).value) for c in range(1, 15)]
        if not any(raw):
            continue
        cpu_raw, model_raw, serial, cabinet, slot, usage, category, manage_ip, oob_ip, nic, oob_pwd, inner, report_a, report_b = raw

        # 两行源表把「整机型号」和「CPU 型号」写反了（A=NF5280M7 整机、B=6544Y CPU）
        swapped = model_raw.upper().startswith(("I5-", "I7-", "I9-")) is False and re.fullmatch(r"\d{4}[A-Za-z]*", model_raw or "")
        if swapped:
            cpu, model = model_raw, cpu_raw
        else:
            cpu, model = cpu_raw, model_raw

        category_clean = CATEGORY_MAP.get(category, category)
        inner_ip, inner_note = split_ip(inner)
        report_a_ip, report_a_note = split_ip(report_a)
        report_b_ip, report_b_note = split_ip(report_b)

        notes = []
        if category in CATEGORY_MAP:
            notes.append(f"源表用途分类：{category}")
        if inner_note:
            notes.append(f"私网口 {inner_note}")
        for label, ip, note, raw_value in (("报盘主", report_a_ip, report_a_note, report_a), ("报盘备", report_b_ip, report_b_note, report_b)):
            if ip:
                notes.append(f"{label} {ip}" + (f"（{note}）" if note else ""))
            elif raw_value:
                notes.append(f"{label}源表为占位符 {raw_value}，待补")
        if not inner_ip and inner:
            notes.append("源表无私网IP")
        elif not inner:
            notes.append("源表无私网IP")
        if not serial:
            notes.append("源表缺序列号，需现场抄铭牌补录")
        if not nic:
            notes.append("源表缺网卡信息")

        row = {
            "序列号": serial,
            "品牌": "",
            "型号": model,
            "客户id": customer_id,
            "客户名称": customer_name,
            "负责人": "",
            "归属单位": "",
            "CPU型号": cpu,
            "网卡": nic,
            "管理ip": manage_ip,
            "交易ip": inner_ip,
            "带外ip": oob_ip,
            "管理密码": "",
            "带外密码": oob_pwd,
            "服务器成本(万)": "",
            "用途分类": category_clean,
            "用途": usage,
            "防火墙白名单": "",
            "资源申请编号": "",
            "是否液冷": "",
            "所在机房": ROOM,
            "所属机柜": cabinet,
            "所属机位": slot,
            "上架时间": "",
            "接入行情": category_clean if "行情" in category_clean else "",
            "接入柜台": category_clean if "柜台" in category_clean else "",
            "备注": "；".join(notes),
        }
        row["_source_row"] = r
        (rows if serial else waiting).append(row)
    wb.close()
    return headers, rows, waiting


def find_duplicates(rows):
    """同机柜机位重复、序列号重复、报盘地址复用，都要提示。"""
    problems = []
    slots = {}
    for row in rows:
        key = (row["所在机房"], row["所属机柜"], row["所属机位"])
        if not row["所属机位"]:
            continue
        if key in slots:
            problems.append(f"机位冲突：{key[1]} {key[2]} 同时被 {slots[key]} 和 {row['序列号']} 占用")
        else:
            slots[key] = row["序列号"]
    serials = {}
    for row in rows:
        serials.setdefault(row["序列号"], []).append(row["序列号"])
    for serial, items in serials.items():
        if len(items) > 1:
            problems.append(f"序列号重复：{serial}")
    return problems


def flag_reused_report_ips(rows):
    """报盘地址在不同设备上复用（源表常见照抄未改），在备注里标出来。"""
    seen = {}
    for row in rows:
        text = row.get("备注", "")
        for label in ("报盘主", "报盘备"):
            marker = f"{label} "
            if marker not in text:
                continue
            after = text.split(marker, 1)[1]
            ip = after.split("（")[0].split("；")[0].strip()
            if not IP_RE.fullmatch(ip):
                continue
            owner = seen.get((label, ip))
            if owner and owner != row["序列号"]:
                row["备注"] = (text + f"；{label} {ip} 与 {owner} 重复，待核对").strip("；")
                text = row["备注"]
            else:
                seen[(label, ip)] = row["序列号"]



def parse_slot_range(text):
    """'1-47' / '3' / '3-4' → (3, 47) / (3, 3) / (3, 4)；解析不出来返回 None。"""
    text = cell_text(text)
    match = re.fullmatch(r"(\d+)\s*[-~]\s*(\d+)", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    if text.isdigit():
        return int(text), int(text)
    return None


def validate_against_snapshot(rows, snapshot_path):
    """按生产库实况校验：客户是否存在、机柜是否登记、U 位是否越界/被占、序列号是否已存在。"""
    import json

    data = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    customers = {row.get("客户id"): row.get("名称") for row in data.get("customers", [])}
    cabinets = {}
    for row in data.get("cabinets", []):
        cabinets[(row.get("机房"), row.get("机柜"))] = row.get("机位")
    occupied = {}
    for row in data.get("servers", []):
        slot = cell_text(row.get("所属机位"))
        if slot:
            occupied[(row.get("所在机房"), row.get("所属机柜"), slot)] = row.get("序列号")
    serials = {row.get("序列号"): row.get("客户名称") for row in data.get("servers", [])}

    problems = []
    for row in rows:
        serial = row["序列号"]
        customer_id = row["客户id"]
        if customer_id not in customers:
            problems.append(f"{serial}：客户id {customer_id} 在生产客户表里不存在")
        elif customers[customer_id] != row["客户名称"]:
            problems.append(f"{serial}：客户名称与生产库不符（生产为 {customers[customer_id]}）")
        if serial in serials:
            problems.append(f"{serial}：序列号在生产服务器表里已存在（{serials[serial]}）")

        room_cabinet = (row["所在机房"], row["所属机柜"])
        if room_cabinet not in cabinets:
            problems.append(f"{serial}：机柜 {row['所在机房']} {row['所属机柜']} 未在生产机柜表登记")
            continue
        slot = cell_text(row["所属机位"])
        if not slot:
            continue
        slot_range = parse_slot_range(slot)
        cabinet_range = parse_slot_range(cabinets[room_cabinet])
        if not slot_range:
            problems.append(f"{serial}：所属机位 {slot} 不是「起始U-结束U」格式")
        elif cabinet_range and (slot_range[0] < cabinet_range[0] or slot_range[1] > cabinet_range[1]):
            problems.append(f"{serial}：机位 {slot} 超出 {row['所属机柜']} 的登记范围 {cabinets[room_cabinet]}")
        if (row["所在机房"], row["所属机柜"], slot) in occupied:
            problems.append(f"{serial}：机位 {row['所属机柜']} {slot} 已被 {occupied[(row['所在机房'], row['所属机柜'], slot)]} 占用")
    return problems


def write_sheet(path: Path, sheet_name: str, headers, rows, extra_sheets=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    head_fill = PatternFill("solid", fgColor="E8F3F1")
    for index, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=index, value=header)
        cell.font = Font(bold=True, color="17344B")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row_index, row in enumerate(rows, 2):
        for col_index, header in enumerate(headers, 1):
            cell = ws.cell(row=row_index, column=col_index, value=str(row.get(header, "") or ""))
            cell.number_format = "@"  # 全按文本，避免 Excel 把 IP / 序列号转成数字或日期
            cell.alignment = Alignment(vertical="center")
    widths = {"序列号": 22, "型号": 20, "CPU型号": 28, "网卡": 12, "管理ip": 20, "交易ip": 20,
              "带外ip": 20, "带外密码": 16, "用途分类": 14, "用途": 30, "备注": 60,
              "客户id": 12, "客户名称": 26, "所在机房": 12, "所属机柜": 10, "所属机位": 10}
    for index, header in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(index)].width = widths.get(header, 14)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows), 1) + 1}"
    for name, sheet_rows in (extra_sheets or []):
        extra = wb.create_sheet(name)
        for index, header in enumerate(headers, 1):
            cell = extra.cell(row=1, column=index, value=header)
            cell.font = Font(bold=True, color="17344B")
            cell.fill = head_fill
        for row_index, row in enumerate(sheet_rows, 2):
            for col_index, header in enumerate(headers, 1):
                cell = extra.cell(row=row_index, column=col_index, value=str(row.get(header, "") or ""))
                cell.number_format = "@"
        for index, header in enumerate(headers, 1):
            extra.column_dimensions[get_column_letter(index)].width = widths.get(header, 14)
        extra.freeze_panes = "A2"
    wb.save(path)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--customer-id", default="00000001")
    parser.add_argument("--customer-name", default="上海分公司自有")
    parser.add_argument("--business-unit", default="上海分公司")
    parser.add_argument("--snapshot", default="", help="生产库 customers/cabinets/servers 的 JSON 快照，用来做客户/机柜/U位校验")
    parser.add_argument("--write-customer", action="store_true", help="额外生成客户表补录文件（仅当客户在生产库不存在时需要）")
    args = parser.parse_args()

    source = Path(args.source)
    outdir = Path(args.outdir)
    headers, rows, waiting = build_rows(source, args.customer_id, args.customer_name)
    flag_reused_report_ips(rows)

    problems = find_duplicates(rows + waiting)
    if args.snapshot:
        problems += validate_against_snapshot(rows, args.snapshot)
    servers_path = write_sheet(
        outdir / "服务器表导入-期权交易与柜台设备.xlsx",
        "服务器信息",
        SERVER_HEADERS,
        rows,
        extra_sheets=[("待补录（缺序列号）", waiting)],
    )

    print(f"可导入 {len(rows)} 行 → {servers_path}")
    print(f"待补录 {len(waiting)} 行（缺序列号）→ 同一文件的 sheet「待补录（缺序列号）」")
    if args.write_customer:
        customer_row = {"客户id": args.customer_id, "名称": args.customer_name, "业务单位": args.business_unit}
        customers_path = write_sheet(
            outdir / "客户表补录-上海分公司自有.xlsx",
            "客户表",
            CUSTOMER_HEADERS,
            [customer_row],
        )
        print(f"客户补录 1 行（{args.customer_id} {args.customer_name}）→ {customers_path}")
    else:
        print(f"客户 {args.customer_id} {args.customer_name}：未生成补录文件（假定生产库已存在）")
    for item in problems:
        print("  ⚠", item)
    if not problems:
        print("  校验通过：客户/机柜/U 位/序列号均无冲突")


if __name__ == "__main__":
    main()
