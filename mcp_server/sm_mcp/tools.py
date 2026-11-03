"""工具定义与实现（P1：只读 + bootstrap）。

设计约束（与 plan/plan.md 第二节一致）：
- 不 1:1 映射后端接口，按领域合并，工具尽量少。
- 不做本地鉴权：身份完全由 SM_TOKEN 在后端解析，这里只把后端的 401/403 翻译成人话。
- 写操作会在返回里显式区分 ``applied`` / ``pending``；让 agent 不用猜 message 文案。
- 所有输出控制在合理长度：agent 的上下文很贵，默认只给摘要 + 前 N 条。
"""
from __future__ import annotations

import json
import time

from .client import SMError

# ---------------------------------------------------------------- 工具声明

ENTITY_HELP = "customers（客户）/ cabinets（机柜）/ servers（服务器）；以 sm_schema 的 entities 键为准"

TOOL_SPECS = [
    {
        "name": "sm_bootstrap",
        "description": (
            "【每次会话先调这个】一次性拿到当前 skill 正文、版本号、字段清单和你的身份。"
            "内容全部来自后端运行时，永远与当前实现一致——不要依赖本地 skill 文件里的字段列表，"
            "那些可能过期。返回的 version 可用于判断后端是否已升级。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_rules": {
                    "type": "boolean",
                    "description": "是否附带校验规则摘要（默认 true）",
                }
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sm_whoami",
        "description": (
            "查看当前 token 对应的身份（用户id / 身份 / 业务单位 / 所属机房）与 capabilities。"
            "写操作被拒（403）时先调它，不要在身份不符时反复重试。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sm_schema",
        "description": (
            "获取字段结构：实体表头、主键、字段别名、哪些身份可读/可写、上下架请求表头与阶段。"
            "由后端 ENTITIES 实时生成，新增字段会立刻出现在这里。写记录前用它确认列名，"
            "不要凭记忆拼字段名。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "只取某个实体（%s）；留空返回全部，含权限矩阵与规则" % ENTITY_HELP,
                }
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sm_query",
        "description": (
            "查询实体记录。filters 为「字段名 → 包含匹配值」（忽略大小写与空格），"
            "多个条件为「与」关系。返回里会说明总条数与实际显示条数。"
            "行级数据权限由后端裁剪：机房人员只看本机房，业务人员只看本单位。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": ENTITY_HELP},
                "filters": {
                    "type": "object",
                    "description": '例如 {"所在机房": "机房A", "品牌": "Dell"}；值为包含匹配',
                    "additionalProperties": {"type": "string"},
                },
                "record_state": {
                    "type": "string",
                    "enum": ["official", "pending"],
                    "description": "official=正式数据（默认）；pending=含待审批变更的视图",
                },
                "limit": {"type": "integer", "description": "最多返回多少条，默认 10，上限 200"},
            },
            "required": ["entity"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sm_stats",
        "description": "机位/服务器数量统计，按机房与机柜汇总。回答「还有多少空位」这类问题用它，不要用 sm_query 逐条数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sm_rack_list",
        "description": (
            "列出上下架请求（只给摘要：id/状态/阶段/序列号/客户/提交人/时间）。"
            "上架请求的阶段是 allocation（机位IP分配）→ implementation（具体实施），"
            "下架 / 变更请求直接进 implementation，驳回后 stage=rejected。看完整请求内容用 sm_rack_get。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected"],
                    "description": "默认 pending",
                },
                "stage": {
                    "type": "string",
                    "enum": ["allocation", "implementation", "rejected"],
                    "description": "只看某个阶段；留空=全部",
                },
                "scope": {
                    "type": "string",
                    "enum": ["review", "submitted"],
                    "description": "review=按你的审核权限可见（默认）；submitted=只看我自己提交的",
                },
                "limit": {"type": "integer", "description": "最多返回多少条，默认 20"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sm_rack_get",
        "description": "按 request_id 取单个上下架请求的完整内容（请求字段 + 分配/实施阶段数据 + 驳回原因）。",
        "inputSchema": {
            "type": "object",
            "properties": {"request_id": {"type": "string"}},
            "required": ["request_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    # ------------------------------------------------------------ P2 写工具
    {
        "name": "sm_upsert",
        "description": (
            "新增或修改一条记录。传 old_key 就是修改，不传就是新增。"
            "**看返回值里的 applied / pending 字段，不要看 message 文案**："
            "applied=true 表示已直接落库；pending=true 表示只是提交了待审批申请（带 change_id，"
            "需再调 sm_review 通过才生效）。"
            "修改时 row 只需给要改的字段，工具会自动取回原记录合并后再提交；字段名以 sm_schema 为准。"
            "servers 的「接入行情」「接入柜台」是多值字段（见 sm_schema 的 multi_value_fields）："
            "值可以传数组 [\"CTP行情\",\"Level-2\"]，也可以传 \"CTP行情, Level-2\"，服务端会去重归一。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": ENTITY_HELP},
                "row": {
                    "type": "object",
                    "description": "字段名 → 值。新增时必填项要齐全（servers 至少 序列号 + 客户id）",
                },
                "old_key": {
                    "type": ["string", "object"],
                    "description": (
                        "要修改的原记录主键。servers 可直接传序列号字符串，"
                        '也可传 {"序列号": "..."}；cabinets 是复合主键必须传 {"机房": "...", "机柜": "..."}。'
                        "不传 = 新增"
                    ),
                },
            },
            "required": ["entity", "row"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "sm_delete",
        "description": (
            "删除记录。**后端没有回收站，删掉不可撤销**。"
            "调用时必须同时传 keys 与 confirm_keys，两者内容必须完全相同，否则直接拒绝、一条都不删——"
            "这是强制的二次确认，防止手滑或误读上下文，不要尝试用不同参数绕过。"
            "管理员 token 下删除直接生效；其他身份只提交待审批申请。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": ENTITY_HELP},
                "keys": {
                    "type": "array",
                    "items": {"type": ["string", "object"]},
                    "description": "要删的主键列表，单次最多 20 条；复合主键用对象形式",
                },
                "confirm_keys": {
                    "type": "array",
                    "items": {"type": ["string", "object"]},
                    "description": "把 keys 原样再写一遍，内容必须完全一致（顺序可不同），否则拒绝执行",
                },
            },
            "required": ["entity", "keys", "confirm_keys"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "sm_pending",
        "description": (
            "查看待审批的变更申请（id / 实体 / 操作 / 记录主键 / 提交人 / 时间）。"
            "只有管理人员能拿到 change_id；其他身份调这个会降级成「待审批数据形态」的行视图。"
            "审核用 sm_review。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {
                    "type": "string",
                    "description": "只看某个实体的待审批申请；留空=全部",
                },
                "limit": {"type": "integer", "description": "最多返回多少条，默认 50"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sm_review",
        "description": (
            "审核操作变更申请（仅管理人员）。approve 后数据才真正落库，reject 必须给 reason。"
            "ids 只有一条时走单条审核（可带 new_data 覆盖提交内容），多条走批量。"
            "**注意这是审批操作的唯一入口之一，动作不可撤销**。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "string"}, "description": "change_id 列表"},
                "action": {"type": "string", "enum": ["approve", "reject"]},
                "reason": {"type": "string", "description": "驳回原因，action=reject 时必填"},
                "new_data": {
                    "type": "object",
                    "description": (
                        "审批时替换的数据：单条时直接给字段对象；"
                        '多条时给 {"<change_id>": {字段对象}}。不给则用提交时的数据'
                    ),
                },
            },
            "required": ["ids", "action"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "sm_rack_submit",
        "description": (
            "提交服务器上下架请求。row 里的「请求类型」决定流程："
            "上架 → allocation 机位IP分配 → implementation 具体实施（新增服务器记录）；"
            "下架 / 变更 → 直接进 implementation（下架完成后服务器整行移入回收站，变更完成后直接写正式表）。"
            "字段见 sm_schema 的 rack_headers（序列号、请求类型、客户id 等）；"
            "上架要求序列号不在服务器表里，下架/变更要求序列号已存在。同一序列号已有待审核请求会被拒绝。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "row": {"type": "object", "description": "上下架请求字段 → 值，表头见 sm_schema 的 rack_headers"},
            },
            "required": ["row"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "sm_rack_review",
        "description": (
            "审核上下架请求（管理人员全量 / 机房人员限本机房）。"
            "**stage 必须与请求的真实阶段一致**：工具会先查该请求的真实 stage，不一致直接中止且不产生任何变更。"
            "分配阶段（allocation）通过时要给 server_data（机位/IP）；实施阶段（implementation）上架与变更请求也可带 server_data 调整（变更以现有记录为底合并）。"
            "驳回必须给 reason。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "stage": {
                    "type": "string",
                    "enum": ["allocation", "implementation"],
                    "description": "你以为的当前阶段；与真实阶段不符会被拒绝（不会替你猜）",
                },
                "action": {"type": "string", "enum": ["approve", "reject"]},
                "server_data": {"type": "object", "description": "机位/IP 等服务器字段，分配阶段通过时通常必填"},
                "reason": {"type": "string", "description": "驳回原因，action=reject 时必填"},
            },
            "required": ["request_id", "stage", "action"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]

TOOL_NAMES = [spec["name"] for spec in TOOL_SPECS]

# 工具 → 该工具必须持有的 capability。不在表里的工具对所有身份可见，
# 实体级权限（能不能改 servers / 删 cabinets）一律交给后端判，这里不复制一份规则。
TOOL_CAPABILITIES = {
    "sm_pending": "review_operations",
    "sm_review": "review_operations",
    "sm_rack_review": "review_rack_requests",
}


def visible_tool_specs(capabilities):
    """按身份裁剪 tools/list。

    ``capabilities=None`` 表示身份未知（后端不可达、未配 token、/api/me 失败）——
    此时返回**全量**列表：宁可让 agent 看到工具、调用后被后端 403 挡掉，
    也不要让它以为工具不存在而改用别的（更危险的）办法。
    """
    if capabilities is None:
        return TOOL_SPECS
    allowed = set(capabilities)
    return [
        spec for spec in TOOL_SPECS
        if spec["name"] not in TOOL_CAPABILITIES or TOOL_CAPABILITIES[spec["name"]] in allowed
    ]



# ---------------------------------------------------------------- 小工具函数

def _dumps(payload):
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _norm(text):
    return str(text or "").replace(" ", "").replace("\u3000", "").lower()


def _resolve_field(headers, name):
    """把用户写的字段名对到真实表头：先精确，再忽略空格/大小写。"""
    if name in headers:
        return name
    target = _norm(name)
    for header in headers:
        if _norm(header) == target:
            return header
    return None


def _match(value, needle):
    return _norm(needle) in _norm(value)


def _table(rows, headers, limit=None):
    """渲染成 markdown 表格；空值显示为空。"""
    shown = rows if limit is None else rows[:limit]
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in shown:
        lines.append("| " + " | ".join(str(row.get(h, "") or "").replace("|", "\\|") for h in headers) + " |")
    return "\n".join(lines)


# ---------------------------------------------------------------- 工具实现

def _bootstrap(client, args):
    manifest = client.json("/api/skill/manifest")
    skill_md = client.text("/api/skill/docs", {"name": "skill"})
    schema = client.json("/api/skill/schema")

    lines = [
        "# server-management · skill v%s（后端生成于 %s）" % (
            manifest.get("version", "?"), manifest.get("updated_at") or "未知"),
        "",
        "以下正文与字段由后端实时下发，**以本文件为准**；本地 skill 文件只用于发现这个 MCP。",
        "",
        "---",
        "",
        skill_md.strip(),
        "",
        "---",
        "",
        "## 字段清单（实时）",
    ]
    for key, meta in schema.get("entities", {}).items():
        lines.append("- **%s**（%s）主键=%s：%s" % (
            key, meta.get("label", ""), "/".join(meta.get("key") or []),
            " | ".join(meta.get("headers") or []),
        ))
    lines.append("- **上下架请求**：%s" % " | ".join(schema.get("rack_headers") or []))
    lines.append("")
    lines.append("上下架阶段：%s → %s（下架请求直接进 implementation）" % tuple(
        (schema.get("rack_stages") or ["allocation", "implementation"])[:2]))

    if args.get("include_rules", True):
        rules = schema.get("rules") or []
        if rules:
            lines.append("")
            lines.append("## 校验规则（校验失败时先看这里）")
            for rule in rules:
                lines.append("- " + rule)

    lines.append("")
    lines.append("## 身份")
    try:
        me = client.json("/api/me")
        lines.append("- 用户id=%s 身份=%s 业务单位=%s 所属机房=%s" % (
            me.get("用户id", ""), me.get("身份", ""),
            me.get("业务单位") or "-", me.get("所属机房") or "-"))
        lines.append("- capabilities=%s" % ", ".join(me.get("capabilities") or []))
        if me.get("身份") == "管理人员":
            lines.append("- 注意：你的写操作会**直接生效**（不走审批流）。")
        else:
            lines.append("- 注意：你的写操作会进入待审批，需管理人员 approve 后才落库。")
    except SMError as exc:
        lines.append("- 拿不到身份：%s" % exc)
        lines.append("  （读文档不需要 token，但任何业务查询/写入都需要先配好 SM_TOKEN。）")
    return "\n".join(lines)


def _whoami(client, args):
    me = client.json("/api/me")
    return _dumps(me)


def _schema(client, args):
    schema = client.json("/api/skill/schema")
    entity = (args.get("entity") or "").strip()
    if not entity:
        return _dumps(schema)
    entities = schema.get("entities") or {}
    if entity not in entities:
        raise SMError("未知实体 %s；可用：%s" % (entity, ", ".join(entities)))
    return _dumps({
        "entity": entity,
        "definition": entities[entity],
        "rack_headers": schema.get("rack_headers"),
        "rack_aliases": schema.get("rack_aliases"),
        "capabilities": schema.get("capabilities"),
        "rules": schema.get("rules"),
    })


def _query(client, args):
    entity = (args.get("entity") or "").strip()
    if not entity:
        raise SMError("缺少 entity；可用：%s" % ENTITY_HELP)
    state = args.get("record_state") or "official"
    if state not in ("official", "pending"):
        raise SMError("record_state 只能是 official 或 pending")
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 200))

    data = client.json("/api/" + entity, {"record_state": state})
    records = data.get("records") or []
    headers = list((data.get("meta") or {}).get("headers") or [])

    filters = args.get("filters") or {}
    unknown = []
    for name in filters:
        if _resolve_field(headers, name) is None:
            unknown.append(name)
    if unknown:
        raise SMError(
            "这些字段不存在：%s。先调 sm_schema(entity=%s) 看可用字段，不要猜列名。"
            % (", ".join(unknown), entity)
        )

    for name, needle in filters.items():
        field = _resolve_field(headers, name)
        records = [row for row in records if _match(row.get(field, ""), needle)]

    total = len(records)
    shown = records[:limit]
    head = "共 %d 条匹配，显示前 %d 条（record_state=%s，字段 %d 个）" % (
        total, len(shown), state, len(headers))
    if total == 0:
        return head + "\n没有匹配的记录。"
    if total > len(shown):
        head += "\n如需更多：加大 limit（上限 200）或增加 filters 缩小范围。"
    return head + "\n\n" + _dumps(
        [{h: row.get(h, "") for h in headers} for row in shown]
    )


def _stats(client, args):
    return _dumps(client.json("/api/statistics"))


def _rack_list(client, args):
    status = args.get("status") or "pending"
    params = {"status": status}
    if args.get("stage"):
        params["stage"] = args["stage"]
    if args.get("scope"):
        params["scope"] = args["scope"]
    data = client.json("/api/rack-requests", params)
    rows = data.get("requests") or []
    try:
        limit = int(args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    brief = []
    for row in rows[:limit]:
        request_data = row.get("request_data")
        if isinstance(request_data, str):
            try:
                request_data = json.loads(request_data)
            except ValueError:
                request_data = {}
        request_data = request_data or {}
        brief.append({
            "id": row.get("id", ""),
            "类型": row.get("request_type", ""),
            "状态": row.get("status", ""),
            "阶段": row.get("stage", ""),
            "序列号": row.get("serial_number") or request_data.get("序列号", ""),
            "客户": request_data.get("客户名称") or request_data.get("客户id", ""),
            "机房": request_data.get("所在机房", ""),
            "提交人": row.get("submitter", ""),
            "提交时间": row.get("submitted_at", ""),
        })

    head = "%s 状态共 %d 条，显示 %d 条" % (status, len(rows), len(brief))
    if not brief:
        return head + "\n当前没有符合条件的上下架请求。"
    if len(rows) > len(brief):
        head += "（用 limit 调整）"
    return head + "\n\n" + _table(
        brief, ["id", "类型", "状态", "阶段", "序列号", "客户", "机房", "提交人", "提交时间"]
    ) + "\n\n查看完整请求内容：sm_rack_get(request_id)"


def _find_rack_request(client, request_id):
    """按 id 在三个状态里找请求。后端 status 是等值过滤、没有 all，只能逐个查。"""
    for status in ("pending", "approved", "rejected"):
        data = client.json("/api/rack-requests", {"status": status})
        for row in data.get("requests") or []:
            if str(row.get("id")) == str(request_id):
                return row
    return None


def _rack_get(client, args):
    request_id = (args.get("request_id") or "").strip()
    if not request_id:
        raise SMError("缺少 request_id")

    def flatten(row):
        """request_data / stage_data 在库里是 JSON 字符串，转成对象再给 agent，
        否则 agent 看到的是一坨 \\uXXXX 转义文本，既难读又费 token。"""
        row = dict(row)
        for field in ("request_data", "stage_data"):
            value = row.get(field)
            if isinstance(value, str) and value.strip().startswith("{"):
                try:
                    row[field] = json.loads(value)
                except ValueError:
                    pass
        return row

    # 后端的 status 是 SQL 等值过滤（默认 pending），没有 "all"，所以要按状态逐个查
    found = _find_rack_request(client, request_id)
    if found is not None:
        return _dumps(flatten(found))
    raise SMError("未找到 request_id=%s 的上下架请求（可能已被处理或不在你的可见范围内）" % request_id)


# ---------------------------------------------------------------- 写操作（P2）

WRITABLE_ENTITIES = ("servers", "cabinets", "customers")
MAX_DELETE_KEYS = 20
# 同一工具 + 同样参数在这个窗口内复用上次结果，挡住 agent 原样重试造成的重复提交
IDEMPOTENCY_WINDOW = 60
_IDEMPOTENCY = {}


def reset_idempotency():
    """测试用：清空短时幂等缓存，避免用例互相命中。"""
    _IDEMPOTENCY.clear()


def _require_entity(entity):
    name = str(entity or "").strip()
    if name not in WRITABLE_ENTITIES:
        raise SMError("未知实体 %s；可用：%s" % (name or "(空)", ", ".join(WRITABLE_ENTITIES)))
    return name


def _load_entity(client, entity):
    """取表头、主键字段与现有记录。

    写之前必须先拿到这三样：字段名校验（不猜列名）、主键展开（复合主键）、
    修改时合并原值（agent 只需给要改的字段）都依赖它。
    """
    data = client.json("/api/" + entity)
    meta = data.get("meta") or {}
    return (
        list(meta.get("headers") or []),
        list(meta.get("key") or []),
        list(data.get("records") or []),
    )


def _canonical_fields(headers, row):
    """把用户写的字段名映射成真实表头，遇到不存在的字段直接报错而不是静默丢弃。"""
    if not isinstance(row, dict) or not row:
        raise SMError("row 不能为空")
    resolved, unknown = {}, []
    for name, value in row.items():
        field = _resolve_field(headers, name)
        if field is None:
            unknown.append(name)
        else:
            resolved[field] = value
    if unknown:
        raise SMError(
            "这些字段不存在：%s。先调 sm_schema 看可用字段，不要猜列名。" % ", ".join(unknown)
        )
    return resolved


def _key_parts(key_fields, raw, entity):
    """主键归一：单主键实体允许直接给字符串，复合主键必须给对象。"""
    if isinstance(raw, dict):
        return {f: str(raw.get(f, "") or "").strip() for f in key_fields}
    if len(key_fields) == 1:
        return {key_fields[0]: str(raw if raw is not None else "").strip()}
    raise SMError(
        "实体 %s 是复合主键，主键要用对象形式：%s" % (entity, "、".join(key_fields))
    )


def _key_text(key_fields, row):
    return " / ".join(str(row.get(f, "") or "").strip() for f in key_fields)


def _same_key(left, right):
    return _norm(left) == _norm(right)


def _write_result(operation, entity, record_key, result):
    """把后端的中文 message 翻译成结构化字段。

    后端没有返回布尔标记，只能靠文案判断是否直接生效（routes.py 里 admin 分支固定带
    「直接生效」，其他身份是「已提交…申请」）。这个判断集中在这一处，agent 就不用猜措辞。
    """
    result = result or {}
    message = str(result.get("message") or "")
    change_id = str(result.get("change_id") or "")
    change_ids = result.get("change_ids") or []
    applied = "直接生效" in message
    return _dumps({
        "operation": operation,
        "entity": entity,
        "record_key": record_key,
        "applied": applied,
        "pending": bool(change_id or change_ids) and not applied,
        "change_id": change_id,
        "change_ids": change_ids,
        "message": message,
        "next": (
            "已生效，可用 sm_query 复核。"
            if applied
            else '尚未生效：这是待审批申请，需管理人员调 sm_review(ids=["%s"], action="approve") 后才落库。'
            % (change_id or "<change_id>")
        ),
    })


def _idempotent(name, args, run):
    """同一工具 + 同样参数在 60 秒内复用上次结果。

    agent 遇到超时或看不懂返回时常原样重试，写操作重放会多出一条待审批记录。
    只包新增类操作（upsert / rack_submit）；删除和审核不做幂等——那两个本来就该由人确认。
    """
    now = time.time()
    for key in [k for k, (stamp, _) in _IDEMPOTENCY.items() if now - stamp > IDEMPOTENCY_WINDOW]:
        _IDEMPOTENCY.pop(key, None)
    cache_key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True, default=str))
    hit = _IDEMPOTENCY.get(cache_key)
    if hit:
        stamp, value = hit
        if now - stamp <= IDEMPOTENCY_WINDOW:
            return value
    value = run()
    _IDEMPOTENCY[cache_key] = (now, value)
    return value


def _upsert(client, args):
    entity = _require_entity(args.get("entity"))
    headers, key_fields, records = _load_entity(client, entity)
    row = _canonical_fields(headers, args.get("row"))
    old_key = args.get("old_key")
    is_update = old_key not in (None, "", {}, [])

    def run():
        if not is_update:
            result = client.json("/api/%s/add" % entity, method="POST", payload=row)
            return _write_result("新增", entity, _key_text(key_fields, row), result)

        old = _key_parts(key_fields, old_key, entity)
        blank = [f for f in key_fields if not old.get(f)]
        if blank:
            raise SMError("old_key 缺少主键字段：%s" % "、".join(blank))
        current = next(
            (r for r in records if _same_key(_key_text(key_fields, r), _key_text(key_fields, old))),
            None,
        )
        if current is None:
            raise SMError(
                "找不到主键为 %s 的%s记录；先用 sm_query 确认（也可能记录已被删除）。"
                % (_key_text(key_fields, old), entity)
            )
        # 只给要改的字段即可：以服务端现有记录为底，避免 agent 回传 27 列还可能传错
        new = dict(current)
        new.update(row)
        result = client.json("/api/%s/update" % entity, method="POST", payload={"old": old, "new": new})
        return _write_result("修改", entity, _key_text(key_fields, new), result)

    return _idempotent("sm_upsert", args, run)


def _delete(client, args):
    entity = _require_entity(args.get("entity"))
    keys = args.get("keys")
    confirm = args.get("confirm_keys")
    if not isinstance(keys, list) or not keys:
        raise SMError("keys 不能为空")
    if len(keys) > MAX_DELETE_KEYS:
        raise SMError("单次最多删除 %d 条（本次 %d 条），请分批调用" % (MAX_DELETE_KEYS, len(keys)))
    if not isinstance(confirm, list):
        raise SMError(
            "缺少 confirm_keys。删除必须把 keys 原样再写一遍作为二次确认，否则拒绝执行（本次未删除任何记录）。"
        )

    headers, key_fields, records = _load_entity(client, entity)
    parts = [_key_parts(key_fields, item, entity) for item in keys]
    confirm_parts = [_key_parts(key_fields, item, entity) for item in confirm]

    wanted = sorted(_norm(_key_text(key_fields, p)) for p in parts)
    confirmed = sorted(_norm(_key_text(key_fields, p)) for p in confirm_parts)
    if len(set(wanted)) != len(wanted):
        raise SMError("keys 里有重复主键，请去重后再调用")
    if wanted != confirmed:
        raise SMError(
            "confirm_keys 与 keys 不一致，已拒绝执行（一条都没删）。\n"
            "  keys         = %s\n  confirm_keys = %s\n"
            "删除不可撤销：请确认要删的正是这些记录，确认无误后把 confirm_keys 写成与 keys 完全相同再调。"
            % ("; ".join(wanted), "; ".join(confirmed))
        )

    existing = {_norm(_key_text(key_fields, r)) for r in records}
    missing = [p for p in parts if _norm(_key_text(key_fields, p)) not in existing]
    if missing:
        raise SMError(
            "这些记录不存在（可能已被删除，或主键写错）：%s。先用 sm_query 确认；本次未删除任何记录。"
            % "; ".join(_key_text(key_fields, p) for p in missing)
        )

    if len(parts) == 1:
        result = client.json("/api/%s/delete" % entity, method="POST", payload=parts[0])
        return _write_result("删除", entity, _key_text(key_fields, parts[0]), result)

    result = client.json("/api/%s/delete-batch" % entity, method="POST", payload={"records": parts})
    return _write_result("批量删除", entity, "%d 条" % len(parts), result)


def _pending(client, args):
    entity = str(args.get("entity") or "").strip()
    try:
        limit = int(args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    try:
        data = client.json("/api/changes", {"status": "pending"})
    except SMError as exc:
        if "无此能力" not in str(exc):
            raise
        # 非管理人员没有 review_operations，/api/changes 直接 403，也就没有 change_id。
        # 退化成 pending 数据视图：能看到「哪些记录有未生效的改动」，只是不能在这里批准。
        fallback_entity = entity if entity in WRITABLE_ENTITIES else "servers"
        text = _query(client, {"entity": fallback_entity, "record_state": "pending", "limit": limit})
        return (
            "你的身份没有「操作审核」权限，看不到 change_id（/api/changes 返回 403）。\n"
            "以下是 %s 的待审批数据形态（pending 视图）；审核需要管理人员账号。\n\n%s"
            % (fallback_entity, text)
        )

    rows = data.get("changes") or []
    if entity:
        rows = [row for row in rows if row.get("entity") == entity]
    brief = [{
        "change_id": row.get("id", ""),
        "实体": row.get("entity_label") or row.get("entity", ""),
        "操作": row.get("operation", ""),
        "记录主键": row.get("record_key", ""),
        "提交人": row.get("submitter", ""),
        "提交时间": row.get("submitted_at", ""),
    } for row in rows[:limit]]
    head = "待审批申请共 %d 条，显示 %d 条" % (len(rows), len(brief))
    if not brief:
        return head + "\n当前没有待审批申请。"
    return (
        head
        + "\n\n"
        + _table(brief, ["change_id", "实体", "操作", "记录主键", "提交人", "提交时间"])
        + '\n\n审核：sm_review(ids=["<change_id>"], action="approve")；驳回要给 reason。'
    )


def _review(client, args):
    raw_ids = args.get("ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        raise SMError("ids 不能为空（先用 sm_pending 查 change_id）")
    ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    if not ids:
        raise SMError("ids 不能为空（先用 sm_pending 查 change_id）")
    action = str(args.get("action") or "").strip()
    if action not in ("approve", "reject"):
        raise SMError("action 只能是 approve 或 reject")
    reason = str(args.get("reason") or "").strip()
    if action == "reject" and not reason:
        raise SMError("驳回必须给 reason，说明为什么驳回")
    new_data = args.get("new_data") or {}
    if not isinstance(new_data, dict):
        raise SMError("new_data 必须是对象")

    if len(ids) == 1:
        payload = {"action": action, "reason": reason}
        if new_data:
            payload["new_data"] = new_data
        result = client.json("/api/changes/%s/review" % ids[0], method="POST", payload=payload)
        return _dumps({
            "action": action,
            "reviewed": [{"id": ids[0], "message": result.get("message", "")}],
            "message": result.get("message", ""),
        })

    payload = {"action": action, "ids": ids, "reason": reason}
    if new_data:
        payload["new_data"] = new_data
    result = client.json("/api/changes/review-batch", method="POST", payload=payload)
    return _dumps({
        "action": action,
        "reviewed": result.get("results") or [],
        "count": result.get("reviewed", 0),
        "message": result.get("message", ""),
    })


RACK_SUBMIT_NEXT = {
    "上架": "请求已提交，等待机位IP分配（allocation）审核；用 sm_rack_get 查看进度。",
    "下架": "请求已提交，直接进入具体实施（implementation）；完成后服务器整行移入回收站。",
    "变更": "请求已提交，直接进入具体实施（implementation）；完成后直接写入服务器正式表。",
}


def _rack_submit(client, args):
    row = args.get("row")
    if not isinstance(row, dict) or not row:
        raise SMError("row 不能为空（上下架请求字段，表头见 sm_schema 的 rack_headers）")
    request_type = str(row.get("请求类型") or "").strip() or "上架"

    def run():
        result = client.json("/api/rack-requests", method="POST", payload=row)
        return _dumps({
            "submitted": True,
            "request_id": result.get("request_id", ""),
            "message": result.get("message", ""),
            "next": RACK_SUBMIT_NEXT.get(request_type, RACK_SUBMIT_NEXT["上架"]),
        })

    return _idempotent("sm_rack_submit", args, run)


def _rack_review(client, args):
    request_id = str(args.get("request_id") or "").strip()
    if not request_id:
        raise SMError("缺少 request_id")
    stage = str(args.get("stage") or "").strip()
    if stage not in ("allocation", "implementation"):
        raise SMError("stage 必须显式给 allocation 或 implementation，不要留空让服务端猜")
    action = str(args.get("action") or "").strip()
    if action not in ("approve", "reject"):
        raise SMError("action 只能是 approve 或 reject")
    reason = str(args.get("reason") or "").strip()
    if action == "reject" and not reason:
        raise SMError("驳回必须给 reason")

    # 前置校验：真实阶段与入参不一致就中止，绝不交给后端去猜（那会走错分支、产生副作用）
    row = _find_rack_request(client, request_id)
    if row is None:
        raise SMError(
            "未找到 request_id=%s 的上下架请求（可能已处理或不在你的可见范围），未做任何变更。" % request_id
        )
    status = str(row.get("status") or "")
    if status != "pending":
        raise SMError("该请求当前状态是 %s、不是待审核，未做任何变更。" % status)
    real_stage = str(row.get("stage") or "")
    if real_stage != stage:
        raise SMError(
            "request_id=%s 的真实阶段是 %s，你传的是 %s，已中止（未产生任何变更）。请按真实阶段重新调用。"
            % (request_id, real_stage or "(空)", stage)
        )

    payload = {"action": action}
    if action == "reject":
        payload["reason"] = reason
    else:
        server_data = args.get("server_data")
        if isinstance(server_data, dict) and server_data:
            payload["server_data"] = server_data
    result = client.json("/api/rack-requests/%s/review" % request_id, method="POST", payload=payload)
    return _dumps({
        "request_id": request_id,
        "stage": stage,
        "action": action,
        "message": result.get("message", ""),
    })


HANDLERS = {
    "sm_bootstrap": _bootstrap,
    "sm_whoami": _whoami,
    "sm_schema": _schema,
    "sm_query": _query,
    "sm_stats": _stats,
    "sm_rack_list": _rack_list,
    "sm_rack_get": _rack_get,
    "sm_upsert": _upsert,
    "sm_delete": _delete,
    "sm_pending": _pending,
    "sm_review": _review,
    "sm_rack_submit": _rack_submit,
    "sm_rack_review": _rack_review,
}


def call_tool(name, args, client):
    handler = HANDLERS.get(name)
    if handler is None:
        raise SMError("未知工具 %s；可用：%s" % (name, ", ".join(TOOL_NAMES)))
    return handler(client, args or {})
