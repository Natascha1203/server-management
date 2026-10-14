"""HTTP 路由层：全部接口，只做参数解析、权限判断和 service 调用。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Blueprint, g, jsonify, redirect, render_template, request, send_file, session, url_for

from . import services, storage
from .config import legacy_role_name, role_code
from .core import (
    ENTITIES,
    ROLE_ADMIN,
    ROLE_BUSINESS,
    ROLE_ROOM,
    can_mutate_entity,
    can_view_entity,
    capabilities_for,
    clean_value,
    filter_records_for_user,
    normalize_entity,
    positions,
)

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "请先登录"}), 401
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user["身份"] != ROLE_ADMIN:
            return jsonify({"error": "仅管理员可访问"}), 403
        return fn(*args, **kwargs)
    return wrapper


def _token_user():
    """从 Authorization: Bearer <token> 解析并校验 token 用户，未命中返回 None。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    return storage.get_user_by_token(token) if token else None


def current_user():
    """当前请求身份，按优先级：session 用户 > Bearer token 用户。缓存到 g，避免重复校验。"""
    cached = getattr(g, "_current_user", None)
    if cached is not None:
        return cached

    user = None
    if session.get("username"):
        user_id = session.get("user_id") or session.get("username")
        configured = storage.get_users().get(user_id, {})
        user = {
            "用户id": user_id,
            "身份": session.get("role_name") or configured.get("身份") or legacy_role_name(session.get("role")),
            "业务单位": session.get("business_unit", configured.get("业务单位", "")),
            "所属机房": session.get("room", configured.get("所属机房", "")),
        }
    else:
        token_user = _token_user()
        if token_user:
            user = {
                "用户id": token_user["用户id"],
                "身份": token_user["身份"],
                "业务单位": token_user.get("业务单位", ""),
                "所属机房": token_user.get("所属机房", ""),
            }

    g._current_user = user
    return user


def forbidden(message="无权访问"):
    return jsonify(error=message), 403


def ensure_entity_view_response(entity):
    if normalize_entity(entity) not in ENTITIES:
        return jsonify(error="未知数据类型"), 404
    if not can_view_entity(entity, current_user()):
        return forbidden()
    return None


def ensure_entity_mutate_response(entity):
    if normalize_entity(entity) not in ENTITIES:
        return jsonify(error="未知数据类型"), 404
    if not can_mutate_entity(entity, current_user()):
        return forbidden()
    return None


def request_record_state(default="pending"):
    value = clean_value(request.args.get("record_state", default))
    if value not in {"official", "pending"}:
        raise ValueError("记录状态仅支持 official 或 pending")
    return value


def filling_instructions_path():
    return Path(storage.STATE["config"]["base_dir"]) / "docs" / "信息维护填写说明.md"


@bp.route("/")
def index():
    if not session.get("username"):
        return redirect(url_for(".login_page"))
    user = current_user()
    return render_template(
        "index.html",
        role=session["role"],
        role_name=user["身份"],
        username=session["username"],
        app_user=user,
        capabilities=capabilities_for(user),
        entities=ENTITIES,
    )


@bp.get("/api/filling-instructions")
@require_login
def download_filling_instructions():
    return send_file(
        filling_instructions_path(),
        as_attachment=True,
        download_name="信息维护填写说明.md",
        mimetype="text/markdown; charset=utf-8",
    )


@bp.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return render_template("login.html")
    users = storage.get_users()
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if username not in users or users[username]["密码"] != password:
        return render_template("login.html", error="用户名或密码错误"), 401
    session.clear()
    user = users[username]
    session.update(
        username=username,
        user_id=username,
        role=role_code(user["身份"]),
        role_name=user["身份"],
        business_unit=user.get("业务单位", ""),
        room=user.get("所属机房", ""),
    )
    return redirect(url_for(".index"))


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for(".login_page"))


@bp.get("/api/me")
@require_login
def me():
    user = current_user()
    return jsonify({
        "username": user["用户id"],
        "role": role_code(user["身份"]),
        "用户id": user["用户id"],
        "身份": user["身份"],
        "业务单位": user["业务单位"],
        "所属机房": user["所属机房"],
        "capabilities": capabilities_for(user),
    })


@bp.get("/api/my-token")
@require_login
def my_token():
    """返回当前登录用户自己的 API token；未配置时返回空串（前端据此提示联系管理员）。"""
    user = current_user()
    configured = storage.get_users().get(user["用户id"], {}) or {}
    return jsonify({"api_token": clean_value(configured.get("api_token", ""))})


@bp.get("/api/cabinet-options")
@require_login
def cabinet_options_api():
    data = services.cabinet_options(request.args.get("exclude_server"))
    user = current_user()
    if user and user["身份"] == ROLE_ROOM:
        data["rooms"] = [room for room in data["rooms"] if room["name"] == user["所属机房"]]
    return jsonify(data)


@bp.post("/api/rack-requests/auto-ip")
@require_login
def auto_ip_for_rack_request():
    user = current_user()
    if "review_rack_requests" not in capabilities_for(user):
        return forbidden()
    body = request.get_json(silent=True) or {}
    room = clean_value(body.get("所在机房"))
    cabinet_name = clean_value(body.get("所属机柜"))
    request_id = clean_value(body.get("request_id"))
    if not room or not cabinet_name:
        return jsonify(error="所在机房和所属机柜不能为空"), 400
    if user["身份"] == ROLE_ROOM and room != user["所属机房"]:
        return forbidden()
    try:
        ips = services.auto_assign_ips(room, cabinet_name, request_id)
    except LookupError as exc:
        return jsonify(error=str(exc)), 409
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ips=ips)


@bp.get("/api/<entity>")
@require_login
def list_entity(entity):
    denied = ensure_entity_view_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    try:
        record_state = request_record_state()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(records=services.records_for_state(entity, record_state, current_user()), meta=ENTITIES[entity], record_state=record_state)


@bp.get("/api/customer-options")
@require_login
def customer_options():
    records = storage.read_records("customers")
    return jsonify(customers=[
        {"客户id": row.get("客户id", ""), "客户名称": row.get("名称", ""), "负责人": row.get("负责人", "")}
        for row in records
        if row.get("客户id")
    ])


@bp.post("/api/<entity>/search")
@require_login
def search_entity(entity):
    denied = ensure_entity_view_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    try:
        record_state = request_record_state()
        body = request.get_json(silent=True) or {}
        records = services.query_records(entity, body, record_state, current_user())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(records=records, meta=ENTITIES[entity], count=len(records), record_state=record_state)


@bp.post("/api/<entity>/export")
@require_login
def export_entity(entity):
    denied = ensure_entity_view_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    export_format = clean_value(request.args.get("format", "xlsx")).lower()
    if export_format not in ("xlsx", "csv"):
        return jsonify(error="导出格式仅支持 xlsx 或 csv"), 400
    try:
        record_state = request_record_state()
        body = request.get_json(silent=True) or {}
        records = services.query_records(entity, body, record_state, current_user())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    meta = ENTITIES[entity]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if export_format == "csv":
        return send_file(
            storage.records_to_csv(meta, records, include_pending_status=True),
            as_attachment=True,
            download_name=f"{meta['label']}-检索结果-{timestamp}.csv",
            mimetype="text/csv; charset=utf-8",
        )
    return send_file(
        storage.records_to_xlsx(meta, records, include_pending_status=True),
        as_attachment=True,
        download_name=f"{meta['label']}-检索结果-{timestamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/api/<entity>/template")
@require_login
def download_template(entity):
    denied = ensure_entity_view_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    template_format = clean_value(request.args.get("format", "xlsx")).lower()
    if template_format not in ("xlsx", "csv"):
        return jsonify(error="模板格式仅支持 xlsx 或 csv"), 400
    if template_format == "csv":
        return send_file(
            storage.records_to_csv(meta, []),
            as_attachment=True,
            download_name=f"{meta['label']}模板.csv",
            mimetype="text/csv; charset=utf-8",
        )
    return send_file(
        storage.records_to_xlsx(meta, []),
        as_attachment=True,
        download_name=meta["filename"],
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.post("/api/<entity>/add")
@require_login
def add_entity(entity):
    denied = ensure_entity_mutate_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    current = storage.read_records(entity)
    row, errors = services.validate_record(entity, request.get_json(silent=True) or {}, current)
    if errors:
        return jsonify(error="；".join(errors)), 400
    from .core import record_key

    change_id = services.submit_change(entity, "新增", record_key(ENTITIES[entity], row), row, submitter=current_user()["用户id"])
    return jsonify(message="已提交新增申请", change_id=change_id)


@bp.post("/api/<entity>/import")
@require_login
def import_entity(entity):
    denied = ensure_entity_mutate_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    meta = ENTITIES[entity]
    uploaded = request.files.get("file")
    # 覆盖模式：full=全量覆盖（默认，未提供列置空）；merge=增量覆盖（未提供列保留旧值）
    mode = clean_value(request.form.get("mode") or request.args.get("mode") or "full")
    if mode not in ("full", "merge"):
        return jsonify(error="覆盖模式仅支持 full 或 merge"), 400
    try:
        source_label, values = storage.uploaded_table_rows(uploaded, meta) if uploaded else (None, None)
    except UnicodeDecodeError:
        return jsonify(error="CSV 文件必须使用 UTF-8 编码"), 400
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if values is None:
        return jsonify(error="请选择 .xlsx 或 .csv 文件"), 400
    if not values:
        return jsonify(error=f"{source_label} 没有数据"), 400
    from .core import normalize_row, record_key

    headers = [clean_value(x) for x in values[0]]
    missing = [h for h in meta["headers"] if h not in headers]
    if missing:
        return jsonify(error="缺少列：" + "、".join(missing)), 400
    existing = storage.read_records(entity)
    existing_keys = {record_key(meta, x) for x in existing}
    existing_by_key = {record_key(meta, x): x for x in existing}
    accepted, row_errors = [], []
    file_seen = set()
    for line_no, values_row in enumerate(values[1:], 2):
        raw = {h: v for h, v in zip(headers, values_row)}
        if not any(clean_value(v) for v in raw.values()):
            continue
        provisional = normalize_row(entity, raw)
        provisional_key = record_key(meta, provisional)
        is_update = provisional_key in existing_keys
        row, errors = services.validate_record(entity, raw, None, provisional_key if is_update and entity == "servers" else None)
        if mode == "merge" and is_update:
            old = existing_by_key.get(provisional_key, {})
            # 仅当上传文件该字段提供了非空值时才覆盖，空值列保留旧值；
            # 「客户名称」由 validate_record 按客户id派生，始终保留派生结果，不覆盖为旧值
            for field in meta["headers"]:
                if not clean_value(raw.get(field)) and field != "客户名称":
                    row[field] = old.get(field, "")
        key = record_key(meta, row)
        if key in file_seen:
            errors.append(f"主键 {key} 文件内重复")
        elif key in existing_keys:
            pending = services.pending_mutation_for_record(entity, key)
            if pending:
                errors.append(f"记录 {key} 已有待审批{pending['operation']}申请，请切换到待审批状态处理")
        if errors:
            row_errors.append({"row": line_no, "errors": errors})
        else:
            accepted.append((key, "修改" if key in existing_keys else "新增", row))
            file_seen.add(key)
    if row_errors:
        return jsonify(error="导入校验失败", details=row_errors), 400
    submitter = current_user()["用户id"]
    ids = [services.submit_change(entity, operation, key, row, submitter=submitter) for key, operation, row in accepted]
    add_count = sum(1 for _, operation, _ in accepted if operation == "新增")
    update_count = sum(1 for _, operation, _ in accepted if operation == "修改")
    parts = []
    if add_count:
        parts.append(f"{add_count} 条新增申请")
    if update_count:
        parts.append(f"{update_count} 条修改申请")
    return jsonify(message="已提交 " + "，".join(parts), change_ids=ids)


@bp.post("/api/<entity>/update")
@require_login
def update_entity(entity):
    denied = ensure_entity_mutate_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    from .core import normalize_row, record_key

    body = request.get_json(silent=True) or {}
    old = normalize_row(entity, body.get("old") or {})
    records = storage.read_records(entity)
    old_key = record_key(ENTITIES[entity], old)
    new, errors = services.validate_record(entity, body.get("new") or {}, None, old_key if entity == "servers" else None)
    current = next((x for x in records if record_key(ENTITIES[entity], x) == old_key), None)
    if not current:
        return jsonify(error="原记录不存在或已变化，请刷新后重试"), 409
    new_key = record_key(ENTITIES[entity], new)
    if new_key != old_key and entity != "customers":
        errors.append("不允许修改主键字段")
    if entity == "customers" and services.duplicate_key_exists(ENTITIES[entity], records, new_key, old_key):
        errors.append(f"主键 {new_key} 已存在")
    if errors:
        return jsonify(error="；".join(errors)), 400
    pending = services.pending_mutation_for_record(entity, old_key)
    if pending:
        return jsonify(error=f"记录 {old_key} 已有待审批{pending['operation']}申请，请切换到待审批状态处理"), 409
    change_id = services.submit_change(entity, "修改", old_key, new, submitter=current_user()["用户id"])
    return jsonify(message="已提交修改申请", change_id=change_id)


@bp.post("/api/<entity>/delete")
@require_login
def delete_entity(entity):
    denied = ensure_entity_mutate_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    from .core import normalize_row, record_key

    target = normalize_row(entity, request.get_json(silent=True) or {})
    key = record_key(ENTITIES[entity], target)
    current = next((x for x in storage.read_records(entity) if record_key(ENTITIES[entity], x) == key), None)
    if not current:
        return jsonify(error="记录不存在或已变化，请刷新后重试"), 409
    pending = services.pending_mutation_for_record(entity, key)
    if pending:
        return jsonify(error=f"记录 {key} 已有待审批{pending['operation']}申请，请切换到待审批状态处理"), 409
    change_id = services.submit_change(entity, "删除", key, submitter=current_user()["用户id"])
    return jsonify(message="已提交删除申请", change_id=change_id)


@bp.post("/api/<entity>/delete-batch")
@require_login
def delete_entity_batch(entity):
    denied = ensure_entity_mutate_response(entity)
    if denied:
        return denied
    entity = normalize_entity(entity)
    from .core import normalize_row, record_key

    body = request.get_json(silent=True) or {}
    targets = body.get("records") or []
    if not isinstance(targets, list) or not targets:
        return jsonify(error="请选择要删除的记录"), 400
    meta = ENTITIES[entity]
    records = storage.read_records(entity)
    by_key = {record_key(meta, item): item for item in records}
    seen = set()
    change_ids = []
    errors = []
    submitter = current_user()["用户id"]
    for index, target in enumerate(targets, 1):
        key = record_key(meta, normalize_row(entity, target or {}))
        if not key:
            errors.append({"index": index, "error": "记录主键不能为空"})
            continue
        if key in seen:
            errors.append({"index": index, "record_key": key, "error": "重复选择"})
            continue
        seen.add(key)
        if key not in by_key:
            errors.append({"index": index, "record_key": key, "error": "记录不存在或已变化，请刷新后重试"})
            continue
        pending = services.pending_mutation_for_record(entity, key)
        if pending:
            errors.append({"index": index, "record_key": key, "error": f"记录已有待审批{pending['operation']}申请，请切换到待审批状态处理"})
            continue
        change_ids.append(services.submit_change(entity, "删除", key, submitter=submitter))
    if errors:
        return jsonify(error="部分记录无法提交删除申请", details=errors, change_ids=change_ids, submitted=len(change_ids)), 409
    return jsonify(message=f"已提交 {len(change_ids)} 条删除申请", change_ids=change_ids, submitted=len(change_ids))


@bp.get("/api/changes")
@require_login
def changes():
    from .core import can_review_operation_change

    status = request.args.get("status", "pending")
    user = current_user()
    if "review_operations" not in capabilities_for(user):
        return jsonify(error="无权限访问操作审核"), 403
    with storage.db() as conn:
        rows = conn.execute("SELECT * FROM changes WHERE status=? ORDER BY submitted_at,id", (status,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["new_data"] = json.loads(item["new_data"]) if item["new_data"] else None
        item["current_data"] = services.find_record(item["entity"], item["record_key"])
        item["entity_label"] = ENTITIES[item["entity"]]["label"] if item["entity"] in ENTITIES else ""
        item["meta"] = ENTITIES[item["entity"]]
        visible = can_review_operation_change(item, user)
        if user["身份"] == ROLE_BUSINESS:
            visible = item["submitter"] == user["用户id"]
        if visible:
            result.append(item)
    return jsonify(changes=result)


@bp.post("/api/changes/<change_id>/update-pending")
@require_login
def update_pending_change(change_id):
    body = request.get_json(silent=True) or {}
    with storage.LOCK, storage.db() as conn:
        change = conn.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
        if not change or change["status"] != "pending":
            return jsonify(error="申请不存在或已审核"), 404
        if not services.can_manage_pending_change(change, current_user()):
            return jsonify(error="只能修改自己提交的待审批申请"), 403
        try:
            key, new_data = services.validate_pending_change_update(change, body.get("new_data") or {})
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        conn.execute(
            "UPDATE changes SET record_key=?,new_data=? WHERE id=?",
            (key, json.dumps(new_data, ensure_ascii=False), change_id),
        )
        storage.sync_pending_excel(conn)
    return jsonify(message="已更新待审批申请", change_id=change_id, record_key=key, new_data=new_data)


@bp.post("/api/changes/<change_id>/withdraw")
@require_login
def withdraw_pending_change(change_id):
    with storage.LOCK, storage.db() as conn:
        change = conn.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
        if not change or change["status"] != "pending":
            return jsonify(error="申请不存在或已审核"), 404
        if not services.can_manage_pending_change(change, current_user()):
            return jsonify(error="只能撤回自己提交的待审批申请"), 403
        conn.execute("DELETE FROM changes WHERE id=?", (change_id,))
        storage.sync_pending_excel(conn)
    return jsonify(message="已撤回待审批申请", change_id=change_id)


@bp.post("/api/changes/<change_id>/review")
@require_login
def review_change(change_id):
    from .core import can_review_operation_change

    body = request.get_json(silent=True) or {}
    action = body.get("action")
    logger.info(
        "review request change_id=%s action=%s reviewer=%s has_new_data=%s",
        change_id,
        action,
        session.get("username"),
        "new_data" in body,
    )
    if action not in ("approve", "reject"):
        logger.warning("review rejected invalid action change_id=%s action=%s", change_id, action)
        return jsonify(error="审核动作无效"), 400
    with storage.LOCK, storage.db() as conn:
        change = conn.execute("SELECT * FROM changes WHERE id=?", (change_id,)).fetchone()
        if not change or change["status"] != "pending":
            logger.warning("review change not pending change_id=%s found=%s", change_id, bool(change))
            return jsonify(error="申请不存在或已审核"), 404
        if not can_review_operation_change(change, current_user()):
            return forbidden()
        logger.info(
            "review loaded change_id=%s entity=%s operation=%s record_key=%s submitter=%s submitted_at=%s",
            change["id"],
            change["entity"],
            change["operation"],
            change["record_key"],
            change["submitter"],
            change["submitted_at"],
        )
        try:
            message = services.apply_review_action(conn, change, body, reviewer=current_user()["用户id"])
            storage.prune_audit_records(conn)
            storage.sync_pending_excel(conn)
            logger.info("review completed change_id=%s action=%s message=%s", change_id, action, message)
        except ValueError as exc:
            message = str(exc)
            code = 400 if "不允许修改主键" in message or "缺少审批后数据" in message else 409
            logger.warning("review validation failed change_id=%s status_code=%s error=%s", change_id, code, message)
            return jsonify(error=message), code
        except Exception:
            logger.exception("review unexpected error change_id=%s action=%s", change_id, action)
            raise
    return jsonify(message=message)


@bp.post("/api/changes/review-batch")
@require_login
def review_changes_batch():
    import uuid as uuid_module

    from .core import can_review_operation_change

    body = request.get_json(silent=True) or {}
    action = body.get("action")
    ids = body.get("ids") or []
    if action not in ("approve", "reject"):
        return jsonify(error="审核动作无效"), 400
    if not isinstance(ids, list) or not ids:
        return jsonify(error="请选择要审核的申请"), 400
    if action == "reject" and not clean_value(body.get("reason")):
        return jsonify(error="请输入驳回原因"), 400
    new_data_map = body.get("new_data") or {}
    if not isinstance(new_data_map, dict):
        return jsonify(error="审批后数据格式无效"), 400
    reviewed = []
    with storage.LOCK, storage.db() as conn:
        try:
            changes = []
            for change_id in ids:
                change = conn.execute("SELECT rowid,* FROM changes WHERE id=?", (change_id,)).fetchone()
                if not change or change["status"] != "pending":
                    raise ValueError(f"申请 {change_id} 不存在或已审核")
                if not can_review_operation_change(change, current_user()):
                    return forbidden()
                changes.append(change)
            changes.sort(key=lambda item: (item["submitted_at"], item["rowid"]))
            batch_version_code = None
            reviewer = current_user()["用户id"]
            if action == "approve":
                batch_version_code = storage.create_version_record(
                    conn,
                    f"batch:{uuid_module.uuid4().hex[:12]}",
                    "batch",
                    "批量通过",
                    f"{len(changes)} 条申请",
                    reviewer,
                )
            for change in changes:
                item_body = {"action": action, "reason": body.get("reason", "")}
                if action == "approve" and change["id"] in new_data_map:
                    item_body["new_data"] = new_data_map[change["id"]]
                message = services.apply_review_action(conn, change, item_body, reviewer=reviewer, version_code=batch_version_code)
                reviewed.append({"id": change["id"], "message": message})
            storage.prune_audit_records(conn)
            storage.sync_pending_excel(conn)
        except ValueError as exc:
            conn.rollback()
            storage.sync_pending_excel(conn)
            message = str(exc)
            code = 400 if "不允许修改主键" in message or "缺少审批后数据" in message or "格式无效" in message else 409
            return jsonify(error=message, reviewed=reviewed), code
    action_label = "通过" if action == "approve" else "驳回"
    return jsonify(message=f"已{action_label} {len(reviewed)} 条申请", reviewed=len(reviewed), results=reviewed)


@bp.get("/api/versions")
@require_admin
def versions():
    with storage.db() as conn:
        rows = conn.execute("SELECT * FROM versions ORDER BY id DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["entity_label"] = ENTITIES[item["entity"]]["label"] if item["entity"] in ENTITIES else "批量审核"
        item.pop("snapshot_dir", None)
        result.append(item)
    return jsonify(versions=result)


@bp.get("/api/versions/<int:version_id>/<entity>/download")
@require_admin
def download_version(version_id, entity):
    entity = normalize_entity(entity)
    if entity not in ENTITIES:
        return jsonify(error="未知数据类型"), 404
    with storage.db() as conn:
        version = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
    if not version:
        return jsonify(error="版本不存在"), 404
    filename = ENTITIES[entity]["filename"]
    path = Path(version["snapshot_dir"]) / filename
    if not path.exists():
        return jsonify(error="版本文件不存在"), 404
    return send_file(path, as_attachment=True, download_name=f"{version['version_code']}_{filename}")


@bp.route("/api/settings", methods=["GET", "PUT"])
@require_login
def settings_api():
    user = current_user()
    if "manage_settings" not in capabilities_for(user):
        return forbidden()
    if request.method == "GET":
        return jsonify(settings=storage.read_settings())
    body = request.get_json(silent=True) or {}
    try:
        notification_settings = storage.validate_notification_settings(body)
        settings = storage.write_settings({
            "backup_keep_count": int(body.get("backup_keep_count", 0)),
            "backup_command": body.get("backup_command", ""),
            "audit_keep_count": int(body.get("audit_keep_count", 0)),
            **notification_settings,
        })
    except (TypeError, ValueError) as exc:
        return jsonify(error=f"设置格式无效：{exc}"), 400
    return jsonify(message="设置已保存", settings=settings)


@bp.post("/api/backups/run")
@require_login
def run_backup_api():
    import subprocess

    user = current_user()
    if "manage_settings" not in capabilities_for(user):
        return forbidden()
    try:
        target = storage.create_backup("manual")
    except subprocess.CalledProcessError as exc:
        return jsonify(error=f"备份命令执行失败：{exc}"), 500
    return jsonify(message="备份已完成", backup=target.name)


@bp.post("/api/flow-state/reset")
@require_login
def reset_flow_state_api():
    user = current_user()
    if "manage_settings" not in capabilities_for(user):
        return forbidden()
    summary = storage.reset_flow_state()
    return jsonify(message="流程状态已重置", summary=summary)


@bp.route("/api/system-users", methods=["GET", "PUT"])
@require_login
def system_users_api():
    user = current_user()
    if "manage_settings" not in capabilities_for(user):
        return forbidden()
    if request.method == "GET":
        return jsonify(users=storage.system_users_list())
    body = request.get_json(silent=True) or {}
    try:
        users = storage.save_system_users(body.get("users"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(message="用户配置已保存", users=users)


@bp.route("/api/rack-requests", methods=["GET", "POST"])
@require_login
def rack_requests_api():
    from .core import (
        can_manage_rack_request,
        can_review_rack_request,
        rack_request_visible,
        submitted_rack_request_visible,
    )

    user = current_user()
    if request.method == "GET":
        status = request.args.get("status", "pending")
        scope = request.args.get("scope", "review")
        stage = clean_value(request.args.get("stage"))
        with storage.db() as conn:
            if stage:
                rows = conn.execute("SELECT * FROM rack_requests WHERE status=? AND stage=? ORDER BY submitted_at,rowid", (status, stage)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM rack_requests WHERE status=? ORDER BY submitted_at,rowid", (status,)).fetchall()
        if scope == "submitted":
            visible = submitted_rack_request_visible
        else:
            visible = rack_request_visible
        return jsonify(requests=[services.rack_request_to_json(row) for row in rows if visible(row, user)])
    if "submit_rack_request" not in capabilities_for(user):
        return forbidden()
    data = services.normalize_rack_request(request.get_json(silent=True) or {})
    errors = services.validate_rack_request(data)
    if errors:
        return jsonify(error="；".join(errors)), 400
    with storage.LOCK, storage.db() as conn:
        duplicate = services.pending_rack_request_duplicate(conn, data["序列号"])
        if duplicate:
            return jsonify(error=f"序列号 {data['序列号']} 已有待审核上下架请求"), 409
        request_id = services.insert_rack_request(conn, data, user)
    return jsonify(message="上下架请求已提交", request_id=request_id)


@bp.post("/api/rack-requests/export")
@require_login
def export_rack_requests():
    """上下架请求导出：支持按选中条目（ids）导出，缺省导出当前筛选下可见的全部。"""
    from .core import (
        RACK_REQUEST_EXPORT_HEADERS,
        rack_request_visible,
        submitted_rack_request_visible,
    )

    user = current_user()
    if "review_rack_requests" not in capabilities_for(user) and "submit_rack_request" not in capabilities_for(user):
        return forbidden()
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    ids = [clean_value(item) for item in ids if clean_value(item)] if isinstance(ids, list) else []
    status = clean_value(body.get("status") or request.args.get("status") or "pending")
    scope = clean_value(body.get("scope") or request.args.get("scope") or "review")
    stage = clean_value(body.get("stage") or request.args.get("stage"))

    with storage.db() as conn:
        if status:
            rows = conn.execute("SELECT * FROM rack_requests WHERE status=? ORDER BY submitted_at,rowid", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM rack_requests ORDER BY submitted_at,rowid").fetchall()
    visible = submitted_rack_request_visible if scope == "submitted" else rack_request_visible
    rows = [row for row in rows if visible(row, user)]
    if stage:
        rows = [row for row in rows if (row["stage"] if "stage" in row.keys() else "") == stage]
    if ids:
        wanted = set(ids)
        rows = [row for row in rows if row["id"] in wanted]
    if not rows:
        return jsonify(error="没有可导出的上下架请求"), 400

    meta = {"label": "上下架请求", "sheet": "上下架请求", "headers": RACK_REQUEST_EXPORT_HEADERS}
    records = services.rack_request_export_records(rows)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(
        storage.records_to_xlsx(meta, records),
        as_attachment=True,
        download_name=f"上下架请求-{timestamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.get("/api/rack-requests/template")
@require_login
def download_rack_request_template():
    from .core import RACK_REQUEST_META

    user = current_user()
    if "submit_rack_request" not in capabilities_for(user):
        return forbidden()
    template_format = clean_value(request.args.get("format", "xlsx")).lower()
    if template_format not in ("xlsx", "csv"):
        return jsonify(error="模板格式仅支持 xlsx 或 csv"), 400
    if template_format == "csv":
        return send_file(
            storage.records_to_csv(RACK_REQUEST_META, []),
            as_attachment=True,
            download_name="上下架请求导入模板.csv",
            mimetype="text/csv; charset=utf-8",
        )
    return send_file(
        storage.records_to_xlsx(RACK_REQUEST_META, []),
        as_attachment=True,
        download_name="上下架请求导入模板.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.post("/api/rack-requests/import")
@require_login
def import_rack_requests():
    user = current_user()
    if "submit_rack_request" not in capabilities_for(user):
        return forbidden()
    try:
        source_label, data_rows = services.rack_request_rows_from_upload(request.files.get("file"))
    except (UnicodeDecodeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400
    row_errors = []
    accepted = []
    seen_serials = set()
    with storage.LOCK, storage.db() as conn:
        for row_index, data in data_rows:
            errors = services.validate_rack_request(data)
            serial_number = data["序列号"]
            if serial_number:
                if serial_number in seen_serials:
                    errors.append(f"序列号 {serial_number} 在导入文件中重复")
            if errors:
                row_errors.append({"row": row_index, "error": "；".join(errors)})
            else:
                seen_serials.add(serial_number)
                accepted.append(data)
        if row_errors:
            return jsonify(error="导入校验失败", details=row_errors), 400
        from .core import now_text

        submitted_at = now_text()
        request_ids = []
        created = 0
        updated = 0
        for data in accepted:
            duplicate = services.importable_rack_request_duplicate(conn, data["序列号"])
            if duplicate:
                request_ids.append(services.overwrite_rack_request(conn, duplicate, data, user, submitted_at))
                updated += 1
            else:
                request_ids.append(services.insert_rack_request(conn, data, user, submitted_at))
                created += 1
    return jsonify(
        message=f"已从 {source_label} 导入 {len(request_ids)} 条上下架请求，新增 {created} 条，覆盖 {updated} 条",
        request_ids=request_ids,
        submitted=len(request_ids),
        created=created,
        updated=updated,
    )


@bp.post("/api/rack-requests/<request_id>/update")
@require_login
def update_rack_request(request_id):
    from .core import can_manage_rack_request, initial_rack_request_stage, now_text

    user = current_user()
    body = request.get_json(silent=True) or {}
    data = services.normalize_rack_request(body.get("request_data") or body)
    with storage.LOCK, storage.db() as conn:
        row = conn.execute("SELECT * FROM rack_requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] not in {"pending", "rejected"}:
            return jsonify(error="请求不存在或已完成"), 404
        if not can_manage_rack_request(row, user):
            return forbidden("只能修改自己提交的待审核上下架请求")
        errors = services.validate_rack_request(data)
        if errors:
            return jsonify(error="；".join(errors)), 400
        duplicate = services.pending_rack_request_duplicate(conn, data["序列号"], request_id)
        if duplicate:
            return jsonify(error=f"序列号 {data['序列号']} 已有待审核上下架请求"), 409
        request_type = "上架" if data["是否上架"] == "是" else "下架"
        stage = initial_rack_request_stage(request_type)
        was_rejected = row["status"] == "rejected"
        submitted_at = now_text() if was_rejected else row["submitted_at"]
        conn.execute(
            """UPDATE rack_requests
               SET serial_number=?,request_type=?,request_data=?,status='pending',stage=?,stage_data='{}',
                   submitted_at=?,reviewer_id='',reviewed_at='',reject_reason='',
                   allocation_reviewer_id='',allocation_at='',implementation_reviewer_id='',implementation_at=''
               WHERE id=?""",
            (data["序列号"], request_type, json.dumps(data, ensure_ascii=False), stage, submitted_at, request_id),
        )
        event_type = "resubmitted" if was_rejected else "updated"
        title = "服务器上下架请求已重新提交" if was_rejected else "服务器上下架请求已更新"
        services.notify_audit_event(conn, services.rack_request_event({
            "id": request_id,
            "serial_number": data["序列号"],
            "request_type": request_type,
            "request_data": json.dumps(data, ensure_ascii=False),
            "status": "pending",
            "stage": stage,
            "stage_data": "{}",
            "submitter_id": user["用户id"],
            "submitted_at": submitted_at,
            "reviewed_at": submitted_at,
            "reject_reason": "",
        }, event_type, title))
    message = "上下架请求已重新提交" if was_rejected else "上下架请求已更新"
    return jsonify(message=message, request_id=request_id, request=services.rack_request_to_json({
        "id": request_id,
        "serial_number": data["序列号"],
        "request_type": request_type,
        "request_data": json.dumps(data, ensure_ascii=False),
        "status": "pending",
        "stage": stage,
        "stage_data": "{}",
        "submitter_id": user["用户id"],
        "submitted_at": submitted_at,
        "reviewer_id": "",
        "reviewed_at": "",
        "reject_reason": "",
    }))


@bp.post("/api/rack-requests/<request_id>/withdraw")
@require_login
def withdraw_rack_request(request_id):
    from .core import can_manage_rack_request

    user = current_user()
    with storage.LOCK, storage.db() as conn:
        row = conn.execute("SELECT * FROM rack_requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] != "pending":
            return jsonify(error="请求不存在或已审核"), 404
        if not can_manage_rack_request(row, user):
            return forbidden("只能撤回自己提交的待审核上下架请求")
        conn.execute("DELETE FROM rack_requests WHERE id=?", (request_id,))
    return jsonify(message="上下架请求已撤回", request_id=request_id)


@bp.post("/api/rack-requests/<request_id>/review")
@require_login
def review_rack_request(request_id):
    from .core import can_review_rack_request, initial_rack_request_stage

    user = current_user()
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in {"approve", "reject"}:
        return jsonify(error="审核动作无效"), 400
    with storage.LOCK, storage.db() as conn:
        row = conn.execute("SELECT * FROM rack_requests WHERE id=?", (request_id,)).fetchone()
        if not row or row["status"] != "pending":
            return jsonify(error="请求不存在或已审核"), 404
        if not can_review_rack_request(row, user):
            return forbidden()
        data = json.loads(row["request_data"])
        if action == "reject":
            from .core import now_text

            reviewed_at = now_text()
            reject_reason = body.get("reason", "")
            conn.execute(
                "UPDATE rack_requests SET status='rejected',stage='rejected',reviewer_id=?,reviewed_at=?,reject_reason=? WHERE id=?",
                (user["用户id"], reviewed_at, reject_reason, request_id),
            )
            event_row = dict(row)
            event_row["reviewed_at"] = reviewed_at
            event_row["reject_reason"] = reject_reason
            services.notify_audit_event(conn, services.rack_request_event(event_row, "rejected", "服务器上下架审核已驳回", user["用户id"]))
            storage.prune_audit_records(conn)
            return jsonify(message="上下架请求已驳回")
        stage = row["stage"] if "stage" in row.keys() and row["stage"] else initial_rack_request_stage(row["request_type"])
        if stage == "allocation":
            if row["request_type"] != "上架":
                return jsonify(error="下架请求不需要机位IP分配"), 400
            server_data = services.rack_server_draft(row, body.get("server_data") or {})
            errors = services.validate_rack_allocation(server_data, request_id)
            if errors:
                return jsonify(error="；".join(errors)), 400
            from .core import now_text

            reviewed_at = now_text()
            conn.execute("""UPDATE rack_requests
                SET serial_number=?,stage='implementation',stage_data=?,allocation_reviewer_id=?,allocation_at=?,reviewer_id=?,reviewed_at=?,reject_reason=''
                WHERE id=?""", (
                server_data["序列号"], json.dumps(server_data, ensure_ascii=False), user["用户id"], reviewed_at,
                user["用户id"], reviewed_at, request_id,
            ))
            event_row = dict(row)
            event_row["reviewed_at"] = reviewed_at
            event_row["stage_data"] = json.dumps(server_data, ensure_ascii=False)
            services.notify_rack_stage(conn, event_row, "approved", "服务器机位IP分配已完成", user["用户id"], server_data)
            storage.prune_audit_records(conn)
            return jsonify(message="服务器机位IP分配已完成，已进入具体实施", request_id=request_id)
        if stage != "implementation":
            return jsonify(error="请求当前阶段不可审核"), 400
        if row["request_type"] == "上架":
            server_data = services.rack_server_draft(row, body.get("server_data") or {})
            # 具体实施阶段调整机位/IP 时，不得占用其他待实施请求已分配的资源
            conflict_errors = services.implementation_slot_conflict_errors(server_data, request_id) + services.same_kind_ip_conflict_errors(
                services.normalize_row("servers", server_data),
                include_official=False,
                include_implementation=True,
                exclude_request_id=request_id,
            )
            if conflict_errors:
                return jsonify(error="；".join(conflict_errors)), 400
        else:
            server_data = {}
        message, errors = services.complete_rack_request(conn, row, user, server_data)
        if errors:
            return jsonify(error="；".join(errors)), 400 if "不存在" not in "；".join(errors) else 409
        storage.prune_audit_records(conn)
    return jsonify(message=message, request_id=request_id)


@bp.get("/api/statistics")
@require_login
def statistics():
    user = current_user()
    if "view_statistics" not in capabilities_for(user):
        return forbidden()
    customers = storage.read_records("customers")
    cabinets = filter_records_for_user("cabinets", storage.read_records("cabinets"), user)
    servers = filter_records_for_user("servers", storage.read_records("servers"), user)
    user_map = {x["客户id"]: x["名称"] for x in customers}

    cabinet_map = {}
    for cabinet in cabinets:
        key = (cabinet["机房"], cabinet["机柜"])
        rack_positions = positions(cabinet["机位"])
        cabinet_map[key] = {
            "room": key[0], "name": key[1], "rack_range": cabinet["机位"],
            "power": cabinet.get("瓦数(kw)", ""), "annual_cost": cabinet["费用(年)"],
            "slots": [{"slot": str(pos), "status": "free", "servers": [], "occupied_by": ""} for pos in rack_positions],
        }
    for server in servers:
        key = (server["所在机房"], server["所属机柜"])
        cabinet = cabinet_map.setdefault(key, {"room": key[0], "name": key[1], "rack_range": "", "power": "", "annual_cost": "", "slots": []})
        item = dict(server)
        item["客户名称"] = user_map.get(server["客户id"], "未关联客户" if server["客户id"] else "公司自有")
        occupied = positions(server["所属机位"])
        if occupied:
            for pos in occupied:
                slot = next((x for x in cabinet["slots"] if x["slot"] == str(pos)), None)
                if not slot:
                    slot = {"slot": str(pos), "status": "free", "servers": [], "occupied_by": ""}
                    cabinet["slots"].append(slot)
                slot["occupied_by"] = server["序列号"]
                slot["status"] = "occupied"
            top = next(x for x in cabinet["slots"] if x["slot"] == str(max(occupied)))
            top["servers"].append(item)
        else:
            cabinet["slots"].append({"slot": server["所属机位"] or "未分配", "status": "occupied", "servers": [item], "occupied_by": server["序列号"]})
    # 在途机位状态：待具体实施上下架单 + 信息维护待审批变更（后者优先展示）
    for state_map in (services.implementation_slot_states(), services.pending_server_slot_states()):
        for key, slots in state_map.items():
            cabinet = cabinet_map.get(key)
            if not cabinet:
                continue
            for pos, pending in slots.items():
                slot = next((x for x in cabinet["slots"] if x["slot"] == str(pos)), None)
                if not slot:
                    slot = {"slot": str(pos), "status": "free", "servers": [], "occupied_by": ""}
                    cabinet["slots"].append(slot)
                slot["status"] = pending["status"]
                if pending["status"] in ("pending_occupied", "pending_implementation"):
                    slot["pending_server"] = pending["server"]

    def slot_num(value):
        try:
            return int(str(value).split("-")[0])
        except ValueError:
            return 9999
    result = list(cabinet_map.values())
    for cabinet in result:
        cabinet["slots"].sort(key=lambda x: slot_num(x["slot"]), reverse=True)
    result.sort(key=lambda x: (x["room"], x["name"]))
    return jsonify(summary={"cabinet_count": len(result), "server_count": len(servers), "client_count": len(customers)}, cabinets=result)


@bp.get("/health")
def health():
    from .core import now_text

    return jsonify(status="ok", time=now_text())
