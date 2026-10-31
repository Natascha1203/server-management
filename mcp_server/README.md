# server-management MCP server

把「服务器资产管理系统」暴露成 MCP 工具，让 agent 能查数据、读实时字段结构。

**同一份代码同时适用于 Claude Code CLI 与 WorkBuddy 桌面/CLI** —— server 侧只实现
`tools`（各家支持度最一致的能力），不依赖 `resources` / `prompts` /
`InitializeResult.instructions` 这些实现差异较大的部分。

## 零依赖

只用 Python 标准库，不需要 `pip install`。任何 Python 3.7+ 都能跑，CentOS 7 自带
python3、conda 环境、Windows 官方安装包都可以。

## 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `SM_BASE_URL` | 是 | 后端地址，如 `https://sm.example.com` 或 `http://127.0.0.1:18888` |
| `SM_TOKEN` | 业务调用必填 | 账号的 API token（页面左下角 🔑 →「获取 Token」自己生成）。**读文档不需要它，任何查询/写入都需要。** |
| `SM_TIMEOUT` | 否 | 单次请求超时秒数，默认 20 |
| `SM_INSECURE` | 否 | 设 `1` 时跳过 HTTPS 证书校验（自签证书场景） |

一个进程 = 一枚 token = 一个固定身份。权限完全等于该账号：管理人员 token 的写操作
会直接生效，业务/机房人员 token 的写操作进入待审批。**不支持**在参数里声明身份。

## 从系统页面下载

页面左下角钥匙按钮 →「⬇ 下载 MCP」→ 得到 `server_management_mcp.zip`。

这个包由后端**实时从 `mcp_server/` 目录打包**，所以永远等于当前线上代码（不会出现
「下载的包里没有某个新工具」），响应头 `X-MCP-Version` 是包内文件内容的 sha256 前
12 位，可用来核对两边是不是同一份代码。解压后顶层目录是 `server_management_mcp/`：

```bash
python server_management_mcp/install_config.py --host both \
  --base-url http://127.0.0.1:18888 --token <你的 token>
```

从源码仓库直接跑的话，把路径换成仓库里的 `mcp_server/`，命令一字不变。

## 启动方式

```bash
# 用绝对路径启动（推荐，host 配置最省事）
SM_BASE_URL=http://127.0.0.1:18888 SM_TOKEN=xxx python /path/to/mcp_server/run_server.py

# 或在 mcp_server/ 目录下
SM_BASE_URL=... SM_TOKEN=... python -m sm_mcp
```

## 一条命令注册（推荐）

`install_config.py` 会读-合并-原子写入，**先备份**原文件，不动其他 mcpServers：

```bash
# 两个 host 一起配（也可以 --host claude / --host workbuddy）
python install_config.py --host both \
  --base-url http://127.0.0.1:18888 \
  --token <你的 token>

# 先看看要写什么，不落盘
python install_config.py --host both --base-url ... --token ... --dry-run
```

它会把 `command` 填成当前 python 的绝对路径、`args` 填成 `run_server.py` 的绝对路径，
所以不用手改路径。Windows 下 `--python` 可指定别的解释器。

手写配置见下面两节。

## 配置：Claude Code CLI

```bash
claude mcp add server-management \
  --env SM_BASE_URL=http://127.0.0.1:18888 \
  --env SM_TOKEN=xxxxx \
  -- python /path/to/mcp_server/run_server.py
```

等价的手写配置（`~/.claude.json` 顶层 `mcpServers`，或项目级 `.mcp.json`）：

```json
{
  "mcpServers": {
    "server-management": {
      "type": "stdio",
      "command": "/usr/bin/python3",
      "args": ["/path/to/mcp_server/run_server.py"],
      "env": {
        "SM_BASE_URL": "http://127.0.0.1:18888",
        "SM_TOKEN": "xxxxx"
      }
    }
  }
}
```

Windows 上把 `command` 换成 python 的绝对路径（如
`C:\\Users\\admin\\.workbuddy\\binaries\\python\\versions\\3.13.12\\python.exe`），
`args` 用正斜杠路径。

## 配置：WorkBuddy 桌面 / CLI

写在 `~/.workbuddy/mcp.json`（注意：**不是** `~/.workbuddy/connectors/default/mcp.json`，
那是内置连接器市场的配置）：

```json
{
  "mcpServers": {
    "server-management": {
      "command": "C:/Users/admin/.workbuddy/binaries/python/versions/3.13.12/python.exe",
      "args": ["D:/project/ServerManagement/server-management-v1/mcp_server/run_server.py"],
      "env": {
        "SM_BASE_URL": "http://127.0.0.1:18888",
        "SM_TOKEN": "xxxxx"
      }
    }
  }
}
```

写完后**不会自动生效**：打开连接器管理页右上角的「自定义连接器」，对新条目点
**信任**，然后新开一个会话。

## 工具

| 工具 | 作用 |
|---|---|
| `sm_bootstrap` | **每次会话先调**。返回当前 skill 正文 + 版本 + 实时字段清单 + 你的身份 |
| `sm_whoami` | 身份与 capabilities（403 时先看这个，别盲重试） |
| `sm_schema` | 字段/别名/权限矩阵/上下架阶段（可只取某个实体） |
| `sm_query` | 查记录，`filters` 为包含匹配，受后端行级权限裁剪 |
| `sm_stats` | 机位与服务器统计 |
| `sm_rack_list` | 上下架请求摘要列表 |
| `sm_rack_get` | 单个请求的完整内容（`request_data` 已解开成对象） |
| `sm_upsert` | 新增 / 修改记录；改记录只给要改的字段，工具自动合并原值。`接入行情`/`接入柜台` 可传数组，服务端去重成 `A, B` |
| `sm_delete` | 删记录，**必须同时给 `keys` 与内容完全相同的 `confirm_keys`**，否则拒绝执行 |
| `sm_pending` | 待审批变更申请（拿 `change_id`）；非管理人员降级为 pending 数据视图 |
| `sm_review` | 审批变更申请（`approve` / `reject`），仅管理人员 |
| `sm_rack_submit` | 提交上下架请求 |
| `sm_rack_review` | 审核上下架请求；`stage` 必须与真实阶段一致，否则中止且无副作用 |

写工具的返回值固定带 `applied` / `pending` / `change_id`：`applied:true` 表示已直接落库，
`pending:true` 表示只是提交了待审批申请，**不要靠 `message` 的中文文案判断**。

`tools/list` 会按身份裁剪：业务人员看不到 `sm_review` / `sm_pending`，机房人员看不到
`sm_review` / `sm_pending` 但保留 `sm_rack_review`。身份在第一次 `tools/list` 时懒加载
`/api/me` 并缓存（`initialize` 不发网络请求）；拿不到身份时返回全量工具，由后端 403 兜底。

## 「自动获取最新 skill」是怎么做到的

`sm_bootstrap` 每次都从后端拉：

- `GET /api/skill/manifest` → 版本号
- `GET /api/skill/docs?name=skill` → SKILL.md 正文
- `GET /api/skill/schema` → **由 `app/core.py` 的 `ENTITIES` 运行时组装**，零缓存

所以给 `servers` 加一列，只要重启后端，agent 下一次 `sm_bootstrap` 就能看到新字段，
MCP 和 skill 文件都不用改。这也意味着**装了这个 MCP 的 host 可以不装 skill**——
工具描述里已经写明先调 `sm_bootstrap`。

## 排查

| 现象 | 原因 |
|---|---|
| host 里看不到这些工具 | WorkBuddy 未点「信任」；或配置写到了 `connectors/default/mcp.json` |
| 工具返回「后端不可达」 | `SM_BASE_URL` 不对，或 host 所在机器连不上后端 |
| 工具返回「token 无效或已失效」 | token 被重新生成过；去页面重新拿一枚并更新 `SM_TOKEN` |
| 工具返回「当前身份无此能力」 | 该账号确实没有这个权限，换账号的 token；重试无用 |
| 握手失败 | 检查 `command` 路径是否可执行；server 的日志只走 stderr，stdout 不会有杂音 |

自检（不经过 host，直接看 server 是否能起来）：

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | SM_BASE_URL=http://127.0.0.1:18888 python run_server.py
```

应输出一行包含 13 个工具名的 JSON。
