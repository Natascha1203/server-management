---
name: server-management
description: 操作「服务器资产管理系统」（server-management-v1）的 API 手册。当需要查询、新增、修改或删除服务器/机柜/客户数据，提交或审核上下架请求，上传/查看/改删服务器资料（附件），查询统计与版本记录，或理解该系统的审批流机制时使用此技能。触发场景包括：查某台/某机房服务器、批量导入导出资产、提交上架/下架请求、审核变更申请、查待审批项、给服务器上传或下载资料。
agent_created: true
---

# 服务器资产管理系统 · 操作手册

通过 HTTP API 操作「服务器资产管理系统」（Flask + SQLite，机柜/服务器/客户资产管理，两阶段审批流）。

## 核心要点

0. **MCP 优先**：如果当前会话装了 `server-management` MCP 工具，**先调 `sm_bootstrap`**——它会从后端实时拉取本文件的当前版本、版本号、字段清单和你的身份，比本地磁盘上的副本更可信。字段名一律以 `sm_schema` / `sm_bootstrap` 返回为准，不要凭记忆拼列名。本文件与 `references/api-reference.md` 是**没装 MCP 时的兜底**，可能落后于实现。
1. **鉴权**：所有 `/api/*` 接口需 `Authorization: Bearer <token>` 请求头，**每个账号一枚 token**；未带/错误返回 401。token **不需要手工配置，也不由管理员代发**：本人登录页面后点左下角钥匙图标 →「获取 Token」，即可一键生成 / 查看 / 重新生成，接口侧对应 `GET /api/my-token`、`POST /api/my-token/regenerate`（后者**只接受页面登录态**，用 token 自己换发会 403）。管理员看不到也改不了他人的 token（`/api/system-users` 不下发 `api_token`）。**重新生成会让旧 token 立即失效**。**token 登录与页面登录共用同一套权限判定**——能做什么完全取决于该账号的「身份」（见第 4 条）。
2. **审批流（最关键）**：非管理人员的变更走「申请 → 审核 → 批准」——新增/修改/删除接口成功仅代表「申请已提交」（返回 `change_id`），要让数据真正生效，需再调审核接口 `approve` 该 `change_id`。**管理人员（admin）例外：变更免审批、调用即直接生效**（返回 message 含「直接生效」，无需再审核，但仍生成版本记录与审计留痕）。
3. **记录状态**：`record_state=official` 是正式数据（默认）；`pending` 是含待审批变更的预览视图。
4. **身份与权限**：三种身份（`管理人员` / `机房人员` / `业务人员`）权限不同，token 同样受此约束。要点：**仅管理人员**能审核操作变更、访问设置与版本记录；**机房人员**只能看/审本机房数据，且不能提交上下架请求；**业务人员**只能提交（上下架 + 客户/服务器变更）、不能审核，且只看得到自己提交的请求。完整矩阵 → `references/api-reference.md` 第 3 节。

## 数据模型速览

| 实体 | URL 段 | 主键 |
|---|---|---|
| 服务器 | `servers` | `序列号` |
| 机柜 | `cabinets` | `机房`+`机柜` |
| 客户 | `customers` | `客户id` |

完整字段清单、字段别名、全部接口参数与返回结构、校验规则、错误码、curl 示例 → 见 `references/api-reference.md`。

> `servers` 的 `用途分类` / `接入行情` / `接入柜台` 是**多值标签字段**：存 `A, B` 文本，写入时也接受数组，每个字段有独立候选池。候选池见 `GET /api/field-options?entity=servers`，增删改仅管理人员（API 参考 2.8 节）。

## 常见任务 → 操作步骤

1. **查某台服务器**：`POST /api/servers/search`，`filters` 用 `{"field":"序列号","pattern":"^SN-XXX$"}`（正则）。
2. **查某机房所有服务器**：search，`filters` 用 `所在机房`。
3. **新增服务器**：`POST /api/servers/add`（管理人员直接生效）→ 其他身份记下 `change_id` → `POST /api/changes/<id>/review`（`{"action":"approve"}`）。
4. **改服务器某字段**：`POST /api/servers/update`（Body 带 `old` 定位 + `new` 新值）→ 管理人员直接生效；其他身份再审核。
5. **删除服务器**：`POST /api/servers/delete` → 管理人员直接生效；其他身份再审核。
6. **批量导入**：`POST /api/servers/import`（multipart 文件，表头必须 27 列全，末列 `备注`）→ 管理人员直接生效；其他身份再审核。
7. **查待审批申请**：`GET /api/changes?status=pending`。
8. **提交上下架 / 变更请求**：`POST /api/rack-requests`，`请求类型` 取 `上架` / `下架` / `变更`。上架要求序列号不存在（走 机位IP分配 → 具体实施）；下架 / 变更要求序列号已存在，**直接进具体实施**（下架完成后服务器进回收站，变更完成后直接写正式表）。
9. **审核上架（分配机位IP）**：`GET /api/rack-requests?stage=allocation` → `POST /api/rack-requests/<id>/review`（`approve` + `server_data`）。下架 / 变更只需一次 `approve`（stage 已是 `implementation`），变更的 `server_data` 会以现有记录为底合并。
10. **交换机位**：`POST /api/servers/swap-slot`（Body 带 `old` / `new` / `target`）→ 我方服务器与目标机位占用者**互换位置**（占用者可为已实施/待审核/待实施三种载体）。对方的机位范围**以服务端判定为准**：`target.slot` 只需给被点击的单个机位，服务端会取出该占用者的完整机位范围（可能多行）整段对调，`new.所属机位` 会被覆盖。管理人员直接生效；其他身份提交后需 `review` 批准，两侧同时生效。
11. **上传 / 查看 / 改删服务器资料（附件）**：`POST /api/attachments`（multipart，`serial` + 多个 `files`）→ `GET /api/attachments?serial=SN-1` 列表 → `GET /api/attachments/<id>/download` / `POST /api/attachments/<id>/rename` / `POST /api/attachments/<id>/delete`。资料按**序列号**归属，上下架请求、机位IP分配、具体实施、服务器表共用同一份；文件落在 `data/archive/<序列号>/`，**不进入任何 Excel 导出**。详见 `references/api-reference.md` 2.5 节。
12. **维护用途分类 / 接入行情 / 接入柜台（多值标签）**：写服务器时这些字段可以传数组（如 `["CTP行情","Level-2"]`）或 `A, B` 文本，服务端会去重并按 `, ` 归一。读候选池 `GET /api/field-options?entity=servers`；**只有管理人员**能 `POST/PATCH/DELETE /api/field-options` 增删改。删除仍被记录或待审批引用的标签返回 `409`（只能改名，改名会级联刷新正式记录与待审批 `new_data`）。
13. **回收站（仅管理人员）**：`GET /api/trash` 列已下架服务器快照 → `POST /api/trash/<id>/restore`（body 可带 `server_data` 改客户/机位）重新上架 → `DELETE /api/trash/<id>` 永久删除（连带清理该序列号的资料附件）。恢复会走完整服务器校验，冲突（客户未登记 / 机位被占 / 序列号已存在）返回 400 且保留回收站记录。详见 API 参考 2.6 节。

## 操作原则

- 涉及写操作（add/update/delete/review）前，先确认用户意图，避免误操作。
- 写操作默认走审批流：**管理人员调用即直接生效**；其他身份需显式补一次 `approve` 审核调用才能落地。
- 动手前先确认**当前 token 对应哪个身份**——机房人员不能提交上下架、业务人员不能审核，身份不对会直接 403（见 `references/api-reference.md` 第 3 节）。
- 提交前按 `references/api-reference.md` 第 4 节校验规则自查，避免 400（尤其 servers 的客户id 必填且需预先登记、机位/机房约束、IP 格式）。
