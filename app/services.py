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
    RACK_REQUEST_HEADERS,
    RACK_REQUEST_EXPORT_HEADERS,
    RACK_REQUEST_META,
    clean_value,
    entity_meta,
    filter_records_for_user,
    initial_rack_request_stage,
    normalize_entity,
    normalize_rack_request_header,
    normalize_row,
    now_text,
    parse_filters,
    parse_json_dict,
    parse_sorts,
    positions,
    record_key,
    sort_records,
    summarize_record,
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


def pending_server_slot_states():
    result = {}
    with db() as conn:
        rows = conn.execute(
            "SELECT operation,record_key,new_data FROM changes WHERE status='pending' AND entity='servers'"
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
    pending_states = pending_server_slot_states()
    implementation_states = implementation_slot_states()
    rooms = {}
    for cabinet in read_records("cabinets"):
        room_name = cabinet["机房"]
        cabinet_name = cabinet["机柜"]
        room = rooms.setdefault(room_name, {"name": room_name, "cabinets": []})
        taken = occupied.get((room_name, cabinet_name), {})
        pending = {
            **implementation_states.get((room_name, cabinet_name), {}),
            **pending_states.get((room_name, cabinet_name), {}),
        }
        room["cabinets"].append({
            "name": cabinet_name,
            "range": cabinet["机位"],
            "power": cabinet.get("瓦数(kw)", cabinet.get("瓦数(w)", "")),
            "annual_cost": cabinet["费用(年)"],
            "ip_ranges": {field: cabinet.get(range_field, "") for field, range_field in IP_RANGE_FIELDS.items()},
            "slots": [
                {"slot": str(slot), **cabinet_slot_state(slot, taken, pending)}
                for slot in positions(cabinet["机位"])
            ],
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


def validate_record(entity, row, existing=None, exclude_record_key=None):
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
        for slot in server_slots:
            if slot in taken:
                errors.append(f"所属机位 {slot} 已被服务器 {taken[slot]} 占用")
                break
        errors.extend(same_kind_ip_conflict_errors(normalized))
    if existing is not None:
        key = record_key(meta, normalized)
        if any(record_key(meta, x) == key for x in existing):
            errors.append(f"主键 {key} 已存在")
    return normalized, errors


def duplicate_key_exists(meta, records, key, exclude_key=""):
    return any(record_key(meta, item) == key and record_key(meta, item) != exclude_key for item in records)


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
    if change["operation"] == "新增":
        normalized, errors = validate_record(entity, candidate, records)
        key = record_key(meta, normalized)
    else:
        current = next((item for item in records if record_key(meta, item) == change["record_key"]), None)
        if not current:
            raise ValueError("原记录不存在或已变化，请刷新后重试")
        exclude_key = change["record_key"] if entity == "servers" else None
        normalized, errors = validate_record(entity, candidate, None, exclude_key)
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


def submit_change(entity, operation, record_key_value, new_data=None, submitter=""):
    entity = normalize_entity(entity)
    change_id = uuid.uuid4().hex[:12]
    submitted_at = now_text()
    with LOCK, db() as conn:
        conn.execute("""INSERT INTO changes
            (id,entity,operation,record_key,new_data,status,submitter,submitted_at)
            VALUES (?,?,?,?,?,?,?,?)""", (
            change_id, entity, operation, record_key_value,
            json.dumps(new_data, ensure_ascii=False) if new_data else None,
            "pending", submitter, submitted_at,
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


def validate_final_new_data(entity, change, candidate, records):
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    exclude_key = change["record_key"] if entity == "servers" and change["operation"] == "修改" else None
    normalized, errors = validate_record(entity, candidate, None, exclude_key)
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


def approve_change(change, final_new_data=None, conn=None):
    entity = normalize_entity(change["entity"])
    meta = ENTITIES[entity]
    records = read_records(entity)
    new = final_new_data if final_new_data is not None else json.loads(change["new_data"]) if change["new_data"] else None
    if change["operation"] == "新增":
        if not new:
            raise ValueError("新增申请缺少审批后数据")
        key = validate_final_new_data(entity, change, new, records)
        records.append(new)
    else:
        key = change["record_key"]
        idx = next((i for i, x in enumerate(records) if record_key(meta, x) == key), None)
        if idx is None:
            raise ValueError(f"当前记录 {key} 已不存在，无法审批")
        if change["operation"] == "修改":
            if not new:
                raise ValueError("修改申请缺少审批后数据")
            new_key = validate_final_new_data(entity, change, new, records)
            records[idx] = new
        else:
            records = [record for record in records if record_key(meta, record) != key]
    write_records(entity, records)
    if conn and entity == "customers" and change["operation"] == "修改":
        cascade_customer_change(conn, change["record_key"], new_key, new.get("名称", ""), change["id"])
    return new


def create_version(conn, change, reviewer):
    return storage.create_version_record(conn, change["id"], change["entity"], change["operation"], change["record_key"], reviewer)


def apply_review_action(conn, change, body, reviewer, version_code=None):
    action = body.get("action")
    if action == "approve":
        final_new_data = body.get("new_data") if "new_data" in body else None
        approved_data = approve_change(change, final_new_data, conn)
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
        notify_audit_event(conn, operation_change_event(event_change, "approved", "操作审核已通过", reviewer))
    else:
        event_change = dict(change)
        event_change["reviewed_at"] = reviewed_at
        event_change["reject_reason"] = body.get("reason", "")
        notify_audit_event(conn, operation_change_event(event_change, "rejected", "操作审核已驳回", reviewer))
    return message


def normalize_rack_request(row):
    canonical = {}
    for key, value in (row or {}).items():
        target = normalize_rack_request_header(key)
        if target in RACK_REQUEST_HEADERS:
            canonical[target] = value
    data = {field: clean_value(canonical.get(field, "")) for field in RACK_REQUEST_HEADERS}
    if not data["是否上架"]:
        data["是否上架"] = "是"
    if data["是否上架"] == "否" and data["序列号"]:
        server = next((row for row in read_records("servers") if row["序列号"] == data["序列号"]), None)
        if server:
            if not data["客户id"]:
                data["客户id"] = server.get("客户id", "")
            if not data["客户名称"]:
                data["客户名称"] = server.get("客户名称", "")
            if not data["所在机房"]:
                data["所在机房"] = server.get("所在机房", "")
    if data["客户id"]:
        data = apply_customer_name(data)
    return data


def validate_rack_request(data):
    errors = []
    if not data["序列号"]:
        errors.append("序列号不能为空")
    if data["是否上架"] not in {"是", "否"}:
        errors.append("是否上架必须是 是 或 否")
    if not data["是否重装系统"]:
        errors.append("是否重装系统不能为空")
    elif data["是否重装系统"] not in {"是", "否"}:
        errors.append("是否重装系统必须是 是 或 否")
    elif data["是否重装系统"] == "否" and not data["初始密码"]:
        errors.append("是否重装系统为否时初始密码不能为空")
    servers = read_records("servers")
    server_by_serial = {row["序列号"]: row for row in servers}
    existing_server = server_by_serial.get(data["序列号"])
    if data["是否上架"] == "否" and existing_server:
        if data["客户id"] != existing_server.get("客户id", ""):
            errors.append(f"客户id必须与服务器 {data['序列号']} 的客户id {existing_server.get('客户id', '')} 一致")
        if data["所在机房"] != existing_server.get("所在机房", ""):
            errors.append(f"所在机房必须与服务器 {data['序列号']} 的所在机房 {existing_server.get('所在机房', '')} 一致")
    else:
        customer_ids = {row["客户id"] for row in read_records("customers")}
        if not data["客户id"]:
            errors.append("客户id不能为空")
        elif data["客户id"] not in customer_ids:
            errors.append(f"客户id {data['客户id']} 不存在")
        room_names = {row["机房"] for row in read_records("cabinets") if row["机房"]}
        if not data["所在机房"]:
            errors.append("所在机房不能为空")
        elif data["所在机房"] not in room_names:
            errors.append(f"所在机房 {data['所在机房']} 不存在")
    if data["是否上架"] == "是" and existing_server:
        errors.append(f"序列号 {data['序列号']} 已存在")
    if data["是否上架"] == "否" and not existing_server:
        errors.append(f"序列号 {data['序列号']} 不存在")
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
    request_type = "上架" if data["是否上架"] == "是" else "下架"
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
    request_type = "上架" if data["是否上架"] == "是" else "下架"
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


def rack_server_draft(row, override=None):
    data = parse_json_dict(row["request_data"])
    stage_data = parse_json_dict(row["stage_data"] if "stage_data" in row.keys() else "{}")
    override = override or {}
    return normalize_row("servers", {
        **data,
        **stage_data,
        **override,
        "客户id": override.get("客户id") or stage_data.get("客户id") or data.get("客户id", ""),
        "客户名称": override.get("客户名称") or stage_data.get("客户名称") or data.get("客户名称", ""),
        "所在机房": override.get("所在机房") or stage_data.get("所在机房") or data.get("所在机房", ""),
    })


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


def validate_rack_allocation(server_data, request_id=""):
    required = ["所在机房", "所属机柜", "所属机位", "管理ip", "带外ip"]
    errors = [f"{field}不能为空" for field in required if not clean_value(server_data.get(field))]
    _, record_errors = validate_record("servers", server_data, read_records("servers"))
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


def complete_rack_request(conn, row, user, server_data=None):
    records = read_records("servers")
    operation = "新增" if row["request_type"] == "上架" else "删除"
    key = row["serial_number"]
    event_server_data = server_data or {}
    if operation == "新增":
        normalized, errors = validate_record("servers", server_data or {}, records)
        key = record_key(ENTITIES["servers"], normalized)
        if errors:
            return None, errors
        event_server_data = normalized
        records.append(normalized)
    else:
        current = next((item for item in records if item["序列号"] == key), None)
        if not current:
            return None, ["服务器记录不存在"]
        event_server_data = current
        records = [record for record in records if record["序列号"] != key]
    write_records("servers", records)
    final_serial = normalized["序列号"] if operation == "新增" else key
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
    notify_rack_stage(conn, event_row, "approved", "服务器上下架具体实施已完成", user["用户id"], event_server_data)
    return f"服务器上下架具体实施已完成，已生成版本 {version_code}", []
