"""业务流程层：叠加视图、查询、信息维护审批流、上下架请求、机位/IP 占用、通知事件。

与 HTTP 层解耦：需要登录人信息的地方（提交人、审核人）一律显式传参。
"""
from __future__ import annotations

import json
import logging
import uuid

from . import notifier, storage
from .core import (
    ENTITIES,
    IP_FIELDS,
    IP_RANGE_FIELDS,
    ROLE_ADMIN,
    ROLE_BUSINESS,
    ROLE_ROOM,
    RACK_REQUEST_HEADERS,
    RACK_REQUEST_EXPORT_HEADERS,
    RACK_REQUEST_META,
    REQUEST_TYPES,
    TAG_MAX_COUNT,
    TAG_MAX_LENGTH,
    TAG_MAX_OPTIONS,
    TAG_SEPARATOR_PATTERN,
    capabilities_for,
    clean_value,
    date_text,
    entity_meta,
    filter_records_for_user,
    initial_rack_request_stage,
    is_multi_value_field,
    join_tags,
    multi_value_fields,
    normalize_entity,
    normalize_rack_request_header,
    normalize_rack_request_value,
    normalize_row,
    normalize_tag,
    now_text,
    parse_filters,
    parse_json_dict,
    parse_sorts,
    positions,
    record_key,
    sort_records,
    split_tags,
    summarize_record,
    today_text,
    is_valid_ipv4_or_cidr,
    allocatable_ip,
    ip_last_octet,
    parse_ip_range,
)
from .storage import (
    LOCK,
    NOTIFY_TIMEOUT,
    db,
    read_records,
    read_settings,
    write_records,
    write_settings,
)

logger = logging.getLogger(__name__)


def ip_usage_for(room, cabinet_name, fields=IP_FIELDS, include_official=True, include_implementation=True, exclude_request_id=""):
    usage = {field: {} for field in fields}
    if include_official:
        for server in read_records("servers"):
            if server.get("所在机房") != room:
                continue
            for field in fields:
                value = clean_value(server.get(field))
                if value:
                    usage[field][value] = {"serial": server.get("序列号", ""), "source": "服务器"}
    if include_implementation:
        with db() as conn:
            rows = conn.execute(
                "SELECT id,serial_number,stage_data FROM rack_requests WHERE status='pending' AND stage='implementation' AND request_type='上架'"
            ).fetchall()
        for row in rows:
            if exclude_request_id and row["id"] == exclude_request_id:
                continue
            data = parse_json_dict(row["stage_data"])
            if data.get("所在机房") != room:
                continue
            for field in fields:
                value = clean_value(data.get(field))
                if value:
                    usage[field][value] = {"serial": row["serial_number"], "source": "待具体实施服务器"}
    return usage


def same_kind_ip_conflict_errors(server_data, include_official=True, include_implementation=False, exclude_request_id=""):
    room = clean_value(server_data.get("所在机房"))
    cabinet_name = clean_value(server_data.get("所属机柜"))
    if not room:
        return []
    usage = ip_usage_for(
        room,
        cabinet_name,
        include_official=include_official,
        include_implementation=include_implementation,
        exclude_request_id=exclude_request_id,
    )
    errors = []
    serial = clean_value(server_data.get("序列号"))
    for field in IP_FIELDS:
        value = clean_value(server_data.get(field))
        conflict = usage.get(field, {}).get(value) if value else None
        if conflict and conflict["serial"] != serial:
            errors.append(f"{field} {value} 已被{conflict['source']} {conflict['serial']} 使用")
    return errors


def auto_assign_ips(room, cabinet_name, exclude_request_id=""):
    cabinet = next((row for row in read_records("cabinets") if row["机房"] == room and row["机柜"] == cabinet_name), None)
    if not cabinet:
        raise ValueError(f"所在机房 {room} / 所属机柜 {cabinet_name} 未在机柜信息表登记")
    ranges = {}
    for field, range_field in IP_RANGE_FIELDS.items():
        configured = clean_value(cabinet.get(range_field))
        if configured:
            ranges[field] = parse_ip_range(configured)
    if not ranges:
        raise ValueError("当前机柜未配置可自动分配的 IP 网段")
    usage = ip_usage_for(room, cabinet_name, fields=tuple(ranges.keys()), include_implementation=True, exclude_request_id=exclude_request_id)
    by_tail = {}
    for field, values in ranges.items():
        by_tail[field] = {}
        for ip_text in values:
            if ip_text in usage[field]:
                continue
            tail = ip_last_octet(ip_text)
            if tail is not None and tail not in by_tail[field]:
                by_tail[field][tail] = ip_text
    for tail in range(1, 255):
        if all(tail in candidates for candidates in by_tail.values()):
            return {field: candidates[tail] for field, candidates in by_tail.items()}
    raise LookupError("没有可用的同尾号 IP 组合")


def find_record(entity, key):
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    return next((x for x in read_records(entity) if record_key(meta, x) == key), None)


def occupied_slots(exclude_record_key=None):
    server_meta = ENTITIES["servers"]
    result = {}
    for server in read_records("servers"):
        if exclude_record_key and record_key(server_meta, server) == exclude_record_key:
            continue
        key = (server["所在机房"], server["所属机柜"])
        for slot in positions(server["所属机位"]):
            result.setdefault(key, {})[slot] = server["序列号"]
    return result


def occupied_slot_ranges(exclude_record_key=None):
    """每个已实施服务器占用的机位 -> 该服务器占用的完整机位范围。

    交换时机位需整段对调（对方可能占用多行），因此前端要能拿到「占用者的完整范围」。
    """
    server_meta = ENTITIES["servers"]
    result = {}
    for server in read_records("servers"):
        if exclude_record_key and record_key(server_meta, server) == exclude_record_key:
            continue
        key = (server["所在机房"], server["所属机柜"])
        range_value = clean_value(server["所属机位"])
        for slot in positions(range_value):
            result.setdefault(key, {})[slot] = range_value
    return result


def pending_server_slot_states():
    result = {}
    with db() as conn:
        rows = conn.execute(
            "SELECT id,operation,record_key,new_data FROM changes WHERE status='pending' AND entity='servers'"
        ).fetchall()
    for change in rows:
        if change["operation"] == "新增" and change["new_data"]:
            server = normalize_row("servers", json.loads(change["new_data"]))
            key = (server["所在机房"], server["所属机柜"])
            for slot in positions(server["所属机位"]):
                result.setdefault(key, {})[slot] = {
                    "status": "pending_occupied",
                    "server": server["序列号"] or change["record_key"],
                    "selectable": False,
                    "change_id": change["id"],
                    "slot_range": clean_value(server["所属机位"]),
                }
        if change["operation"] == "删除":
            server = find_record("servers", change["record_key"])
            if not server:
                continue
            key = (server["所在机房"], server["所属机柜"])
            for slot in positions(server["所属机位"]):
                result.setdefault(key, {})[slot] = {
                    "status": "pending_release",
                    "server": server["序列号"],
                    "selectable": True,
                    "slot_range": clean_value(server["所属机位"]),
                }
        if change["operation"] == SWAP_OPERATION and change["new_data"]:
            data = parse_json_dict(change["new_data"])
            self_new = normalize_row("servers", data.get("self") or {})
            self_from = data.get("self_from") or {}
            # 我方：即将迁入对方机位 => 目标机位待审核占用
            for slot in positions(self_new.get("所属机位")):
                result.setdefault((self_new["所在机房"], self_new["所属机柜"]), {})[slot] = {
                    "status": "pending_occupied",
                    "server": self_new["序列号"] or change["record_key"],
                    "selectable": False,
                    "swap": True,
                    "slot_range": clean_value(self_new.get("所属机位")),
                }
            # 我方原机位在新数据里已不再包含，标记为待审核释放（交换中）
            for slot in positions(self_from.get("slot")):
                result.setdefault((clean_value(self_from.get("room")), clean_value(self_from.get("cabinet"))), {})[slot] = {
                    "status": "pending_release",
                    "server": self_new["序列号"] or change["record_key"],
                    "selectable": False,
                    "swap": True,
                    "slot_range": clean_value(self_from.get("slot")),
                }
    return result


def implementation_slot_states(exclude_request_id=""):
    """机位IP分配已完成、待具体实施的上架请求所占用的机位。

    与 IP 的在途占用逻辑（ip_usage_for 的 include_implementation）对齐：
    这些机位虽未写入正式 Excel，但已锁定，不允许再分配给其他请求。
    """
    result = {}
    with db() as conn:
        rows = conn.execute(
            "SELECT id,serial_number,stage_data FROM rack_requests "
            "WHERE status='pending' AND stage='implementation' AND request_type='上架'"
        ).fetchall()
    for row in rows:
        if exclude_request_id and row["id"] == exclude_request_id:
            continue
        data = parse_json_dict(row["stage_data"])
        room = clean_value(data.get("所在机房"))
        cabinet_name = clean_value(data.get("所属机柜"))
        if not room or not cabinet_name:
            continue
        for slot in positions(clean_value(data.get("所属机位"))):
            result.setdefault((room, cabinet_name), {})[slot] = {
                "status": "pending_implementation",
                "server": row["serial_number"],
                "selectable": False,
                "request_id": row["id"],
                "slot_range": clean_value(data.get("所属机位")),
            }
    return result


def cabinet_slot_state(slot, taken, pending):
    if slot in pending:
        return pending[slot]
    if slot in taken:
        return {"status": "occupied", "server": taken[slot], "selectable": False}
    return {"status": "free", "server": "", "selectable": True}


def cabinet_options(exclude_server=None):
    exclude_key = clean_value(exclude_server)
    occupied = occupied_slots(exclude_key)
    occupied_ranges = occupied_slot_ranges(exclude_key)
    pending_states = pending_server_slot_states()
    implementation_states = implementation_slot_states()
    rooms = {}
    for cabinet in read_records("cabinets"):
        room_name = cabinet["机房"]
        cabinet_name = cabinet["机柜"]
        room = rooms.setdefault(room_name, {"name": room_name, "cabinets": []})
        taken = occupied.get((room_name, cabinet_name), {})
        taken_ranges = occupied_ranges.get((room_name, cabinet_name), {})
        pending = {
            **implementation_states.get((room_name, cabinet_name), {}),
            **pending_states.get((room_name, cabinet_name), {}),
        }
        slots = []
        for slot in positions(cabinet["机位"]):
            state = cabinet_slot_state(slot, taken, pending)
            item = {"slot": str(slot), **state}
            # owner_range：占用者占用的完整机位范围（对方多行时交换需整段对调）
            item["owner_range"] = taken_ranges.get(slot) or state.get("slot_range", "")
            slots.append(item)
        room["cabinets"].append({
            "name": cabinet_name,
            "range": cabinet["机位"],
            "power": cabinet.get("瓦数(kw)", cabinet.get("瓦数(w)", "")),
            "annual_cost": cabinet["费用(年)"],
            "ip_ranges": {field: cabinet.get(range_field, "") for field, range_field in IP_RANGE_FIELDS.items()},
            "slots": slots,
        })
    for room in rooms.values():
        room["cabinets"].sort(key=lambda x: x["name"])
    return {"rooms": sorted(rooms.values(), key=lambda x: x["name"])}


def pending_changes_for_entity(entity):
    entity = normalize_entity(entity)
    with db() as conn:
        rows = conn.execute(
            "SELECT id,operation,record_key,new_data,submitter,submitted_at FROM changes WHERE status='pending' AND entity=? ORDER BY submitted_at,id",
            (entity,),
        ).fetchall()
    return [dict(row) for row in rows]


def attach_pending_preview(record, change):
    item = dict(record)
    item["_pending_operation"] = change["operation"]
    item["_pending_change_id"] = change["id"]
    item["_pending_submitter"] = change["submitter"]
    item["_pending_submitted_at"] = change["submitted_at"]
    item["_pending_new_data"] = json.loads(change["new_data"]) if change["new_data"] else None
    return item


def records_for_state(entity, record_state="official", user=None):
    entity = normalize_entity(entity)
    if record_state == "pending":
        return pending_state_records(entity, user)
    return filter_records_for_user(entity, read_records(entity), user)


def pending_state_records(entity, user=None):
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    records = filter_records_for_user(entity, read_records(entity), user)
    result = [dict(record) for record in records]
    index_by_key = {record_key(meta, record): index for index, record in enumerate(result)}
    changes = pending_changes_for_entity(entity)
    for change in changes:
        if change["operation"] == "新增":
            if not change["new_data"]:
                continue
            new_data = json.loads(change["new_data"])
            if filter_records_for_user(entity, [new_data], user):
                result.append(attach_pending_preview(new_data, change))
            continue
        if change["record_key"] not in index_by_key:
            continue
        index = index_by_key[change["record_key"]]
        if change["operation"] == "删除":
            result[index] = attach_pending_preview(result[index], change)
            continue
        if change["operation"] == "修改" and change["new_data"]:
            new_data = json.loads(change["new_data"])
            result[index] = attach_pending_preview(new_data, change)
            new_key = record_key(meta, new_data)
            if new_key != change["record_key"]:
                index_by_key.pop(change["record_key"], None)
                index_by_key[new_key] = index
    return result


def pending_mutation_for_record(entity, record_key_value, conn=None):
    owns = conn is None
    conn = conn or db()
    row = conn.execute(
        """SELECT id,operation FROM changes
        WHERE status='pending' AND entity=? AND record_key=? AND operation IN ('修改','删除')
        ORDER BY submitted_at,id LIMIT 1""",
        (normalize_entity(entity), record_key_value),
    ).fetchone()
    if owns:
        conn.close()
    return dict(row) if row else None


def filter_records(entity, filters, record_state="official", user=None):
    records = records_for_state(entity, record_state, user)
    if not filters:
        return records
    from .core import record_filter_value

    return [
        row for row in records
        if all(regex.search(clean_value(record_filter_value(row, field))) for field, regex in filters)
    ]


def query_records(entity, body, record_state="official", user=None):
    filters = parse_filters(entity, body)
    sorts = parse_sorts(entity, body)
    records = filter_records(entity, filters, record_state, user)
    return sort_records(records, sorts)


def customers_by_id():
    return {row["客户id"]: row for row in read_records("customers") if row.get("客户id")}


def customer_owner(customer_id, customer_map=None):
    """从客户表按客户 id 查询「负责人」字段（用于「自动同步负责人」按钮）。"""
    customer_map = customer_map or customers_by_id()
    customer = customer_map.get(clean_value(customer_id))
    return clean_value(customer.get("负责人")) if customer else ""


def apply_customer_name(row, customer_map=None):
    data = dict(row or {})
    customer_id = clean_value(data.get("客户id"))
    customer_map = customer_map or customers_by_id()
    customer = customer_map.get(customer_id)
    if customer:
        data["客户名称"] = customer.get("名称", "")
    return data


def validate_record(entity, row, existing=None, exclude_record_key=None, allow_occupied_slots=False):
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    errors = []
    normalized = normalize_row(entity, row)
    for field in meta["key"]:
        if not normalized[field]:
            errors.append(f"{field}不能为空")
    if entity == "customers":
        if normalized["客户id"] and not normalized["客户id"].isdigit():
            errors.append("客户id只能是数字")
    if entity in ("cabinets", "servers"):
        slot = normalized["机位"] if entity == "cabinets" else normalized["所属机位"]
        if slot and not __import__("re").fullmatch(r"\d+-\d+", slot):
            errors.append("机位格式应类似 9-10")
    if entity == "servers":
        customer_map = customers_by_id()
        for field in IP_FIELDS:
            value = normalized[field]
            if value and not is_valid_ipv4_or_cidr(value):
                errors.append(f"{field}不是有效IPv4或CIDR地址")
        client_id = normalized["客户id"]
        if not client_id:
            errors.append("客户id不能为空")
        elif client_id not in customer_map:
            errors.append(f"客户id {client_id} 不存在")
        else:
            normalized["客户名称"] = customer_map[client_id].get("名称", "")
            if not normalized.get("负责人"):
                normalized["负责人"] = customer_map[client_id].get("负责人", "")
        room = normalized["所在机房"]
        cabinet_name = normalized["所属机柜"]
        server_slots = positions(normalized["所属机位"])
        cabinet_record = next((x for x in read_records("cabinets") if x["机房"] == room and x["机柜"] == cabinet_name), None)
        if not cabinet_record:
            errors.append(f"所在机房 {room} / 所属机柜 {cabinet_name} 未在机柜信息表登记")
        else:
            cabinet_slots = set(positions(cabinet_record["机位"]))
            if not server_slots or not set(server_slots).issubset(cabinet_slots):
                errors.append(f"所属机位 {normalized['所属机位']} 超出机柜登记范围 {cabinet_record['机位']}")
        taken = occupied_slots(exclude_record_key).get((room, cabinet_name), {})
        if not allow_occupied_slots:
            for slot in server_slots:
                if slot in taken:
                    errors.append(f"所属机位 {slot} 已被服务器 {taken[slot]} 占用")
                    break
        errors.extend(same_kind_ip_conflict_errors(normalized))
    for field in multi_value_fields(entity):
        tags = split_tags(normalized.get(field))
        if len(tags) > TAG_MAX_COUNT:
            errors.append(f"{field}最多选 {TAG_MAX_COUNT} 个标签（当前 {len(tags)} 个）")
        for tag in tags:
            if len(tag) > TAG_MAX_LENGTH:
                errors.append(f"{field}的标签「{tag[:12]}…」超过 {TAG_MAX_LENGTH} 字")
                break
    if existing is not None:
        key = record_key(meta, normalized)
        if any(record_key(meta, x) == key for x in existing):
            errors.append(f"主键 {key} 已存在")
    return normalized, errors


def duplicate_key_exists(meta, records, key, exclude_key=""):
    return any(record_key(meta, item) == key and record_key(meta, item) != exclude_key for item in records)


class FieldOptionInUse(ValueError):
    """标签仍被记录引用，不能删除（只能改名）。"""


def _check_tag_field(entity, field):
    entity = normalize_entity(entity)
    if not is_multi_value_field(entity, field):
        raise ValueError(f"{field} 不是多值标签字段；可用：{'、'.join(multi_value_fields(entity)) or '无'}")
    return entity


def _validated_new_tag(value):
    """新建/改名时的标签校验：必须是单个标签，不能带分隔符。"""
    tag = normalize_tag(value)
    if not tag:
        raise ValueError("标签不能为空")
    if TAG_SEPARATOR_PATTERN.search(clean_value(value)):
        raise ValueError("标签不能包含逗号、顿号或分号（它们是分隔符）")
    if len(tag) > TAG_MAX_LENGTH:
        raise ValueError(f"标签最长 {TAG_MAX_LENGTH} 个字（当前 {len(tag)} 个）")
    return tag


def field_options_list(entity, field):
    entity = _check_tag_field(entity, field)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM field_options WHERE entity=? AND field=? ORDER BY id", (entity, field)
        ).fetchall()
    return [dict(row) for row in rows]


def field_options_map(entity):
    entity = normalize_entity(entity)
    result = {field: [] for field in multi_value_fields(entity)}
    if not result:
        return result
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM field_options WHERE entity=? ORDER BY id", (entity,)
        ).fetchall()
    for row in rows:
        if row["field"] in result:
            result[row["field"]].append(dict(row))
    return result


def tag_usage(entity, field, value, records=None):
    """统计某标签被多少条正式记录 / 待审批变更使用。"""
    entity = _check_tag_field(entity, field)
    rows = records if records is not None else read_records(entity)
    official = sum(1 for row in rows if value in split_tags(row.get(field)))
    pending = 0
    with db() as conn:
        changes = conn.execute(
            "SELECT new_data FROM changes WHERE entity=? AND status='pending'", (entity,)
        ).fetchall()
    for change in changes:
        if value in split_tags(parse_json_dict(change["new_data"]).get(field)):
            pending += 1
    return {"official": official, "pending": pending}


def absorb_field_options(entity, records, actor="", conn=None):
    """把记录里已经出现、但候选池里没有的标签自动收进池（来源标记 data）。

    Excel 手工改值、历史导入、API 直接写入都会走到这里，保证池不会和数据脱节。
    返回新收纳的条数。
    """
    entity = normalize_entity(entity)
    fields = multi_value_fields(entity)
    if not fields:
        return 0
    seen = []
    for row in records:
        for field in fields:
            for tag in split_tags(row.get(field)):
                if (field, tag) not in seen:
                    seen.append((field, tag))
    if not seen:
        return 0
    if conn is None:
        # 独立连接时必须走 with，否则 close 前不会提交（sqlite3 隐式事务会回滚）
        with db() as owned:
            return _absorb_rows(owned, entity, seen, actor)
    return _absorb_rows(conn, entity, seen, actor)


def _absorb_rows(conn, entity, seen, actor):
    added = 0
    stamp = now_text()
    for field, tag in seen:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO field_options (entity,field,value,source,created_at,created_by)"
            " VALUES (?,?,?,?,?,?)",
            (entity, field, tag, "data", stamp, actor),
        )
        added += cursor.rowcount or 0
    return added


def field_options_payload(entity, include_usage=False):
    """候选池对外载荷：字段 → 标签列表（可选带使用次数）。顺带把池外值收纳进池。"""
    entity = normalize_entity(entity)
    fields = list(multi_value_fields(entity))
    if not fields:
        return {"entity": entity, "fields": [], "options": {}}
    records = read_records(entity)
    absorb_field_options(entity, records)
    usage = {}
    if include_usage:
        with db() as conn:
            pending_rows = conn.execute(
                "SELECT new_data FROM changes WHERE entity=? AND status='pending'", (entity,)
            ).fetchall()
        for field in fields:
            counts = {}
            for row in records:
                for tag in split_tags(row.get(field)):
                    counts.setdefault(tag, {"official": 0, "pending": 0})["official"] += 1
            for row in pending_rows:
                for tag in split_tags(parse_json_dict(row["new_data"]).get(field)):
                    counts.setdefault(tag, {"official": 0, "pending": 0})["pending"] += 1
            usage[field] = counts
    options = {}
    for field in fields:
        items = []
        for option in field_options_list(entity, field):
            item = {"value": option["value"], "source": option["source"]}
            if include_usage:
                item["usage"] = usage.get(field, {}).get(option["value"], {"official": 0, "pending": 0})
            items.append(item)
        options[field] = items
    return {"entity": entity, "fields": fields, "options": options}


def create_field_option(entity, field, value, actor=""):
    entity = _check_tag_field(entity, field)
    tag = _validated_new_tag(value)
    with db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM field_options WHERE entity=? AND field=? AND value=?", (entity, field, tag)
        ).fetchone()
        if exists:
            raise ValueError(f"标签「{tag}」已存在")
        total = conn.execute(
            "SELECT COUNT(*) FROM field_options WHERE entity=? AND field=?", (entity, field)
        ).fetchone()[0]
        if total >= TAG_MAX_OPTIONS:
            raise ValueError(f"{field}候选标签已达上限 {TAG_MAX_OPTIONS} 个")
        conn.execute(
            "INSERT INTO field_options (entity,field,value,source,created_at,created_by) VALUES (?,?,?,?,?,?)",
            (entity, field, tag, "manual", now_text(), actor),
        )
    return tag


def _rewrite_pending_tag(entity, field, old_tag, new_tag, conn):
    """把待审批变更 new_data 里的旧标签换掉，避免改名后审批一通过又把旧值写回去。"""
    rows = conn.execute(
        "SELECT id,new_data FROM changes WHERE entity=? AND status='pending'", (entity,)
    ).fetchall()
    changed = 0
    for row in rows:
        data = parse_json_dict(row["new_data"])
        tags = split_tags(data.get(field))
        if old_tag not in tags:
            continue
        data[field] = join_tags([new_tag if tag == old_tag else tag for tag in tags])
        conn.execute(
            "UPDATE changes SET new_data=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), row["id"]),
        )
        changed += 1
    return changed


def rename_field_option(entity, field, value, new_value, actor=""):
    """标签改名：刷新候选池，并级联刷新正式记录 + 待审批变更里的旧值。

    返回 {records, pending} 两处被刷新的条数。
    """
    entity = _check_tag_field(entity, field)
    old_tag = normalize_tag(value)
    new_tag = _validated_new_tag(new_value)
    if old_tag == new_tag:
        return {"records": 0, "pending": 0}
    records = read_records(entity)
    # 池外值（直接改 Excel 产生）先收进池，否则会出现「记录在用、池里查不到」
    absorb_field_options(entity, records)
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM field_options WHERE entity=? AND field=? AND value=?", (entity, field, old_tag)
        ).fetchone()
        if not row:
            raise ValueError(f"标签「{old_tag}」不存在")
        duplicated = conn.execute(
            "SELECT 1 FROM field_options WHERE entity=? AND field=? AND value=?", (entity, field, new_tag)
        ).fetchone()
        if duplicated:
            raise ValueError(f"标签「{new_tag}」已存在，不能重名")
        conn.execute(
            "UPDATE field_options SET value=?, updated_at=?, updated_by=? WHERE id=?",
            (new_tag, now_text(), actor, row["id"]),
        )
    touched = 0
    for record in records:
        tags = split_tags(record.get(field))
        if old_tag in tags:
            record[field] = join_tags([new_tag if tag == old_tag else tag for tag in tags])
            touched += 1
    if touched:
        storage.write_records(entity, records)
    with db() as conn:
        pending = _rewrite_pending_tag(entity, field, old_tag, new_tag, conn)
    return {"records": touched, "pending": pending}


def delete_field_option(entity, field, value, actor=""):
    """删除标签：只要还有正式记录或待审批变更在用它，就拒绝（只能改名）。"""
    entity = _check_tag_field(entity, field)
    tag = normalize_tag(value)
    usage = tag_usage(entity, field, tag)
    if usage["official"] or usage["pending"]:
        raise FieldOptionInUse(
            f"标签「{tag}」仍有 {usage['official']} 台服务器、{usage['pending']} 条待审批在使用，只能改名，不能删除"
        )
    with db() as conn:
        cursor = conn.execute(
            "DELETE FROM field_options WHERE entity=? AND field=? AND value=?", (entity, field, tag)
        )
        if not cursor.rowcount:
            raise ValueError(f"标签「{tag}」不存在")
    return tag


def update_pending_user_references(conn, old_user_id, new_user_id, new_user_name, exclude_change_id):
    rows = conn.execute(
        "SELECT id,entity,operation,record_key,new_data FROM changes WHERE status='pending' AND id<>?",
        (exclude_change_id,),
    ).fetchall()
    for change in rows:
        updates = {}
        if normalize_entity(change["entity"]) == "customers" and change["record_key"] == old_user_id:
            updates["record_key"] = new_user_id
        if change["new_data"]:
            new_data = json.loads(change["new_data"])
            changed = False
            if normalize_entity(change["entity"]) == "servers" and (new_data.get("客户id") == old_user_id or new_data.get("所属客户id") == old_user_id):
                new_data["客户id"] = new_user_id
                new_data["客户名称"] = new_user_name
                new_data.pop("所属客户id", None)
                changed = True
            elif normalize_entity(change["entity"]) == "servers" and new_data.get("客户id") == new_user_id:
                new_data["客户名称"] = new_user_name
                changed = True
            if normalize_entity(change["entity"]) == "customers" and change["record_key"] == old_user_id and (new_data.get("客户id") == old_user_id or new_data.get("id") == old_user_id or new_data.get("用户id") == old_user_id):
                new_data["客户id"] = new_user_id
                new_data.pop("id", None)
                new_data.pop("用户id", None)
                changed = True
            if changed:
                updates["new_data"] = json.dumps(new_data, ensure_ascii=False)
        if updates:
            assignments = ",".join(f"{column}=?" for column in updates)
            conn.execute(f"UPDATE changes SET {assignments} WHERE id=?", (*updates.values(), change["id"]))


def update_pending_rack_customer_references(conn, old_user_id, new_user_id, new_user_name, exclude_request_id=""):
    rows = conn.execute(
        "SELECT id,request_data,stage_data FROM rack_requests WHERE status='pending' AND id<>?",
        (exclude_request_id,),
    ).fetchall()
    for row in rows:
        updates = {}
        request_data = parse_json_dict(row["request_data"])
        if request_data.get("客户id") in {old_user_id, new_user_id}:
            request_data["客户id"] = new_user_id
            request_data["客户名称"] = new_user_name
            updates["request_data"] = json.dumps(request_data, ensure_ascii=False)
        stage_data = parse_json_dict(row["stage_data"])
        if stage_data.get("客户id") in {old_user_id, new_user_id}:
            stage_data["客户id"] = new_user_id
            stage_data["客户名称"] = new_user_name
            updates["stage_data"] = json.dumps(stage_data, ensure_ascii=False)
        if updates:
            assignments = ",".join(f"{column}=?" for column in updates)
            conn.execute(f"UPDATE rack_requests SET {assignments} WHERE id=?", (*updates.values(), row["id"]))


def cascade_customer_change(conn, old_user_id, new_user_id, new_user_name, exclude_change_id):
    servers = read_records("servers")
    changed = False
    for server in servers:
        if server["客户id"] in {old_user_id, new_user_id}:
            server["客户id"] = new_user_id
            server["客户名称"] = new_user_name
            changed = True
    if changed:
        write_records("servers", servers)
    update_pending_user_references(conn, old_user_id, new_user_id, new_user_name, exclude_change_id)
    update_pending_rack_customer_references(conn, old_user_id, new_user_id, new_user_name)


def can_manage_pending_change(change, user):
    if user["身份"] == ROLE_ADMIN or change["submitter"] == user["用户id"]:
        return True
    if change["operation"] == "新增":
        return False
    entity = normalize_entity(change["entity"])
    meta = ENTITIES[entity]
    current = find_record(entity, change["record_key"])
    if not current:
        return False
    return any(record_key(meta, row) == change["record_key"] for row in filter_records_for_user(entity, [current], user))


def validate_pending_change_update(change, candidate):
    if change["operation"] == "删除":
        raise ValueError("删除申请不支持修改")
    entity = normalize_entity(change["entity"])
    meta = ENTITIES[entity]
    records = read_records(entity)
    # 申请提交时就带了占用意图：编辑申请内容后仍然允许占着那些机位，否则用户一改备注就被拦下
    allow_takeover = bool(dict(change).get("takeover")) and entity == "servers"
    if change["operation"] == "新增":
        # 占用模式下同序列号的被替换记录要剔除，否则「主键已存在」会先拦住编辑
        checked = records_for_takeover_validation(records, candidate, allow_takeover)
        normalized, errors = validate_record(
            entity, candidate, checked, allow_occupied_slots=allow_takeover
        )
        key = record_key(meta, normalized)
    else:
        current = next((item for item in records if record_key(meta, item) == change["record_key"]), None)
        if not current:
            raise ValueError("原记录不存在或已变化，请刷新后重试")
        exclude_key = change["record_key"] if entity == "servers" else None
        normalized, errors = validate_record(
            entity, candidate, None, exclude_key, allow_occupied_slots=allow_takeover
        )
        key = record_key(meta, normalized)
        if key != change["record_key"] and entity != "customers":
            errors.append("不允许修改主键字段")
        if entity == "customers" and duplicate_key_exists(meta, records, key, change["record_key"]):
            errors.append(f"主键 {key} 已存在")
    if errors:
        raise ValueError("；".join(errors))
    return key, normalized


def notify_audit_event(conn, event):
    config = read_settings(conn)
    notifier.send_notification_async(config, event, logger=logger, timeout=NOTIFY_TIMEOUT)


def operation_change_event(change, event_type, title, reviewer=""):
    entity = normalize_entity(change["entity"])
    new_data = parse_json_dict(change["new_data"])
    event_time = change.get("reviewed_at") if isinstance(change, dict) else change["reviewed_at"] if "reviewed_at" in change.keys() else ""
    if event_type == "created":
        event_time = change["submitted_at"]
    return {
        "event_type": event_type,
        "audit_type": "操作审核",
        "title": title,
        "entity_label": entity_meta(entity)["label"],
        "operation": change["operation"],
        "record_key": change["record_key"],
        "submitter": change["submitter"],
        "reviewer": reviewer,
        "time": event_time,
        "reject_reason": change.get("reject_reason") if isinstance(change, dict) else change["reject_reason"] if "reject_reason" in change.keys() else "",
        "summary": summarize_record(new_data, entity_meta(entity)["headers"]),
    }


def rack_request_event(row, event_type, title, reviewer=""):
    data = parse_json_dict(row["request_data"])
    stage_data = parse_json_dict(row["stage_data"] if not isinstance(row, dict) and "stage_data" in row.keys() else row.get("stage_data") if isinstance(row, dict) else "{}")
    data.update({key: value for key, value in stage_data.items() if key in {"所在机房", "所属机柜", "所属机位", "管理ip", "交易ip", "带外ip"}})
    event_time = row.get("reviewed_at") if isinstance(row, dict) else row["reviewed_at"] if "reviewed_at" in row.keys() else ""
    if event_type in {"created", "updated", "resubmitted"}:
        event_time = row["submitted_at"]
    reject_reason = row.get("reject_reason") if isinstance(row, dict) else row["reject_reason"] if "reject_reason" in row.keys() else ""
    if event_type in {"created", "updated", "resubmitted"}:
        summary_headers = ["所在机房", "序列号", "客户id", "客户名称"]
    elif event_type == "rejected":
        data["驳回原因"] = reject_reason
        summary_headers = ["所在机房", "序列号", "客户id", "客户名称", "驳回原因"]
    else:
        summary_headers = ["所在机房", "序列号", "客户id", "客户名称", "所属机柜", "所属机位", "管理ip", "交易ip", "带外ip"]
    return {
        "event_type": event_type,
        "audit_type": "服务器上下架审核",
        "title": title,
        "entity_label": "服务器上下架审核",
        "operation": row["request_type"],
        "record_key": row["serial_number"],
        "submitter": row["submitter_id"],
        "reviewer": reviewer,
        "time": event_time,
        "reject_reason": reject_reason,
        "summary": summarize_record(data, summary_headers, limit=len(summary_headers)),
    }


def submit_change(entity, operation, record_key_value, new_data=None, submitter="", takeover=False):
    entity = normalize_entity(entity)
    change_id = uuid.uuid4().hex[:12]
    submitted_at = now_text()
    with LOCK, db() as conn:
        conn.execute("""INSERT INTO changes
            (id,entity,operation,record_key,new_data,status,submitter,submitted_at,takeover)
            VALUES (?,?,?,?,?,?,?,?,?)""", (
            change_id, entity, operation, record_key_value,
            json.dumps(new_data, ensure_ascii=False) if new_data else None,
            "pending", submitter, submitted_at, 1 if takeover else 0,
        ))
        storage.sync_pending_excel(conn)
        notify_audit_event(conn, operation_change_event({
            "id": change_id,
            "entity": entity,
            "operation": operation,
            "record_key": record_key_value,
            "new_data": json.dumps(new_data, ensure_ascii=False) if new_data else None,
            "submitter": submitter,
            "submitted_at": submitted_at,
            "reviewed_at": "",
        }, "created", "操作审核新增待审核项"))
    return change_id


def submit_and_approve_change(entity, operation, record_key_value, new_data=None, submitter="", reviewer="", takeover=False):
    """管理员直改：在单个事务内提交变更并立即批准生效。

    复用 apply_review_action 的批准内核（approve_change + 版本记录 + 审计事件 + 待审核 Excel 同步），
    因此与常规审批相比只是省略了人工点击这一步，其余行为（版本、审计、留痕）完全一致。
    返回 change_id。
    """
    entity = normalize_entity(entity)
    reviewer = reviewer or submitter
    change_id = uuid.uuid4().hex[:12]
    submitted_at = now_text()
    with LOCK, db() as conn:
        conn.execute("""INSERT INTO changes
            (id,entity,operation,record_key,new_data,status,submitter,submitted_at,takeover)
            VALUES (?,?,?,?,?,?,?,?,?)""", (
            change_id, entity, operation, record_key_value,
            json.dumps(new_data, ensure_ascii=False) if new_data else None,
            "pending", submitter, submitted_at, 1 if takeover else 0,
        ))
        change = conn.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
        apply_review_action(conn, change, {"action": "approve"}, reviewer=reviewer, event_title="管理员变更直接生效")
        storage.prune_audit_records(conn)
        storage.sync_pending_excel(conn)
    return change_id


def validate_final_new_data(entity, change, candidate, records, allow_takeover=False):
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    exclude_key = change["record_key"] if entity == "servers" and change["operation"] == "修改" else None
    normalized, errors = validate_record(
        entity, candidate, None, exclude_key, allow_occupied_slots=allow_takeover
    )
    key = record_key(meta, normalized)
    if key != change["record_key"] and not (entity == "customers" and change["operation"] == "修改"):
        errors.append("审核时不允许修改主键字段")
    if entity == "customers" and change["operation"] == "修改" and duplicate_key_exists(meta, records, key, change["record_key"]):
        errors.append(f"记录 {key} 已存在")
    if change["operation"] == "新增" and any(record_key(meta, x) == key for x in records):
        errors.append(f"记录 {key} 已存在")
    if errors:
        raise ValueError("；".join(errors))
    return key


def approve_change(change, final_new_data=None, conn=None, actor=""):
    entity = normalize_entity(change["entity"])
    if change["operation"] == SWAP_OPERATION:
        approve_slot_swap(change, conn)
        return None
    meta = ENTITIES[entity]
    records = read_records(entity)
    new = final_new_data if final_new_data is not None else json.loads(change["new_data"]) if change["new_data"] else None
    if change["operation"] == "新增":
        if not new:
            raise ValueError("新增申请缺少审批后数据")
        takeover = bool(dict(change).get("takeover")) and entity == "servers"
        # 同序列号 + 占用 = 原地覆盖那条记录（不进回收站），校验时要先把它从「已有记录」里剔除
        replacement = takeover_replacement(records, new) if takeover else None
        checked = records_for_takeover_validation(records, new, takeover)
        key = validate_final_new_data(entity, change, new, checked, allow_takeover=takeover)
        if takeover:
            displaced = takeover_victims(checked, new)
            if displaced:
                victims = {clean_value(item.get("序列号")) for item in displaced}
                for victim in displaced:
                    snapshot_deleted_server(
                        victim,
                        change_id=change["id"],
                        deleted_by=actor or dict(change).get("submitter", ""),
                        reason=TRASH_REASON_TAKEOVER,
                        conn=conn,
                    )
                # 先腾位再落新记录：否则新记录和原占用者会同时出现在同一机位上
                records = [item for item in records if clean_value(item.get("序列号")) not in victims]
        if replacement is not None:
            target_serial = clean_value(replacement.get("序列号"))
            records = [
                new if clean_value(item.get("序列号")) == target_serial else item
                for item in records
            ]
        else:
            records.append(new)
    else:
        key = change["record_key"]
        idx = next((i for i, x in enumerate(records) if record_key(meta, x) == key), None)
        if idx is None:
            raise ValueError(f"当前记录 {key} 已不存在，无法审批")
        if change["operation"] == "修改":
            if not new:
                raise ValueError("修改申请缺少审批后数据")
            takeover = bool(dict(change).get("takeover"))
            new_key = validate_final_new_data(entity, change, new, records, allow_takeover=takeover)
            if takeover and entity == "servers":
                displaced = takeover_victims(records, new, exclude_key=key)
                if displaced:
                    victims = {clean_value(item.get("序列号")) for item in displaced}
                    for victim in displaced:
                        snapshot_deleted_server(
                            victim,
                            change_id=change["id"],
                            deleted_by=actor or dict(change).get("submitter", ""),
                            reason=TRASH_REASON_TAKEOVER,
                            conn=conn,
                        )
                    records = [item for item in records if clean_value(item.get("序列号")) not in victims]
                    # 下架了别人之后原下标可能已经错位，按主键重新定位
                    idx = next((i for i, x in enumerate(records) if record_key(meta, x) == key), None)
                    if idx is None:
                        raise ValueError(f"当前记录 {key} 已不存在，无法审批")
            records[idx] = new
        else:
            # 删除也走回收站：先落快照再删行，误删还能恢复（与下架实施完成同一条通路）
            if entity == "servers":
                snapshot_deleted_server(
                    deleted=records[idx],
                    change_id=change["id"],
                    deleted_by=actor or dict(change).get("submitter", ""),
                    conn=conn,
                )
            records = [record for record in records if record_key(meta, record) != key]
    write_records(entity, records)
    # 写库成功后把出现的新标签自动收进候选池（Excel 手工改、导入、API 写入都走这里）
    absorb_field_options(entity, records, conn=conn)
    if conn and entity == "customers" and change["operation"] == "修改":
        cascade_customer_change(conn, change["record_key"], new_key, new.get("名称", ""), change["id"])
    return new


def create_version(conn, change, reviewer):
    return storage.create_version_record(conn, change["id"], change["entity"], change["operation"], change["record_key"], reviewer)


def apply_review_action(conn, change, body, reviewer, version_code=None, event_title=None):
    action = body.get("action")
    if action == "approve":
        final_new_data = body.get("new_data") if "new_data" in body else None
        approved_data = approve_change(change, final_new_data, conn, actor=reviewer)
        version_code = version_code or create_version(conn, change, reviewer)
        if approved_data is not None:
            conn.execute("UPDATE changes SET new_data=? WHERE id=?", (json.dumps(approved_data, ensure_ascii=False), change["id"]))
        status, message = "approved", f"审核通过，已生成版本 {version_code}"
    else:
        status, message = "rejected", "申请已驳回"
    reviewed_at = now_text()
    conn.execute(
        "UPDATE changes SET status=?,reviewer=?,reviewed_at=?,reject_reason=? WHERE id=?",
        (status, reviewer, reviewed_at, body.get("reason", ""), change["id"]),
    )
    if action == "approve":
        event_change = dict(change)
        event_change["reviewed_at"] = reviewed_at
        if approved_data is not None:
            event_change["new_data"] = json.dumps(approved_data, ensure_ascii=False)
        notify_audit_event(conn, operation_change_event(event_change, "approved", event_title or "操作审核已通过", reviewer))
    else:
        event_change = dict(change)
        event_change["reviewed_at"] = reviewed_at
        event_change["reject_reason"] = body.get("reason", "")
        notify_audit_event(conn, operation_change_event(event_change, "rejected", "操作审核已驳回", reviewer))
    return message


SWAP_OPERATION = "交换机位"


def _slot_matches(room, cabinet_name, slot, target_room, target_cabinet, target_slot):
    """判断记录 (机房, 机柜, 机位范围) 是否覆盖目标机位。"""
    if clean_value(room) != clean_value(target_room) or clean_value(cabinet_name) != clean_value(target_cabinet):
        return False
    return str(target_slot) in {str(pos) for pos in positions(slot)}


def slot_swap_target(room, cabinet_name, slot, exclude_server=""):
    """定位某机位上当前的占用者，返回可作为交换对象的载体信息。

    优先级与机柜统计/机位选择器一致：待审核变更 > 待实施请求 > 正式记录。
    返回结构：{kind, key, room, cabinet, slot, slots?, change_id?, request_id?, server?}
    其中 slot 为被点击的单个机位，slots 为占用者占用的**完整机位范围**（可能多行），
    交换需按 slots 整段对调。
    """
    room, cabinet_name, slot = clean_value(room), clean_value(cabinet_name), str(slot)
    exclude = clean_value(exclude_server)
    meta = ENTITIES["servers"]

    with db() as conn:
        pending_rows = conn.execute(
            "SELECT id,operation,record_key,new_data FROM changes "
            "WHERE status='pending' AND entity='servers' AND operation IN ('新增','修改')"
        ).fetchall()
    for row in pending_rows:
        if not row["new_data"]:
            continue
        data = normalize_row("servers", json.loads(row["new_data"]))
        if record_key(meta, data) == exclude:
            continue
        if _slot_matches(data.get("所在机房"), data.get("所属机柜"), data.get("所属机位"), room, cabinet_name, slot):
            return {
                "kind": "pending_change",
                "key": record_key(meta, data),
                "room": room,
                "cabinet": cabinet_name,
                "slot": slot,
                "slots": clean_value(data.get("所属机位")),
                "change_id": row["id"],
                "pending_operation": row["operation"],
                "server": data,
            }

    with db() as conn:
        request_rows = conn.execute(
            "SELECT id,serial_number,stage_data FROM rack_requests "
            "WHERE status='pending' AND stage='implementation' AND request_type='上架'"
        ).fetchall()
    for row in request_rows:
        data = parse_json_dict(row["stage_data"])
        if _slot_matches(data.get("所在机房"), data.get("所属机柜"), data.get("所属机位"), room, cabinet_name, slot):
            return {
                "kind": "rack_request",
                "key": clean_value(row["serial_number"]),
                "room": room,
                "cabinet": cabinet_name,
                "slot": slot,
                "slots": clean_value(data.get("所属机位")),
                "request_id": row["id"],
                "server": data,
            }

    for server in read_records("servers"):
        if record_key(meta, server) == exclude:
            continue
        if _slot_matches(server.get("所在机房"), server.get("所属机柜"), server.get("所属机位"), room, cabinet_name, slot):
            return {
                "kind": "official",
                "key": record_key(meta, server),
                "room": room,
                "cabinet": cabinet_name,
                "slot": slot,
                "slots": clean_value(server.get("所属机位")),
                "server": server,
            }
    return None


def build_swap_change_data(self_new, self_old, target):
    """构造交换机位单的 new_data 载荷（我方新数据 + 对方载体引用）。"""
    meta = ENTITIES["servers"]
    return {
        "kind": "slot_swap",
        "self": self_new,
        "self_key": record_key(meta, self_new),
        "self_from": {
            "room": clean_value(self_old.get("所在机房")),
            "cabinet": clean_value(self_old.get("所属机柜")),
            "slot": clean_value(self_old.get("所属机位")),
        },
        "target": {
            "kind": target["kind"],
            "key": target["key"],
            "change_id": target.get("change_id", ""),
            "request_id": target.get("request_id", ""),
            "from": {
                "room": clean_value(target.get("room")),
                "cabinet": clean_value(target.get("cabinet")),
                # 对方完整机位范围（可能多行），交换按整段对调
                "slot": clean_value(target.get("slots") or target.get("slot")),
            },
        },
    }


def pending_swap_for_record(entity, record_key_value, conn=None):
    """该服务器是否处于待审核的交换机位单中（作为我方或对方）。"""
    entity = normalize_entity(entity)
    if entity != "servers":
        return None
    owns = conn is None
    conn = conn or db()
    rows = conn.execute(
        "SELECT id,record_key,new_data FROM changes WHERE status='pending' AND entity='servers' AND operation=?",
        (SWAP_OPERATION,),
    ).fetchall()
    if owns:
        conn.close()
    for row in rows:
        data = parse_json_dict(row["new_data"])
        if clean_value(row["record_key"]) == clean_value(record_key_value):
            return dict(row)
        target = data.get("target") or {}
        if clean_value(target.get("key")) == clean_value(record_key_value):
            return dict(row)
    return None


def submit_slot_swap(self_new, self_old, target, submitter=""):
    """提交机位交换单：我方服务器生成一条「交换机位」变更，对方载体在审批时联动改写。"""
    self_new = normalize_row("servers", self_new)
    payload = build_swap_change_data(self_new, self_old or {}, target)
    return submit_change("servers", SWAP_OPERATION, payload["self_key"], payload, submitter=submitter)


def submit_and_approve_slot_swap(self_new, self_old, target, submitter="", reviewer=""):
    """管理员直改：单个事务内提交并批准一次机位交换。"""
    self_new = normalize_row("servers", self_new)
    payload = build_swap_change_data(self_new, self_old or {}, target)
    reviewer = reviewer or submitter
    change_id = uuid.uuid4().hex[:12]
    submitted_at = now_text()
    with LOCK, db() as conn:
        conn.execute("""INSERT INTO changes
            (id,entity,operation,record_key,new_data,status,submitter,submitted_at)
            VALUES (?,?,?,?,?,?,?,?)""", (
            change_id, "servers", SWAP_OPERATION, payload["self_key"],
            json.dumps(payload, ensure_ascii=False), "pending", submitter, submitted_at,
        ))
        change = conn.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
        apply_review_action(conn, change, {"action": "approve"}, reviewer=reviewer, event_title="管理员机位交换直接生效")
        storage.prune_audit_records(conn)
        storage.sync_pending_excel(conn)
    return change_id


def _apply_swap_to_pending_target(conn, data, target):
    """把对方（待审核变更 / 待实施请求）的机位改写为我方原位置。"""
    self_from = data.get("self_from") or {}
    to_room = clean_value(self_from.get("room"))
    to_cabinet = clean_value(self_from.get("cabinet"))
    to_slot = clean_value(self_from.get("slot"))
    kind = target.get("kind")
    if kind == "pending_change":
        row = conn.execute("SELECT id,new_data,status FROM changes WHERE id=?", (target.get("change_id"),)).fetchone()
        if not row or row["status"] != "pending":
            raise ValueError("交换对象对应的待审核申请已变化，请刷新后重试")
        payload = json.loads(row["new_data"]) if row["new_data"] else {}
        payload["所在机房"] = to_room
        payload["所属机柜"] = to_cabinet
        payload["所属机位"] = to_slot
        conn.execute("UPDATE changes SET new_data=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"]))
        return
    if kind == "rack_request":
        row = conn.execute(
            "SELECT id,stage_data,status FROM rack_requests WHERE id=?", (target.get("request_id"),)
        ).fetchone()
        if not row or row["status"] != "pending":
            raise ValueError("交换对象对应的上下架请求已变化，请刷新后重试")
        payload = parse_json_dict(row["stage_data"])
        payload["所在机房"] = to_room
        payload["所属机柜"] = to_cabinet
        payload["所属机位"] = to_slot
        conn.execute("UPDATE rack_requests SET stage_data=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), row["id"]))
        return


def approve_slot_swap(change, conn):
    """交换机位单批准：我方写入新机位，对方按载体类型联动。"""
    data = json.loads(change["new_data"]) if change["new_data"] else {}
    if data.get("kind") != "slot_swap":
        raise ValueError("交换申请数据无效")
    meta = ENTITIES["servers"]
    self_new = normalize_row("servers", data.get("self") or {})
    self_key = change["record_key"]
    target = data.get("target") or {}
    records = read_records("servers")
    idx = next((i for i, x in enumerate(records) if record_key(meta, x) == self_key), None)
    if idx is None:
        raise ValueError(f"服务器 {self_key} 已不存在，无法完成交换")
    if target.get("kind") == "official":
        peer_key = clean_value(target.get("key"))
        peer_idx = next((i for i, x in enumerate(records) if record_key(meta, x) == peer_key), None)
        if peer_idx is None:
            raise ValueError(f"交换对象 {peer_key} 已不存在，无法完成交换")
        self_from = data.get("self_from") or {}
        peer = dict(records[peer_idx])
        peer["所在机房"] = clean_value(self_from.get("room"))
        peer["所属机柜"] = clean_value(self_from.get("cabinet"))
        peer["所属机位"] = clean_value(self_from.get("slot"))
        records[idx] = self_new
        records[peer_idx] = peer
        write_records("servers", records)
        return
    records[idx] = self_new
    write_records("servers", records)
    _apply_swap_to_pending_target(conn, data, target)


def normalize_rack_request(row):
    """请求数据归一：字段白名单化 + 多值标签统一分隔符。

    请求类型缺省按「上架」处理；下架/变更针对已有服务器，客户与机房缺省用正式
    记录补齐（表单上仍可改，校验时会按请求类型分别约束）。
    """
    canonical = {}
    for key, value in (row or {}).items():
        target = normalize_rack_request_header(key)
        if target in RACK_REQUEST_HEADERS:
            canonical[target] = value
    data = {field: normalize_rack_request_value(field, canonical.get(field, "")) for field in RACK_REQUEST_HEADERS}
    if not data["请求类型"]:
        data["请求类型"] = "上架"
    if data["请求类型"] in {"下架", "变更"} and data["序列号"]:
        server = next((item for item in read_records("servers") if item["序列号"] == data["序列号"]), None)
        if server:
            for field in ("客户id", "客户名称", "所在机房"):
                if not data[field]:
                    data[field] = clean_value(server.get(field, ""))
    if data["客户id"]:
        data = apply_customer_name(data)
    return data


def validate_rack_request(data):
    errors = []
    request_type = data["请求类型"]
    if not data["序列号"]:
        errors.append("序列号不能为空")
    if request_type not in REQUEST_TYPES:
        errors.append(f"请求类型必须是 {'、'.join(REQUEST_TYPES)} 之一")
    if not data["是否重装系统"]:
        errors.append("是否重装系统不能为空")
    elif data["是否重装系统"] not in {"是", "否"}:
        errors.append("是否重装系统必须是 是 或 否")
    elif data["是否重装系统"] == "否" and not data["初始密码"]:
        errors.append("是否重装系统为否时初始密码不能为空")
    servers = read_records("servers")
    server_by_serial = {row["序列号"]: row for row in servers}
    existing_server = server_by_serial.get(data["序列号"])
    customer_ids = {row["客户id"] for row in read_records("customers")}
    room_names = {row["机房"] for row in read_records("cabinets") if row["机房"]}
    if request_type == "上架":
        # 序列号已存在不再直接拒绝：上架支持「同序列号原地替换」——机位点选那台服务器
        # 所在的机位并开启占用，实施时用本次数据覆盖原记录（见 takeover_replacement）。
        # 机位要到分配阶段才确定，这里只校验客户与机房，重复由分配/实施阶段判定。
        if not data["客户id"]:
            errors.append("客户id不能为空")
        elif data["客户id"] not in customer_ids:
            errors.append(f"客户id {data['客户id']} 不存在")
        if not data["所在机房"]:
            errors.append("所在机房不能为空")
        elif data["所在机房"] not in room_names:
            errors.append(f"所在机房 {data['所在机房']} 不存在")
        return errors
    # 下架 / 变更：必须指向一条现存服务器记录
    if not existing_server:
        errors.append(f"序列号 {data['序列号']} 不存在，无法提交{request_type}请求")
        return errors
    if request_type == "下架":
        # 下架不允许顺手改客户/机房，避免误改台账
        if data["客户id"] != clean_value(existing_server.get("客户id", "")):
            errors.append(f"客户id必须与服务器 {data['序列号']} 的客户id {existing_server.get('客户id', '')} 一致")
        if data["所在机房"] != clean_value(existing_server.get("所在机房", "")):
            errors.append(f"所在机房必须与服务器 {data['序列号']} 的所在机房 {existing_server.get('所在机房', '')} 一致")
    else:
        # 变更：客户与机房允许改，但必须已登记
        if data["客户id"] and data["客户id"] not in customer_ids:
            errors.append(f"客户id {data['客户id']} 不存在")
        if data["所在机房"] and data["所在机房"] not in room_names:
            errors.append(f"所在机房 {data['所在机房']} 不存在")
    return errors


def pending_rack_request_duplicate(conn, serial_number, exclude_request_id=""):
    if not serial_number:
        return None
    row = conn.execute(
        "SELECT id FROM rack_requests WHERE status='pending' AND serial_number=? AND id<>? LIMIT 1",
        (serial_number, exclude_request_id),
    ).fetchone()
    return row["id"] if row else None


def importable_rack_request_duplicate(conn, serial_number):
    if not serial_number:
        return None
    return conn.execute(
        """SELECT * FROM rack_requests
           WHERE status IN ('pending','rejected') AND serial_number=?
           ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, submitted_at DESC, rowid DESC
           LIMIT 1""",
        (serial_number,),
    ).fetchone()


def insert_rack_request(conn, data, user, submitted_at=None):
    request_id = uuid.uuid4().hex[:12]
    request_type = data["请求类型"]
    stage = initial_rack_request_stage(request_type)
    submitted_at = submitted_at or now_text()
    conn.execute("""INSERT INTO rack_requests
        (id,serial_number,request_type,request_data,status,submitter_id,submitted_at,stage,stage_data)
        VALUES (?,?,?,?,?,?,?,?,?)""", (
            request_id, data["序列号"], request_type,
            json.dumps(data, ensure_ascii=False), "pending", user["用户id"], submitted_at, stage, "{}",
        ))
    notify_audit_event(conn, rack_request_event({
        "id": request_id,
        "serial_number": data["序列号"],
        "request_type": request_type,
        "request_data": json.dumps(data, ensure_ascii=False),
        "status": "pending",
        "submitter_id": user["用户id"],
        "submitted_at": submitted_at,
        "reviewed_at": "",
    }, "created", "服务器上下架审核新增待审核项"))
    return request_id


def overwrite_rack_request(conn, row, data, user, submitted_at=None):
    request_type = data["请求类型"]
    stage = initial_rack_request_stage(request_type)
    submitted_at = submitted_at or now_text()
    conn.execute(
        """UPDATE rack_requests
           SET serial_number=?,request_type=?,request_data=?,status='pending',submitter_id=?,
               submitted_at=?,stage=?,stage_data='{}',
               reviewer_id='',reviewed_at='',reject_reason='',
               allocation_reviewer_id='',allocation_at='',implementation_reviewer_id='',implementation_at=''
           WHERE id=?""",
        (
            data["序列号"], request_type, json.dumps(data, ensure_ascii=False), user["用户id"],
            submitted_at, stage, row["id"],
        ),
    )
    event_type = "resubmitted" if row["status"] == "rejected" else "updated"
    title = "服务器上下架请求已重新提交" if row["status"] == "rejected" else "服务器上下架请求已更新"
    notify_audit_event(conn, rack_request_event({
        "id": row["id"],
        "serial_number": data["序列号"],
        "request_type": request_type,
        "request_data": json.dumps(data, ensure_ascii=False),
        "status": "pending",
        "stage": stage,
        "stage_data": "{}",
        "submitter_id": user["用户id"],
        "submitted_at": submitted_at,
        "reviewed_at": "",
        "reject_reason": "",
    }, event_type, title))
    return row["id"]


def rack_request_rows_from_upload(uploaded):
    source_label, values = storage.uploaded_table_rows(uploaded, RACK_REQUEST_META) if uploaded else (None, None)
    if values is None:
        raise ValueError("请选择 .xlsx 或 .csv 文件")
    rows = list(values)
    if not rows:
        raise ValueError("导入文件不能为空")
    headers = [normalize_rack_request_header(value) for value in rows[0]]
    missing = [header for header in RACK_REQUEST_HEADERS if header not in headers]
    if missing:
        raise ValueError(f"导入文件缺少字段：{'、'.join(missing)}")
    data_rows = []
    for row_index, values in enumerate(rows[1:], start=2):
        raw_values = [clean_value(value) for value in values]
        if not any(raw_values):
            continue
        raw = {header: value for header, value in zip(headers, raw_values) if header in RACK_REQUEST_HEADERS}
        data_rows.append((row_index, normalize_rack_request(raw)))
    if not data_rows:
        raise ValueError("导入文件没有可提交的数据行")
    return source_label, data_rows


def rack_request_to_json(row):
    data = json.loads(row["request_data"])
    stage_data = parse_json_dict(row["stage_data"] if "stage_data" in row.keys() else "{}")
    base_data = {}
    if row["request_type"] == "变更":
        # 变更的实施表单要以现行服务器记录为底（请求里只有部分字段）
        serial = clean_value(row["serial_number"])
        base_data = next((item for item in read_records("servers") if item["序列号"] == serial), {})
    item = dict(data)
    item.update({
        "id": row["id"],
        "request_type": row["request_type"],
        "status": row["status"],
        "stage": row["stage"] if "stage" in row.keys() else "",
        "stage_data": stage_data,
        "提单人id": row["submitter_id"],
        "submitted_at": row["submitted_at"],
        "reviewer_id": row["reviewer_id"],
        "reviewed_at": row["reviewed_at"],
        "reject_reason": row["reject_reason"],
        "request_data": data,
        "base_data": base_data,
    })
    return item


def rack_request_export_records(rows):
    """把上下架请求摊平成可导出的行：请求字段 + 机位/IP 分配结果（stage_data 优先）。"""
    records = []
    for row in rows:
        item = rack_request_to_json(row)
        stage_data = item.get("stage_data") or {}
        flat = {}
        for header in RACK_REQUEST_EXPORT_HEADERS:
            value = stage_data.get(header) or item.get(header) or ""
            flat[header] = clean_value(value)
        records.append(flat)
    return records


def rack_request_rack_date(row):
    """上下架请求对应的服务器记录「上架时间」默认值：取请求提交日期。

    只取到日期（YYYY-MM-DD）：提交上下架请求即视为当天上架；机位IP分配 / 具体实施
    阶段可以在表单里改这个日期，改了就以表单值优先（见 rack_server_draft）。
    """
    submitted_at = row["submitted_at"] if "submitted_at" in row.keys() else ""
    return date_text(submitted_at, today_text())


def rack_server_draft(row, override=None, base=None):
    """请求 → 服务器记录草稿。

    base 用于「变更」请求：以现行服务器记录为底，请求里留空的字段不会把已有值冲掉；
    override 是表单提交值，允许显式清空（原样覆盖）。
    """
    data = parse_json_dict(row["request_data"])
    stage_data = parse_json_dict(row["stage_data"] if "stage_data" in row.keys() else "{}")
    override = override or {}
    draft = dict(base or {})
    for source in (data, stage_data):
        draft.update({key: value for key, value in source.items() if clean_value(value)})
    draft.update(override)
    draft.update({
        "客户id": override.get("客户id") or stage_data.get("客户id") or data.get("客户id", "") or draft.get("客户id", ""),
        "客户名称": override.get("客户名称") or stage_data.get("客户名称") or data.get("客户名称", "") or draft.get("客户名称", ""),
        "所在机房": override.get("所在机房") or stage_data.get("所在机房") or data.get("所在机房", "") or draft.get("所在机房", ""),
        # 分配/实施表单里手动填了上架时间就用它，否则回落到请求提交日期（不允许留空）
        "上架时间": date_text(
            override.get("上架时间") or stage_data.get("上架时间") or draft.get("上架时间"),
            rack_request_rack_date(row),
        ),
    })
    return normalize_row("servers", draft)


def implementation_slot_conflict_errors(server_data, request_id=""):
    """机位在途占用冲突：与 IP 的 include_implementation 逻辑对齐。"""
    room = clean_value(server_data.get("所在机房"))
    cabinet_name = clean_value(server_data.get("所属机柜"))
    if not room or not cabinet_name:
        return []
    taken = implementation_slot_states(exclude_request_id=request_id).get((room, cabinet_name), {})
    serial = clean_value(server_data.get("序列号"))
    errors = []
    for slot in positions(clean_value(server_data.get("所属机位"))):
        holder = taken.get(slot)
        if holder and holder["server"] != serial:
            errors.append(f"所属机位 {slot} 已被待具体实施服务器 {holder['server']} 占用")
            break
    return errors


def takeover_victims(records, server_data, exclude_key=""):
    """目标机位上被别的服务器占用的记录 —— 这些记录会被整台移到回收站。

    机位是物理连续分配的，只取「相交的那几 U」没有意义：一台机器占 18-19，
    新机器只要 19，整台都必须下架，18-19 才能腾出来。
    """
    room = clean_value(server_data.get("所在机房"))
    cabinet_name = clean_value(server_data.get("所属机柜"))
    target = {str(pos) for pos in positions(clean_value(server_data.get("所属机位")))}
    if not room or not cabinet_name or not target:
        return []
    exclude = clean_value(exclude_key)
    victims = []
    for record in records:
        if exclude and clean_value(record.get("序列号")) == exclude:
            continue
        if clean_value(record.get("所在机房")) != room:
            continue
        if clean_value(record.get("所属机柜")) != cabinet_name:
            continue
        if target & {str(pos) for pos in positions(clean_value(record.get("所属机位")))}:
            victims.append(record)
    return victims


def takeover_replacement(records, server_data, exclude_key=""):
    """同序列号的「原地替换」对象 —— 占用模式下要覆盖而不是新增的那条记录。

    上架/新增时如果填的序列号正好等于目标机位上那台服务器，就不该「新机顶掉旧机
    再写一条」：同一台机器不会同时存在两条记录。这种情况按**原地覆盖**处理——
    用本次填写的数据换掉服务器表里那一行，不写回收站（机器并没有下架）。
    """
    serial = clean_value(server_data.get("序列号"))
    if not serial:
        return None
    for victim in takeover_victims(records, server_data, exclude_key=exclude_key):
        if clean_value(victim.get("序列号")) == serial:
            return victim
    return None


def records_for_takeover_validation(records, server_data, allow_takeover, exclude_key=""):
    """校验用的「已有记录」：占用模式下先剔除将被原地覆盖的同序列号记录。

    否则 validate_record 的「主键已存在」会先报错，根本走不到腾位那一步。
    只用于校验与算受害者，落库仍然操作原始 records。
    """
    if not allow_takeover:
        return records
    replacement = takeover_replacement(records, server_data, exclude_key=exclude_key)
    if not replacement:
        return records
    target = clean_value(replacement.get("序列号"))
    return [item for item in records if clean_value(item.get("序列号")) != target]


def validate_rack_allocation(server_data, request_id="", allow_takeover=False):
    required = ["所在机房", "所属机柜", "所属机位", "管理ip", "带外ip"]
    errors = [f"{field}不能为空" for field in required if not clean_value(server_data.get(field))]
    records = read_records("servers")
    replacement = takeover_replacement(records, server_data) if allow_takeover else None
    serial = clean_value(server_data.get("序列号"))
    # 同序列号原地覆盖时，那条记录要先从「已有记录」里剔除，否则主键重复会先报错
    checked = records_for_takeover_validation(records, server_data, allow_takeover)
    if serial and replacement is None:
        duplicate = next((item for item in records if clean_value(item.get("序列号")) == serial), None)
        if duplicate:
            location = " / ".join(
                clean_value(duplicate.get(field)) for field in ("所在机房", "所属机柜", "所属机位")
            )
            errors.append(
                f"序列号 {serial} 已在 {location} 使用：要替换它请点选该机位并开启占用模式，"
                "否则请改用「变更」请求"
            )
            # 已经给出准确提示，不再重复报「主键已存在」
            checked = [item for item in checked if clean_value(item.get("序列号")) != serial]
    _, record_errors = validate_record(
        "servers", server_data, checked, allow_occupied_slots=allow_takeover
    )
    return errors + record_errors + implementation_slot_conflict_errors(server_data, request_id) + same_kind_ip_conflict_errors(
        normalize_row("servers", server_data),
        include_official=False,
        include_implementation=True,
        exclude_request_id=request_id,
    )


def notify_rack_stage(conn, row, event_type, title, reviewer, server_data=None):
    event_row = dict(row)
    if server_data:
        data = parse_json_dict(event_row["request_data"])
        data.update({key: server_data.get(key, "") for key in ["序列号", "客户id", "客户名称", "所属机柜", "所属机位", "管理ip", "交易ip", "带外ip"]})
        data["所在机房"] = server_data.get("所在机房", data.get("所在机房", ""))
        event_row["request_data"] = json.dumps(data, ensure_ascii=False)
        event_row["stage_data"] = json.dumps(server_data, ensure_ascii=False)
    notify_audit_event(conn, rack_request_event(event_row, event_type, title, reviewer))


def complete_rack_request(conn, row, user, server_data=None, takeover=False):
    """具体实施收尾：按请求类型新增 / 更新 / 移入回收站。

    takeover=True 表示允许占走已被占用的机位：目标机位上原有的服务器会整台
    移进回收站（reason=机位被占用），腾出位置后再写入本次记录。
    例外：占用对象与本次序列号相同时按「原地覆盖」处理——同一台机器换配置/换位置，
    用新数据替换那一行，不写回收站。
    """
    records = read_records("servers")
    request_type = row["request_type"]
    key = row["serial_number"]
    event_server_data = server_data or {}
    operation = {"上架": "新增", "变更": "更新"}.get(request_type, "删除")
    final_serial = key
    displaced = []
    replaced = False
    if request_type == "上架":
        replacement = takeover_replacement(records, server_data or {}) if takeover else None
        checked = records_for_takeover_validation(records, server_data or {}, takeover)
        normalized, errors = validate_record(
            "servers", server_data or {}, checked, allow_occupied_slots=takeover
        )
        if errors:
            return None, errors
        event_server_data = normalized
        final_serial = record_key(ENTITIES["servers"], normalized)
        if takeover:
            displaced = takeover_victims(checked, normalized)
            if displaced:
                victims = {clean_value(item.get("序列号")) for item in displaced}
                for victim in displaced:
                    move_server_to_trash(
                        conn, victim, request_id=row["id"], deleted_by=user["用户id"],
                        reason=TRASH_REASON_TAKEOVER,
                    )
                records = [item for item in records if clean_value(item.get("序列号")) not in victims]
        if replacement is not None:
            # 同序列号：覆盖原记录，不新增、不写回收站
            target_serial = clean_value(replacement.get("序列号"))
            records = [
                normalized if clean_value(item.get("序列号")) == target_serial else item
                for item in records
            ]
            replaced = True
        else:
            records.append(normalized)
        write_records("servers", records)
    elif request_type == "变更":
        current = next((item for item in records if item["序列号"] == key), None)
        if not current:
            return None, ["服务器记录不存在"]
        current_key = record_key(ENTITIES["servers"], current)
        merged = rack_server_draft(row, server_data, base=current)
        merged["序列号"] = key  # 序列号是定位键，变更不允许改
        normalized, errors = validate_record(
            "servers", merged, exclude_record_key=current_key, allow_occupied_slots=takeover
        )
        if errors:
            return None, errors
        event_server_data = normalized
        if takeover:
            displaced = takeover_victims(records, normalized, exclude_key=key)
            if displaced:
                victims = {clean_value(item.get("序列号")) for item in displaced}
                for victim in displaced:
                    move_server_to_trash(
                        conn, victim, request_id=row["id"], deleted_by=user["用户id"],
                        reason=TRASH_REASON_TAKEOVER,
                    )
                records = [item for item in records if clean_value(item.get("序列号")) not in victims]
        records = [normalized if item["序列号"] == key else item for item in records]
        write_records("servers", records)
    else:
        current = next((item for item in records if item["序列号"] == key), None)
        if not current:
            return None, ["服务器记录不存在"]
        event_server_data = current
        records = [record for record in records if record["序列号"] != key]
        write_records("servers", records)
        move_server_to_trash(conn, current, request_id=row["id"], deleted_by=user["用户id"], reason=TRASH_REASON_RACK_DOWN)
    version_code = storage.create_version_record(conn, row["id"], "servers", operation, final_serial, user["用户id"])
    reviewed_at = now_text()
    conn.execute("""UPDATE rack_requests
        SET status='approved',stage='completed',reviewer_id=?,reviewed_at=?,reject_reason='',
            implementation_reviewer_id=?,implementation_at=?,stage_data=?,serial_number=?
        WHERE id=?""", (
        user["用户id"], reviewed_at, user["用户id"], reviewed_at,
        json.dumps(event_server_data, ensure_ascii=False), final_serial, row["id"],
    ))
    event_row = dict(row)
    event_row["reviewed_at"] = reviewed_at
    notes = []
    if displaced:
        titles = "、".join(clean_value(item.get("序列号")) for item in displaced)
        notes.append(f"占用的机位原有服务器 {titles} 已下架并移入回收站")
    if replaced:
        # 同序列号原地覆盖：机器没下架，只是记录被新数据替换，不进回收站
        notes.append(f"同序列号 {final_serial} 已原地覆盖原记录")
    message = f"服务器{request_type}具体实施已完成，已生成版本 {version_code}"
    if notes:
        message += "；" + "；".join(notes)
    notify_rack_stage(conn, event_row, "approved", "服务器上下架具体实施已完成", user["用户id"], event_server_data)
    return message, []


# ---------------------------------------------------------------- 回收站
# 四个入口会把记录写进 trash 表：
#   1. 下架实施完成（complete_rack_request）——服务器整行快照，服务器表删行
#   2. 页面上的「删除 / 批量删除」（approve_change 的删除分支）——同上
#   3. 回收站里手动登记（add_manual_trash_server）——只有序列号，其余字段等上架时补
#   4. 上架/变更占用已有机位（takeover）——原占用者整台下架，reason=机位被占用
# 管理人员可查看 / 重新上架 / 永久删除；机房人员只看本机房，可查看与重新上架。
# 永久删除会连带清掉该序列号的资料附件。
# 重新上架时可以改**整行任意字段**（含序列号）：改序列号时该序列号的资料附件一起改名。

TRASH_REASON_RACK_DOWN = "下架实施完成"
TRASH_REASON_DELETE = "删除操作（未走上下架流程）"
TRASH_REASON_MANUAL = "手动登记（信息待补齐）"
TRASH_REASON_TAKEOVER = "机位被新服务器占用，自动下架"

# origin='' 表示来自服务器表的快照；'manual' 表示回收站里手动登记的占位记录。
TRASH_ORIGIN_MANUAL = "manual"


def move_server_to_trash(conn, server, request_id="", deleted_by="", reason="", origin=""):
    trash_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO trash (id,serial_number,data,request_id,deleted_at,deleted_by,reason,origin)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            trash_id, clean_value(server.get("序列号")), json.dumps(server, ensure_ascii=False),
            request_id, now_text(), deleted_by, reason, origin,
        ),
    )
    return trash_id


def add_manual_trash_server(serial, actor="", extra=None):
    """在回收站手动登记一台服务器：序列号必填，其他字段留空等重新上架时再补。

    这条记录只是占位（origin='manual'），不参与任何服务器校验；补全信息的校验
    发生在重新上架那一刻，所以这里只挡「序列号为空」「已存在」这类硬冲突。
    """
    serial = clean_value(serial)
    if not serial:
        raise ValueError("序列号不能为空")
    if any(clean_value(row.get("序列号")) == serial for row in read_records("servers")):
        raise ValueError(f"序列号 {serial} 已在服务器表中，无需在回收站登记")
    headers = ENTITIES["servers"]["headers"]
    extra = {key: value for key, value in (extra or {}).items() if key in headers and key != "序列号"}
    with db() as conn:
        duplicate = conn.execute("SELECT id FROM trash WHERE serial_number=?", (serial,)).fetchone()
        if duplicate:
            raise ValueError(f"序列号 {serial} 已在回收站中，请直接编辑后重新上架")
        server = normalize_row("servers", {"序列号": serial, **extra})
        trash_id = move_server_to_trash(
            conn, server, request_id="", deleted_by=actor,
            reason=TRASH_REASON_MANUAL, origin=TRASH_ORIGIN_MANUAL,
        )
        notify_audit_event(conn, {
            "event_type": "manual_added",
            "audit_type": "回收站",
            "title": "回收站手动登记服务器",
            "entity_label": "服务器",
            "operation": "手动登记",
            "record_key": serial,
            "reviewer": actor,
            "time": now_text(),
            "summary": summarize_record(server, ["序列号"]) or serial,
        })
    return trash_id


def snapshot_deleted_server(deleted, change_id="", deleted_by="", reason=TRASH_REASON_DELETE, conn=None):
    """把被删的服务器整行写进回收站。

    调用方通常已经持有 db 连接（审批事务内），传进来就复用同一事务；
    没有连接时自己开一个。快照必须先于删行落地，写入失败宁可整体报错也不能丢数据。
    """
    if conn is not None:
        return move_server_to_trash(conn, deleted, request_id=change_id, deleted_by=deleted_by, reason=reason)
    with db() as own:
        return move_server_to_trash(own, deleted, request_id=change_id, deleted_by=deleted_by, reason=reason)


def trash_rows(room=""):
    """回收站记录。room 非空时只返回该机房的 —— 机房人员只看本机房。

    手动登记的占位记录没有「所在机房」，只对管理人员可见（也只能由管理人员登记）。
    """
    with db() as conn:
        rows = conn.execute("SELECT * FROM trash ORDER BY deleted_at DESC, rowid DESC").fetchall()
    items = [dict(row) for row in rows]
    if room:
        target = clean_value(room)
        items = [
            item for item in items
            if clean_value(parse_json_dict(item.get("data")).get("所在机房")) == target
        ]
    return items


def trash_entry(trash_id, room=""):
    """取单条回收站记录（含快照字段）；room 非空时校验归属机房，不匹配返回 None。"""
    with db() as conn:
        row = conn.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
    if not row:
        return None
    item = trash_to_json(row)
    if room and clean_value(item.get("所在机房")) != clean_value(room):
        return None
    return item


def trash_to_json(row):
    item = parse_json_dict(row["data"])
    item.update({
        "id": row["id"],
        "serial_number": row["serial_number"],
        "request_id": row["request_id"],
        "deleted_at": row["deleted_at"],
        "deleted_by": row["deleted_by"],
        "reason": row["reason"],
        "origin": dict(row).get("origin", ""),
    })
    return item


def restore_trash(trash_id, overrides=None, actor="", room_scope=""):
    """重新上架：把快照写回服务器表。

    overrides 允许改**整行任意服务器字段**（品牌/型号/IP/密码/备注…），不只是客户与机位；
    序列号也可以改，改完该序列号的资料附件一起迁到新序列号，避免恢复后档案对不上。
    校验不过就整单拒绝——冲突时由管理人员先补登客户 / 腾出机位，再回来恢复，
    不做静默降级。

    room_scope 非空表示调用方只能操作该机房（机房人员）：恢复后的「所在机房」必须
    还是这个机房，否则等于借恢复把机器挪进别的机房。

    手动登记的占位记录（origin='manual'）本身没有信息，表单会把这些字段留空，
    提交时一次补齐即可；错误信息带「手动登记记录信息不完整：」前缀便于区分。
    """
    overrides = overrides or {}
    with db() as conn:
        row = conn.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
    if not row:
        raise ValueError("回收站记录不存在")
    serial = clean_value(row["serial_number"])
    records = read_records("servers")
    headers = ENTITIES["servers"]["headers"]
    picked = {key: value for key, value in overrides.items() if key in headers}
    # 序列号也是可改字段：留空视为不改
    new_serial = clean_value(picked.pop("序列号", "")) or serial
    if new_serial == serial:
        if any(item["序列号"] == serial for item in records):
            raise ValueError(f"序列号 {serial} 已在服务器表中，不能重复恢复")
    else:
        if any(item["序列号"] == new_serial for item in records):
            raise ValueError(f"序列号 {new_serial} 已在服务器表中，不能改用它")
        with db() as conn:
            duplicate = conn.execute(
                "SELECT id FROM trash WHERE serial_number=? AND id<>?", (new_serial, trash_id)
            ).fetchone()
        if duplicate:
            raise ValueError(f"序列号 {new_serial} 已在回收站中，不能改用它")
    snapshot = parse_json_dict(row["data"])
    manual = clean_value(dict(row).get("origin")) == TRASH_ORIGIN_MANUAL
    merged = normalize_row("servers", {**snapshot, **picked})
    merged["序列号"] = new_serial
    normalized, errors = validate_record("servers", merged)
    if errors:
        prefix = "手动登记记录信息不完整：" if manual else ""
        raise ValueError(prefix + "；".join(errors))
    if room_scope and normalized["所在机房"] != clean_value(room_scope):
        raise ValueError(f"只能恢复本机房的服务器（{clean_value(room_scope)}）")
    records.append(normalized)
    write_records("servers", records)
    with db() as conn:
        conn.execute("DELETE FROM trash WHERE id=?", (trash_id,))
        if new_serial != serial and not any(item["序列号"] == serial for item in records):
            # 附件按序列号归属：改名后随记录一起迁，旧序列号已不在服务器表里。
            # 若旧序列号仍被别的服务器记录占用（历史脏数据），不动附件，避免抢走别人的档案。
            conn.execute(
                "UPDATE attachments SET serial_number=? WHERE serial_number=?",
                (new_serial, serial),
            )
        notify_audit_event(conn, {
            "event_type": "restored",
            "audit_type": "回收站",
            "title": "服务器已从回收站重新上架",
            "entity_label": "服务器",
            "operation": "重新上架",
            "record_key": serial,
            "reviewer": actor,
            "time": now_text(),
            "summary": summarize_record(normalized, ["序列号", "客户名称", "所在机房", "所属机柜", "所属机位"]),
        })
    return normalized


def purge_trash(trash_id, actor=""):
    """永久删除：回收站记录 + 该序列号的全部资料附件（含磁盘文件）。"""
    with db() as conn:
        row = conn.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
    if not row:
        raise ValueError("回收站记录不存在")
    serial = clean_value(row["serial_number"])
    with db() as conn:
        attachments = conn.execute("SELECT * FROM attachments WHERE serial_number=?", (serial,)).fetchall()
    removed = 0
    for attachment in attachments:
        if storage.delete_attachment_file(attachment["stored_name"]):
            removed += 1
    with db() as conn:
        conn.execute("DELETE FROM attachments WHERE serial_number=?", (serial,))
        conn.execute("DELETE FROM trash WHERE id=?", (trash_id,))
        notify_audit_event(conn, {
            "event_type": "purged",
            "audit_type": "回收站",
            "title": "服务器已从回收站永久删除",
            "entity_label": "服务器",
            "operation": "永久删除",
            "record_key": serial,
            "reviewer": actor,
            "time": now_text(),
            "summary": f"连带删除资料 {len(attachments)} 个",
        })
    return {"serial_number": serial, "attachments": len(attachments), "deleted": removed}


# ---------------------------------------------------------------- 服务器资料（附件）
# 资料按「序列号」归属：上下架请求、机位IP分配、具体实施、服务器表看到的是同一份档案，
# 因此实施完成落库成服务器记录后，服务器表里天然能看到同一批资料，无需二次搬迁。
# 资料只存在于 attachments 表与 data/archive 目录，不进入任何 Excel 导出。


def max_attachment_bytes():
    config = storage.STATE.get("config") or {}
    return int(config.get("max_upload_mb") or 64) * 1024 * 1024


def visible_serials(user):
    """当前用户可见的服务器序列号集合；返回 None 表示不限制（管理人员）。"""
    if not user:
        return set()
    if user["身份"] == ROLE_ADMIN:
        return None
    keys = set()
    with db() as conn:
        rows = conn.execute("SELECT serial_number,request_data,stage_data,submitter_id FROM rack_requests").fetchall()
    if user["身份"] == ROLE_ROOM:
        room = user["所属机房"]
        for server in read_records("servers"):
            if server.get("所在机房") == room:
                keys.add(clean_value(server.get("序列号")))
        for row in rows:
            data = parse_json_dict(row["request_data"])
            stage_data = parse_json_dict(row["stage_data"])
            if (stage_data.get("所在机房") or data.get("所在机房")) == room:
                keys.add(clean_value(row["serial_number"]))
    elif user["身份"] == ROLE_BUSINESS:
        unit = user["业务单位"]
        for server in read_records("servers"):
            if server.get("归属单位") == unit:
                keys.add(clean_value(server.get("序列号")))
        for row in rows:
            if row["submitter_id"] == user["用户id"]:
                keys.add(clean_value(row["serial_number"]))
    keys.discard("")
    return keys


def can_access_serial(serial_number, user):
    serial = clean_value(serial_number)
    if not serial or not user:
        return False
    visible = visible_serials(user)
    return True if visible is None else serial in visible


def can_upload_attachment(serial_number, user):
    if not user:
        return False
    if not {"manage_all", "submit_rack_request", "review_rack_requests"} & set(capabilities_for(user)):
        return False
    return can_access_serial(serial_number, user)


def attachment_can_manage(row, user, visible=None):
    """谁能改/删某份资料：管理人员、上传者本人、以及该服务器所属机房的机房人员。"""
    if not user:
        return False
    if user["身份"] == ROLE_ADMIN:
        return True
    if clean_value(row["uploader"]) == user["用户id"]:
        return True
    if user["身份"] == ROLE_ROOM:
        if visible is None:
            visible = visible_serials(user)
        return True if visible is None else clean_value(row["serial_number"]) in visible
    return False


def attachment_to_json(row, user=None, visible=None):
    return {
        "id": row["id"],
        "serial_number": clean_value(row["serial_number"]),
        "filename": clean_value(row["filename"]),
        "size": int(row["size"] or 0),
        "uploader": clean_value(row["uploader"]),
        "uploaded_at": clean_value(row["uploaded_at"]),
        "request_id": clean_value(row["request_id"]),
        "source": clean_value(row["source"]),
        "can_manage": attachment_can_manage(row, user, visible),
    }


def list_attachments(serials=None, user=None):
    """列出资料；serials 为 None 表示全部（仍按用户可见范围过滤）。"""
    sql = "SELECT rowid,* FROM attachments"
    params = []
    if serials is not None:
        wanted = [clean_value(item) for item in serials if clean_value(item)]
        if not wanted:
            return []
        sql += f" WHERE serial_number IN ({','.join('?' * len(wanted))})"
        params = wanted
    sql += " ORDER BY uploaded_at DESC, rowid DESC"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    # 可见范围只算一次（会读 Excel + 查 rack_requests），避免每条资料都重算
    visible = visible_serials(user)
    return [
        attachment_to_json(row, user, visible)
        for row in rows
        if visible is None or clean_value(row["serial_number"]) in visible
    ]


def attachment_counts(user=None):
    counts = {}
    for item in list_attachments(None, user):
        serial = item["serial_number"]
        counts[serial] = counts.get(serial, 0) + 1
    return counts


def get_attachment(attachment_id):
    with db() as conn:
        return conn.execute("SELECT rowid,* FROM attachments WHERE id=?", (attachment_id,)).fetchone()


def create_attachments(serial_number, files, user, request_id="", source=""):
    serial = clean_value(serial_number)
    if not serial:
        raise ValueError("序列号不能为空")
    uploader = clean_value(user["用户id"]) if user else ""
    uploaded_at = now_text()
    limit = max_attachment_bytes()
    saved = []
    try:
        for uploaded in files:
            filename = clean_value(getattr(uploaded, "filename", ""))
            if not filename:
                continue
            attachment_id = uuid.uuid4().hex[:12]
            stored_name, size = storage.save_attachment_file(attachment_id, serial, filename, uploaded)
            if size > limit:
                storage.delete_attachment_file(stored_name)
                raise ValueError(f"文件 {filename} 超过 {limit // 1024 // 1024}MB 上传上限")
            saved.append({
                "id": attachment_id,
                "serial_number": serial,
                "filename": storage.safe_filename(filename),
                "stored_name": stored_name,
                "size": size,
                "uploader": uploader,
                "uploaded_at": uploaded_at,
                "request_id": clean_value(request_id),
                "source": clean_value(source),
            })
        if not saved:
            raise ValueError("请选择要上传的文件")
        with LOCK, db() as conn:
            for item in saved:
                conn.execute(
                    """INSERT INTO attachments
                       (id,serial_number,filename,stored_name,size,uploader,uploaded_at,request_id,source)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        item["id"], item["serial_number"], item["filename"], item["stored_name"],
                        item["size"], item["uploader"], item["uploaded_at"], item["request_id"], item["source"],
                    ),
                )
    except Exception:
        for item in saved:
            storage.delete_attachment_file(item["stored_name"])
        raise
    visible = visible_serials(user)
    ids = [item["id"] for item in saved]
    with db() as conn:
        rows = conn.execute(
            f"SELECT rowid,* FROM attachments WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
    by_id = {row["id"]: row for row in rows}
    return [attachment_to_json(by_id[item["id"]], user, visible) for item in saved if item["id"] in by_id]


def rename_attachment(attachment_id, filename, user):
    row = get_attachment(attachment_id)
    if not row:
        raise LookupError("资料不存在或已被删除")
    if not attachment_can_manage(row, user):
        raise PermissionError("无权修改该资料")
    new_name = storage.safe_filename(filename)
    if not new_name:
        raise ValueError("资料名称不能为空")
    stored_name = storage.rename_attachment_file(row["stored_name"], row["id"], new_name)
    with LOCK, db() as conn:
        conn.execute("UPDATE attachments SET filename=?,stored_name=? WHERE id=?", (new_name, stored_name, attachment_id))
    return attachment_to_json(get_attachment(attachment_id), user)


def delete_attachment(attachment_id, user):
    row = get_attachment(attachment_id)
    if not row:
        raise LookupError("资料不存在或已被删除")
    if not attachment_can_manage(row, user):
        raise PermissionError("无权删除该资料")
    with LOCK, db() as conn:
        conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    storage.delete_attachment_file(row["stored_name"])
    return attachment_to_json(row, user)
