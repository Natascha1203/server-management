# 服务器资产管理系统 · API 参考

本文件是 server-management API 的详细参考，由 `SKILL.md` 按需引用。含实体字段清单、完整接口参数与返回结构、校验规则、错误码。

## 1. 数据模型（三大实体）

| 实体 | URL 段 | 主键 key | 文件名 | 说明 |
|---|---|---|---|---|
| 服务器 | `servers` | `序列号` | 服务器信息表.xlsx | 核心资产，27 字段 |
| 机柜 | `cabinets` | `机房`+`机柜` | 机柜信息表.xlsx | 8 字段 |
| 客户 | `customers` | `客户id` | 客户表.xlsx | 8 字段 |

### servers（27 列）

序列号、品牌、型号、客户id、客户名称、负责人、归属单位、CPU型号、网卡、管理ip、交易ip、带外ip、管理密码、带外密码、服务器成本(万)、用途分类、用途、防火墙白名单、资源申请编号、是否液冷、所在机房、所属机柜、所属机位、上架时间、接入行情、接入柜台、备注

> `备注` 为末列。上下架请求（`RACK_REQUEST_HEADERS`）本就含 `备注`，上架请求经「机位IP分配 → 具体实施」落库为服务器记录时会**自动带入**该备注；分配阶段也可在 `server_data.备注` 里改写。
>
> `上架时间` 只存日期（`YYYY-MM-DD`）。上下架请求落库时**自动填入请求提交日期**（`submitted_at` 的日期部分），上下架请求表头里**没有**该字段（无需填写）；「机位IP分配 / 具体实施」两阶段的 `server_data.上架时间` 可改写，以最后一次提交为准；为空时回落到提交日期。提交 `2026/9/1` 之类的写法会被归一成 `2026-09-01`。
>
> `防火墙白名单` 与 `备注` 支持**多行文本**（值里可含 `\n`），读写接口按普通字符串处理，不做转义或截断。Web UI 的编辑/审核表单把这两个字段渲染为可拖拽调整高度的多行编辑框。
>
> `接入行情` / `接入柜台` 是**多值标签字段**：存 `A, B` 文本，写入时也接受数组（去重后按 `, ` 拼接）。候选标签由管理人员在设置页维护，接口见 2.7。

### cabinets（8 列）

机房、机柜、机位、瓦数(kw)、费用(年)、管理ip网段、交易ip网段、带外ip网段

### customers（8 列）

客户id、名称、资产、月产出、业务单位、期货业务单位、负责人、客户经理

### 字段别名（提交时自动映射）

- servers：`归属`→归属单位、`归属部门`→归属单位、`成本`→服务器成本(万)、`所属客户id`→客户id、`密码`→管理密码
- cabinets：`瓦数(w)`→瓦数(kw)
- 上下架请求：`成本`→服务器成本(万)

## 2. 完整接口清单

### 2.1 查询类

**列表（全部正式数据）**
```
GET /api/<entity>?record_state=official|pending
返回：{"records":[...], "meta":{...}, "record_state":"..."}
```

**条件检索**
```
POST /api/<entity>/search?record_state=official
Body: {"filters":[{"field":"序列号","pattern":"^SN-"}],
       "sorts":[{"field":"序列号","direction":"asc"}]}
返回：{"records":[...], "meta":{...}, "count":N, "record_state":"..."}
```
- `filters`：每项 `field`（列名或"在途状态"）+ `pattern`（正则，大小写不敏感）。
- `sorts`：每项 `field` + `direction`（`asc`/`desc`）。
- 未知列名 → 400「未知列名：xxx」；无效正则 → 400。

**当前用户**：`GET /api/me`

**选项（下拉数据源）**：`GET /api/cabinet-options`、`GET /api/customer-options`

### 2.2 变更类（非管理人员走审批流；管理人员直接生效）

> **身份分流**：`管理人员` 调用下列任一变更接口 → **免审批、立即写入正式表**（message 含「直接生效」），同时仍生成版本记录与审计留痕。其他身份 → 仅**提交申请**（message 含「已提交…申请」，返回 `change_id`），需再调 2.3 的 `review` 才生效。

**新增**：`POST /api/<entity>/add`，Body 为字段字典 → 管理人员：`{"message":"已保存并直接生效",...}`；其他身份：`{"message":"已提交新增申请","change_id":"..."}`

**修改**：`POST /api/<entity>/update`，Body `{"old":{主键:旧值},"new":{...}}` → 需 old 定位原记录，new 为新值

**删除**：`POST /api/<entity>/delete`，Body `{主键:值}`

**批量删除**：`POST /api/<entity>/delete-batch`，Body `{"records":[{主键:值},...]}`

**批量导入**：`POST /api/<entity>/import?mode=full|merge`，multipart `file=<xlsx|csv>`
- `mode=full`（默认）：同主键整体替换，未提供列置空。
- `mode=merge`：仅覆盖「提供了非空值的列」，空值列保留旧值。
- **表头必须包含实体全部列**，否则 400「缺少列」。

**下载模板**：`GET /api/<entity>/template?format=xlsx|csv`

**导出**：`POST /api/<entity>/export?format=xlsx|csv`，Body 同 search 的 filters/sorts → 返回文件流

### 2.3 审核类（变更申请 changes）

**查看待审核**：`GET /api/changes?status=pending|approved|rejected`
返回：`{"changes":[{id, entity, operation, record_key, new_data, status, submitter, submitted_at, ...}]}`

**单条审核**：`POST /api/changes/<change_id>/review`，Body `{"action":"approve"}` 或 `{"action":"reject","reason":"..."}`

**批量审核**：`POST /api/changes/review-batch`，Body `{"action":"approve","ids":[...]}` 或 `{"action":"reject","ids":[...],"reason":"..."}`

**更新待审批申请**（仅提交者）：`POST /api/changes/<change_id>/update-pending`，Body `{"new_data":{...}}`

**撤回待审批申请**（仅提交者）：`POST /api/changes/<change_id>/withdraw`

### 2.4 上下架请求（rack-requests，两阶段审批）

字段（RACK_REQUEST_HEADERS）：序列号、品牌、型号、CPU型号、网卡、服务器成本(万)、防火墙白名单、资源申请编号、是否液冷、是否上架、是否重装系统、初始密码、上下架需求、备注、客户id、客户名称、所在机房

**提交**：`POST /api/rack-requests`，Body 为请求字段 → `{"message":"上下架请求已提交","request_id":"..."}`
- `是否上架` 取 `"是"`/`"否"`。

**查询**：`GET /api/rack-requests?status=pending|approved|rejected&scope=review|submitted&stage=allocation|implementation`

**导入**：`POST /api/rack-requests/import`，multipart `file=<xlsx|csv>`

**修改**（仅提交者）：`POST /api/rack-requests/<request_id>/update`，Body 为完整请求字段

**撤回**：`POST /api/rack-requests/<request_id>/withdraw`

**审核（两阶段）**：`POST /api/rack-requests/<request_id>/review`

上架请求是**两阶段**审批：**同一个接口、同一个 body，调两次**。系统按请求当前所处阶段（`stage`）自动区分行为，**无需在 body 里指定 stage**。

Body 结构：
```json
{
  "action": "approve",
  "server_data": {
    "所在机房": "数码通期货",
    "所属机柜": "D06",
    "所属机位": "19-20",
    "管理ip": "10.72.62.8",
    "交易ip": "192.168.62.8",
    "带外ip": "10.72.61.8"
  }
}
```
即 `server_data` = 机位 + 三个 IP，共 6 个字段（`机位` 必填，`管理ip`、`带外ip` 为分配阶段必填校验项）。

两次调用的差别：

| | 第 1 次 | 第 2 次 |
|---|---|---|
| 调用前 `stage` | `allocation`（机位IP分配） | `implementation`（具体实施） |
| 返回文案 | 「服务器机位IP分配已完成，已进入具体实施」 | 「服务器上下架具体实施已完成，已生成版本 V…」 |
| 系统动作 | 把 `server_data` 存入 `stage_data`，阶段推进 | 用 `server_data` 生成记录写入 **servers 表**，`status→approved`、`stage→completed`，并生成版本快照 |

- `action=reject`：Body `{"action":"reject","reason":"驳回原因"}`（两阶段均可驳回）。
- 第 2 次可省略 `server_data`（沿用第 1 次存的 `stage_data`），但显式传同样内容行为更确定。

**自动分配 IP**：`POST /api/rack-requests/auto-ip`，Body `{"所在机房":"...","所属机柜":"...","request_id":"..."}` → `{"ips":{管理ip,交易ip,带外ip}}`

### 2.5 服务器资料（附件）

资料按**序列号**归属：上下架请求、机位IP分配、具体实施、服务器信息表看到的是**同一份档案**（实施完成落库成服务器记录后自动继承，无需搬迁）。文件本体存放在 `data/archive/<序列号>/` 下，元数据在 SQLite `attachments` 表。

> **资料不进入任何 Excel/CSV 导出**（`/api/<entity>/export`、`/api/rack-requests/export`、模板均不含资料列）。
>
> 手动备份（`POST /api/backups/run`）只备份三张正式表 + `app.db` + `users.json`，**不含 `data/archive`**。

**列表**：`GET /api/attachments?serials=SN-1,SN-2`（或 `?serial=SN-1`；不带参数取全部可见）
```json
{"items":[{"id":"a1b2c3d4e5f6","serial_number":"SN-1","filename":"验收单.pdf","size":204800,
           "uploader":"biz1","uploaded_at":"2026-10-30 10:12:00","request_id":"req1",
           "source":"rack-request","can_manage":true}]}
```
- `source`：上传场景（`rack-request` / `allocation` / `implementation` / `server`），仅作留痕。
- `can_manage`：当前用户是否能重命名/删除该条。

**数量汇总**：`GET /api/attachments/summary` → `{"counts":{"SN-1":3}}`（只含当前用户可见的序列号）

**上传（可多个）**：`POST /api/attachments`，multipart
```
serial=SN-1            # 必填，按序列号归属
request_id=req1        # 选填，留痕用
source=rack-request    # 选填，留痕用
files=<file1> & files=<file2> ...   # 也接受 file 字段名
→ {"message":"已上传 2 个资料","items":[...]}
```
- 需要能力 `submit_rack_request` 或 `review_rack_requests`，且该序列号在当前用户可见范围内，否则 403。
- 单文件/单次请求超过 `app.ini` 的 `max_upload_mb`（默认 64MB）→ 413。
- 文件名会清洗（去路径、Windows 保留字符），落盘名为 `<附件id>_<原文件名>`。

**下载**：`GET /api/attachments/<attachment_id>/download` → 文件流（`Content-Disposition: attachment`，文件名取原始名）

**重命名**：`POST /api/attachments/<attachment_id>/rename`，Body `{"filename":"新名字.pdf"}`

**删除**：`POST /api/attachments/<attachment_id>/delete`（也接受 `DELETE`）→ 同时删除磁盘文件

改/删权限：管理人员、上传者本人、该序列号所属机房的机房人员。

### 2.6 其他

- 统计：`GET /api/statistics` → `{"summary":{cabinet_count,server_count,client_count}, "cabinets":[...], "rooms":[{"name":机房名,"cabinet_count","slot_total","slot_occupied","cabinets":[...]}]}`
  - `rooms` 为**按机房分组**的机柜列表，用于页面按机房分区展示；`cabinets` 为扁平列表（兼容旧用法）。
  - 每个机位：`{slot, status, servers:[...], occupied_by, pending_server?, pending_change_id?, pending_request_id?}`；`status` 取值 `free`/`occupied`/`pending_occupied`/`pending_release`/`pending_implementation`。
- 版本记录（管理员）：`GET /api/versions`、`GET /api/versions/<version_id>/<entity>/download`
- 设置（管理员）：`GET/PUT /api/settings`
- 系统用户（管理员）：`GET/PUT /api/system-users`
- 填写说明：`GET /api/filling-instructions`
- 健康检查（无需鉴权）：`GET /health`

### 2.7 字段标签（接入行情 / 接入柜台候选池）

`servers` 的 `接入行情` / `接入柜台` 是**多值字段**：存 `A, B` 文本（Excel 可读），写入时也接受数组，服务端去重后按 `, ` 拼接。候选标签池存在 SQLite 的 `field_options` 表（不进 Excel、不进版本快照）。

| 端点 | 权限 | 说明 |
|---|---|---|
| `GET /api/field-options?entity=servers` | 能看该实体即可 | 返回 `{entity, fields, options:{字段:[{value, source}]}}`；管理人员额外带 `usage:{official, pending}` |
| `POST /api/field-options` | 仅管理人员 | body `{entity, field, value}` 新增标签 |
| `PATCH /api/field-options` | 仅管理人员 | body `{entity, field, value, new_value}` 改名，**级联刷新正式记录与待审批 new_data**，返回 `{records, pending}` |
| `DELETE /api/field-options` | 仅管理人员 | body `{entity, field, value}` 删除；仍有记录 / 待审批在用时返回 `409`（只能改名） |

- 标签规则：单个标签 ≤ 40 字，不能包含 `,` `，` `、` `;`（它们是分隔符）；单字段候选池 ≤ 200 个；单条记录单字段 ≤ 20 个标签。导入时这些分隔符都认，重复标签自动去重。
- **自动收纳**：记录里出现、池里没有的值（直接改 Excel、历史导入、API 写入）会在下次读取候选池时自动进池，`source` 标记 `data`。
- `GET /api/skill/schema` 的 `entities.servers.multi_value_fields` 会列出这两个字段名，agent 不需要猜。

### 2.8 API Token（本人自助生成）

Token 不再需要手工写 `data/users.json`，也**不存在管理员代发**：每人自己在页面上生成，管理员看不到也改不了他人的 token。

| 端点 | 权限 | 说明 |
|---|---|---|
| `GET /api/my-token` | 登录即可 | 返回自己的 token：`{"api_token": "..."}`；未生成时为空串 |
| `POST /api/my-token/regenerate` | 登录即可（**必须是页面 session**） | 生成 / 重置自己的 token，返回 `{"api_token": "...", "message": "..."}` |

- 生成算法 `secrets.token_urlsafe(32)`：43 个字符，只含 `A-Z a-z 0-9 _ -`，可直接放进 header / URL / 配置文件。
- **一个用户只有一枚 token**：重新生成后旧 token 立即失效（原持有者下一次调用返回 401）。
- **换发只接受页面登录态**：带 `Authorization: Bearer <token>` 调用 `/api/my-token/regenerate` 返回 `403`（`{"error": "请先登录页面再生成 token，token 不能用于自我换发"}`）——避免一枚泄露的 token 把本人挤掉。
- 落盘到 `data/users.json` 的 `api_token` 字段并同步内存，**通过接口生成无需重启服务**。
- `GET/PUT /api/system-users`（管理员）**不下发 `api_token`**：响应里没有该字段，传进来的 `api_token` 也一律被忽略、保留磁盘原值——管理员改用户表既建不出也抹不掉别人的 token。其他身份访问该端点返回 403。
- 常见错误：`403` 用 token 换发 / 非管理员访问用户表；`404` 当前账号不在用户配置中；`401` 未登录。


### 2.9 skill 文档分发（免鉴权只读）

供 MCP server 与 agent 获取**当前实现**的字段与文档，避免手写副本过期。

| 端点 | 返回 |
|---|---|
| `GET /api/skill/manifest` | `{name, version, updated_at, files[], endpoints, counts}`；`version` = 「文档内容 + schema」sha256 前 12 位 |
| `GET /api/skill/docs?name=skill\|api-reference` | markdown 正文；带 `ETag` 与 `X-Skill-Version`，支持 `If-None-Match` → `304`；不带 `name` 返回 SKILL.md |
| `GET /api/skill/schema` | **运行时**从 `ENTITIES` / `FIELD_ALIASES` / `RACK_REQUEST_HEADERS` / `capabilities_for()` / `can_view_entity` 组装，零缓存 |

- **默认免鉴权**：只暴露文档与字段名，不含 token、用户列表、业务数据。`app.ini` 配了 `skill_docs_token`（或环境变量 `SM_SKILL_DOCS_TOKEN`）后要求请求头 `X-Skill-Token`，已登录用户仍放行。
- **给 `ENTITIES` 加一列 → 重启后端 → `schema` 立刻多出该列，`manifest.version` 随之变化**。MCP 与 skill 都不用改，这是「字段永不过期」的实现方式。
- 文档正文按 `docs/` → `server_management_skill/` → `server_management_skill/references/` 顺序探测，不维护第二份副本；文档名走白名单，`../` 之类一律 400/404。
- 与 2.6 的 `GET /api/skill/download`（需登录、返回 zip）并存：后者是给不支持 MCP 的 host 的兜底。
- 页面上的下载入口现在下载的是 **MCP 包**（见 2.10），不是 skill zip。

### 2.10 MCP 服务包下载（需登录）

| 端点 | 权限 | 说明 |
|---|---|---|
| `GET /api/mcp/download` | 登录即可 | **实时**打包 `app.ini` 的 `mcp_dir`（留空 = 项目根 `mcp_server/`），返回 `server_management_mcp.zip`，响应头带 `X-MCP-Version` |

- **不预生成包**：每次请求现场 zip，包内文件 = 当前磁盘代码，因此不会出现「客户端拿到的包里没有某个新工具」。改完 `mcp_server/` 不必记得重打包。
- 包内顶层目录固定 `server_management_mcp/`，解压后直接 `python server_management_mcp/install_config.py --host both --base-url ... --token ...`；`__pycache__` / `*.pyc` 已排除。
- `X-MCP-Version` = 包内文件「相对路径 + 内容」的 sha256 前 12 位，与 mtime 无关：同样的代码在任何机器上版本一致，可用来判断「是旧包还是旧进程」。
- `mcp_dir` 指错或目录不存在 → `404`，错误文案里会带实际查找路径。
- 页面入口：左下角钥匙 →「⬇ 下载 MCP」。

## 3. 身份与权限

权限由账号的「身份」决定。**token 登录与页面登录走同一套判定**：`current_user()`（`routes.py`）优先 session、其次 `Authorization: Bearer`，两条路径产出的用户字典结构一致（`用户id` / `身份` / `业务单位` / `所属机房`），此后所有判断都基于它。因此**同一枚 token 的权限 = 该身份用户在页面上的权限**。token 仅对 `/api/*` 生效，不会渲染页面。

三种身份：`管理人员`、`机房人员`、`业务人员`。权限由三层叠加：功能能力、实体读写、行级可见范围。

### 3.1 功能能力（capabilities）

| 身份 | capabilities |
|---|---|
| 管理人员 | `manage_all`、`review_operations`、`review_rack_requests`、`submit_rack_request`、`manage_settings`、`view_statistics` |
| 机房人员 | `review_rack_requests`、`view_statistics` |
| 业务人员 | `submit_rack_request` |

能力 → 接口约束（缺能力返回 403）：

| 能力 | 影响范围 |
|---|---|
| `review_rack_requests` | 上下架请求的查询 / 导出 / 审核 |
| `submit_rack_request` | `POST /api/rack-requests`（提交上下架） |
| `manage_settings` | `/api/settings`、`/api/system-users`（用户表不含 token，token 只能本人自助生成） |
| `view_statistics` | `GET /api/statistics` |
| `review_operations` | 无独立端点：操作变更审核由 `can_review_operation_change()` 判定，**仅管理人员** |
| `manage_all` | 前端通配符，管理人员天然全亮 |

版本记录等端点用 `@require_admin` 装饰器，**硬卡身份字符串**（`身份 != 管理人员` → 403），与 capabilities 无关。

### 3.2 实体读写（实体 × 身份）

| 实体 | 管理人员 | 机房人员 | 业务人员 |
|---|---|---|---|
| customers | 读 + 变更 | 不可见 | 读 + 变更 |
| cabinets | 读 + 变更 | 读 + 变更 | 不可见 |
| servers | 读 + 变更 | 读 + 变更 | 读 + 变更 |

「变更」= 可调用 add / update / delete 接口。**管理人员的变更免审批直接生效**；机房/业务人员仅提交申请，需管理人员审核通过才生效。

### 3.3 行级可见范围（`filter_records_for_user`）

| 身份 | 可见行 |
|---|---|
| 管理人员 | 全部 |
| 机房人员 | cabinets 按 `机房 == 所属机房`；servers 按 `所在机房 == 所属机房` |
| 业务人员 | customers 按 `业务单位 == 用户.业务单位`；servers 按 `归属单位 == 用户.业务单位` |

### 3.4 上下架请求可见性与审核

| 身份 | 可见（`scope=review`） | 可审核 |
|---|---|---|
| 管理人员 | 全部 | 全部机房 |
| 机房人员 | `所在机房 == 所属机房` | 仅本机房（`can_review_rack_request`） |
| 业务人员 | 仅自己提交的（`scope=submitted`） | 不可审核 |

### 3.5 容易踩的坑

1. **机房人员不能提交上下架请求**（无 `submit_rack_request`），只能审本机房的。
2. **业务人员完全无审核权限**（上下架、操作变更均不可），且只看得到自己提交的请求。
3. **机房人员看不到客户表**（`can_view_entity` 里 room 仅 cabinets/servers）。
4. **操作变更审核只有管理人员**：机房/业务人员能提交变更申请，但审不了，需管理人员 `approve`。**管理人员自己的变更免审批、直接生效**（仍留痕，可在 `GET /api/changes?status=approved` 查到）。
5. **手工**改 `data/users.json` 后必须重启服务——用户表在启动时载入内存，token 匹配走的是内存副本；用 `POST /api/my-token/regenerate` 生成则立即生效，无需重启。**注意**：`PUT /api/system-users` 会忽略传入的 `api_token` 并保留磁盘原值，想改 token 只能由本人走自助端点（或直接改文件后重启）。

实现位置：`app/core.py` 的 `capabilities_for()` / `can_view_entity()` / `can_mutate_entity()` / `filter_records_for_user()` / `rack_request_visible()` / `submitted_rack_request_visible()` / `can_review_rack_request()` / `can_review_operation_change()`；鉴权入口 `app/routes.py` 的 `current_user()` / `_token_user()` / `require_login` / `require_admin`。

## 4. 校验规则（提交前自查）

1. **主键非空**：servers=序列号、cabinets=机房+机柜、customers=客户id。
2. **servers 必填客户id**，且必须已在客户表登记，否则「客户id 不存在」；`客户名称`/`负责人` 由系统按客户id自动填充，可省略。
3. **servers 机位/机房/机柜约束**：`所在机房`+`所属机柜` 必须已登记在机柜表；`所属机位` 必须在机柜登记范围内且未被占用；机位格式 `数字-数字`（如 `9-10`）。
4. **IP 格式**：`管理ip`/`交易ip`/`带外ip` 必须有效 IPv4 或 CIDR。
5. **customers 客户id**：只能是数字。
6. **同主键冲突**：新增时主键已存在 → 报错；修改/删除时已有待审批申请 → 报「已有待审批申请」。
7. **多值标签**（`接入行情` / `接入柜台`）：单标签 ≤ 40 字，单字段 ≤ 20 个标签；`A、B` / `A,B` / 数组都会被归一成 `A, B`。标签候选池的增删改仅管理人员，见 2.7。

## 5. 错误码

| 状态码 | 含义 | 处理 |
|---|---|---|
| 401 | 未登录/token 无效 | 检查 Authorization 头 |
| 403 | 无权限 | 检查账号身份/能力（矩阵见第 3 节） |
| 400 | 校验失败 | 读 `error` 字段，按第 4 节自查 |
| 404 | 记录/资源不存在 | 检查主键/ID |
| 409 | 冲突 | 记录已变化、已有待审批申请、重复等 |

## 6. curl 示例

```bash
BASE="http://127.0.0.1:18888"
TOKEN="<你的token>"

# 查所有服务器
curl -H "Authorization: Bearer $TOKEN" "$BASE/api/servers"

# 按序列号检索
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"filters":[{"field":"序列号","pattern":"^SN-001$"}]}' \
  "$BASE/api/servers/search"

# 新增服务器。管理人员：直接生效；其他身份：仅提交申请（记下返回的 change_id）
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"序列号":"SN-999","品牌":"Dell","型号":"R740","客户id":"1001","所在机房":"机房A","所属机柜":"C1","所属机位":"9-10"}' \
  "$BASE/api/servers/add"

# 审核通过（仅非管理人员需要此步，让申请生效）
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"action":"approve"}' \
  "$BASE/api/changes/<change_id>/review"
```
