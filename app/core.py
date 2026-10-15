"""纯逻辑层：实体元数据、权限矩阵、校验、机位/IP 解析。

本模块不依赖 Flask、SQLite 或文件系统，可直接单元测试。
运行期不可变数据（ENTITIES 等）也定义在这里。
"""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime

ROLE_ADMIN = "管理人员"
ROLE_ROOM = "机房人员"
ROLE_BUSINESS = "业务人员"

ENTITIES = {
    "customers": {
        "label": "客户",
        "filename": "客户表.xlsx",
        "sheet": "客户表",
        "key": ["客户id"],
        "headers": ["客户id", "名称", "资产", "月产出", "业务单位", "期货业务单位", "负责人", "客户经理"],
    },
    "cabinets": {
        "label": "机柜",
        "filename": "机柜信息表.xlsx",
        "sheet": "机柜信息表",
        "key": ["机房", "机柜"],
        "headers": ["机房", "机柜", "机位", "瓦数(kw)", "费用(年)", "管理ip网段", "交易ip网段", "带外ip网段"],
    },
    "servers": {
        "label": "服务器",
        "filename": "服务器信息表.xlsx",
        "sheet": "服务器信息",
        "key": ["序列号"],
        "headers": ["序列号", "品牌", "型号", "客户id", "客户名称", "负责人", "归属单位", "CPU型号", "网卡", "管理ip", "交易ip", "带外ip", "管理密码", "带外密码", "服务器成本(万)", "用途分类", "用途", "防火墙白名单", "资源申请编号", "是否液冷", "所在机房", "所属机柜", "所属机位", "上架时间", "接入行情", "接入柜台", "备注"],
    },
}

FIELD_ALIASES = {
    "cabinets": {"瓦数(w)": "瓦数(kw)"},
    "servers": {"归属": "归属单位", "归属部门": "归属单位", "成本": "服务器成本(万)", "所属客户id": "客户id", "密码": "管理密码"},
}

IP_FIELDS = ("管理ip", "交易ip", "带外ip")
IP_RANGE_FIELDS = {"管理ip": "管理ip网段", "交易ip": "交易ip网段", "带外ip": "带外ip网段"}

RACK_REQUEST_HEADERS = ["序列号", "品牌", "型号", "CPU型号", "网卡", "服务器成本(万)", "防火墙白名单", "资源申请编号", "是否液冷", "是否上架", "是否重装系统", "初始密码", "上下架需求", "备注", "客户id", "客户名称", "所在机房"]
RACK_REQUEST_META = {"label": "上下架请求", "sheet": "上下架请求", "headers": RACK_REQUEST_HEADERS}
# 导出时在请求字段基础上追加机位/IP 分配结果，便于在机位IP分配/具体实施阶段留档
RACK_REQUEST_EXPORT_HEADERS = RACK_REQUEST_HEADERS + ["所属机柜", "所属机位", "管理ip", "交易ip", "带外ip"]
RACK_REQUEST_FIELD_ALIASES = {"成本": "服务器成本(万)"}


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_value(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def normalize_entity(entity):
    return entity


def entity_meta(entity):
    return ENTITIES[normalize_entity(entity)]


def normalize_row(entity, row):
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    aliases = FIELD_ALIASES.get(entity, {})
    normalized = {h: "" for h in meta["headers"]}
    for key, value in (row or {}).items():
        target = aliases.get(key, key)
        if target in normalized:
            normalized[target] = clean_value(value)
    return normalized


def record_key(meta, row):
    return "|".join(clean_value(row.get(field)) for field in meta["key"])


def normalize_rack_request_header(header):
    header = clean_value(header)
    return RACK_REQUEST_FIELD_ALIASES.get(header, header)


def initial_rack_request_stage(request_type):
    return "allocation" if request_type == "上架" else "implementation"


def parse_json_dict(value):
    import json

    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def summarize_record(data, headers=None, limit=4):
    if not data:
        return ""
    headers = headers or list(data.keys())
    parts = []
    for header in headers:
        value = clean_value(data.get(header))
        if value:
            parts.append(f"{header}：{value}")
        if len(parts) >= limit:
            break
    return "；".join(parts)


def positions(value):
    match = re.fullmatch(r"(\d+)-(\d+)", clean_value(value))
    if not match:
        return []
    start, end = int(match.group(1)), int(match.group(2))
    low, high = min(start, end), max(start, end)
    return list(range(high, low - 1, -1))


def ip_last_octet(value):
    try:
        return int(str(ipaddress.ip_address(clean_value(value))).split(".")[-1])
    except ValueError:
        return None


def allocatable_ip(value):
    ip_text = clean_value(value)
    if not ip_text:
        return ""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return ""
    if ip.version != 4:
        return ""
    last = int(str(ip).split(".")[-1])
    return "" if last in {0, 255} else str(ip)


def parse_ip_range(value):
    value = clean_value(value)
    if not value:
        return []
    if "*" in value:
        match = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.\*", value)
        if not match:
            raise ValueError(f"IP网段 {value} 格式无效")
        prefix = ".".join(match.groups())
        try:
            ipaddress.ip_address(f"{prefix}.1")
        except ValueError as exc:
            raise ValueError(f"IP网段 {value} 格式无效") from exc
        return [f"{prefix}.{last}" for last in range(1, 255)]
    if "-" in value:
        start_text, end_text = [part.strip() for part in value.split("-", 1)]
        try:
            start = ipaddress.ip_address(start_text)
            end = ipaddress.ip_address(end_text)
        except ValueError as exc:
            raise ValueError(f"IP网段 {value} 格式无效") from exc
        if start.version != 4 or end.version != 4:
            raise ValueError(f"IP网段 {value} 仅支持 IPv4")
        low, high = sorted((int(start), int(end)))
        return [ip for number in range(low, high + 1) if (ip := allocatable_ip(ipaddress.ip_address(number)))]
    try:
        if "/" in value:
            network = ipaddress.ip_network(value, strict=False)
            if network.version != 4:
                raise ValueError
            return [str(ip) for ip in network.hosts()]
        ip = allocatable_ip(value)
        return [ip] if ip else []
    except ValueError as exc:
        raise ValueError(f"IP网段 {value} 格式无效") from exc


def is_valid_ipv4_or_cidr(value):
    try:
        parsed = ipaddress.ip_interface(value) if "/" in value else ipaddress.ip_address(value)
    except ValueError:
        return False
    return parsed.version == 4


def capabilities_for(user):
    if not user:
        return []
    role_name = user["身份"]
    if role_name == ROLE_ADMIN:
        return ["manage_all", "review_operations", "review_rack_requests", "submit_rack_request", "manage_settings", "view_statistics"]
    if role_name == ROLE_ROOM:
        return ["review_rack_requests", "view_statistics"]
    if role_name == ROLE_BUSINESS:
        return ["submit_rack_request"]
    return []


def can_view_entity(entity, user):
    if not user:
        return False
    entity = normalize_entity(entity)
    role_name = user["身份"]
    if role_name == ROLE_ADMIN:
        return entity in {"customers", "cabinets", "servers"}
    if role_name == ROLE_ROOM:
        return entity in {"cabinets", "servers"}
    if role_name == ROLE_BUSINESS:
        return entity in {"customers", "servers"}
    return False


def can_mutate_entity(entity, user):
    if not user:
        return False
    entity = normalize_entity(entity)
    role_name = user["身份"]
    if role_name == ROLE_ADMIN:
        return entity in {"customers", "cabinets", "servers"}
    if role_name == ROLE_ROOM:
        return entity in {"cabinets", "servers"}
    if role_name == ROLE_BUSINESS:
        return entity in {"customers", "servers"}
    return False


def filter_records_for_user(entity, records, user):
    entity = normalize_entity(entity)
    if not user or user["身份"] == ROLE_ADMIN:
        return records
    if user["身份"] == ROLE_ROOM:
        room = user["所属机房"]
        if entity == "cabinets":
            return [row for row in records if row.get("机房") == room]
        if entity == "servers":
            return [row for row in records if row.get("所在机房") == room]
        return []
    if user["身份"] == ROLE_BUSINESS:
        business_unit = user["业务单位"]
        if entity == "customers":
            return [row for row in records if row.get("业务单位") == business_unit]
        if entity == "servers":
            return [row for row in records if row.get("归属单位") == business_unit]
        return []
    return []


def rack_request_visible(row, user):
    import json

    if user["身份"] == ROLE_ADMIN:
        return True
    data = json.loads(row["request_data"])
    if user["身份"] == ROLE_ROOM:
        return data.get("所在机房") == user["所属机房"]
    if user["身份"] == ROLE_BUSINESS:
        return row["submitter_id"] == user["用户id"]
    return False


def submitted_rack_request_visible(row, user):
    return row["submitter_id"] == user["用户id"]


def can_review_rack_request(row, user):
    import json

    if user["身份"] == ROLE_ADMIN:
        return True
    if user["身份"] != ROLE_ROOM:
        return False
    data = json.loads(row["request_data"])
    return data.get("所在机房") == user["所属机房"]


def can_manage_rack_request(row, user):
    return row["submitter_id"] == user["用户id"]


def can_review_operation_change(change, user):
    return user["身份"] == ROLE_ADMIN


def pending_status_text(row):
    operation = row.get("_pending_operation")
    return f"待审批{operation}" if operation else "正式"


def record_filter_value(row, field):
    if field == "在途状态":
        return pending_status_text(row)
    return row.get(field, "")


def parse_filters(entity, body):
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    filters = body.get("filters") if isinstance(body, dict) else []
    if filters is None:
        filters = []
    if not isinstance(filters, list):
        raise ValueError("filters 必须是数组")
    parsed = []
    for index, item in enumerate(filters, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个条件格式无效")
        field = clean_value(item.get("field"))
        pattern = clean_value(item.get("pattern"))
        if not field and not pattern:
            continue
        if field != "在途状态" and field not in meta["headers"]:
            raise ValueError(f"未知列名：{field}")
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"第 {index} 个条件正则表达式无效：{exc}") from exc
        parsed.append((field, regex))
    return parsed


def parse_sorts(entity, body):
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    sorts = body.get("sorts") if isinstance(body, dict) else []
    if sorts is None:
        sorts = []
    if not isinstance(sorts, list):
        raise ValueError("sorts 必须是数组")
    parsed = []
    for index, item in enumerate(sorts, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个排序条件格式无效")
        field = clean_value(item.get("field"))
        direction = clean_value(item.get("direction") or "asc").lower()
        if not field:
            continue
        if field != "在途状态" and field not in meta["headers"]:
            raise ValueError(f"未知排序列名：{field}")
        if direction not in {"asc", "desc"}:
            raise ValueError("排序方向仅支持 asc 或 desc")
        parsed.append((field, direction))
    return parsed


def natural_sort_parts(value):
    value = clean_value(value)
    parts = []
    for part in re.split(r"(\d+)", value):
        if not part:
            continue
        parts.append((0, int(part)) if part.isdigit() else (1, part.casefold()))
    return parts


def compare_sort_values(left, right, direction):
    left_text = clean_value(left)
    right_text = clean_value(right)
    left_empty = left_text == ""
    right_empty = right_text == ""
    if left_empty and right_empty:
        return 0
    if left_empty:
        return 1
    if right_empty:
        return -1
    left_key = natural_sort_parts(left_text)
    right_key = natural_sort_parts(right_text)
    result = (left_key > right_key) - (left_key < right_key)
    return -result if direction == "desc" else result


def sort_records(records, sorts):
    from functools import cmp_to_key

    if not sorts:
        return records

    def compare(left, right):
        for field, direction in sorts:
            result = compare_sort_values(record_filter_value(left, field), record_filter_value(right, field), direction)
            if result:
                return result
        return 0

    return sorted(records, key=cmp_to_key(compare))
