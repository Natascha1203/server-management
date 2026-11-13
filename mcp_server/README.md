# server-management MCP server

把「服务器资产管理系统」暴露成 MCP 工具，让 agent 能查资产、读实时字段结构。

零依赖：只用 Python 标准库，Python 3.7+ 都能跑，不需要 `pip install`。

**下载的包里不含任何配置**。装之前先备好两个值：

| 值 | 从哪拿 |
|---|---|
| 后端地址 | 系统的访问地址。系统就在本机 → `http://127.0.0.1:18888`；系统在别的机器上 → 写它实际可达的地址（内网 IP 或域名）。**填错的表现是「后端不可达」** |
| token | 页面左下角 🔑 →「获取 Token」。一个进程 = 一枚 token = 一个身份：管理员 token 的写操作直接生效，业务/机房人员 token 的写操作进入待审批 |

## 下载

页面左下角 🔑 →「⬇ 下载 MCP」→ 得到 `server_management_mcp.zip`，解压到固定位置
（以后升级直接覆盖同名目录，配置不用改）。包由后端实时打包，永远等于当前线上代码。

下面三节按你用的 host 挑一节看。

## 1. WorkBuddy 桌面版

**不用运行任何命令**，全程点界面。

1. 左侧边栏「专家 · 技能 · 连接器」→「连接器」→ 右上角「自定义连接器」
2. 粘入下面「通用片段」，把两个占位符换成上表的值，保存
3. 回到连接器列表，对 `server-management` 点**信任**
4. 新开一个会话，说「列出 server-management 下所有可用的工具」，出现一批 `sm_*` 就通了
   （管理人员 token 是 13 个；业务/机房人员会少几个，见文末「连不上时」）

## 2. CodeBuddy

**不用运行任何命令**。

1. 打开 CodeBuddy Settings → **MCP** 标签页
2. 点右侧「Add MCP」，粘入下面「通用片段」，保存
3. 在 `server-management` 右侧点「Try to Run」验证（状态变绿、工具列出即成功）

也可以直接改文件：用户级写在 `~/.codebuddy/.mcp.json`，项目级写在项目根目录 `.mcp.json`。

## 3. Claude Code CLI

**运行这一条命令**（路径、后端地址、token 都换成你的实际值）：

```bash
claude mcp add server-management \
  --env SM_BASE_URL=http://127.0.0.1:18888 \
  --env SM_TOKEN=<你的 token> \
  -- python /path/to/server_management_mcp/run_server.py
```

`Added ...` 只代表配置写进去了，不代表连上了：用 `claude mcp list` 看是否 `✔ Connected`，
会话里用 `/mcp` 看工具数量。

默认写进当前项目的 local scope，换个目录跑 `claude` 就没有它了。想全局可用，命令末尾加
`--scope user`（或第一次就带着跑）。

首次调用会请求权限确认。想给只读工具放行，把工具全名加进 `~/.claude/settings.json`：

```json
{
  "permissions": {
    "allow": ["mcp__server-management__sm_query", "mcp__server-management__sm_schema"]
  }
}
```

`sm_upsert` / `sm_delete` / `sm_review` 建议别加，每次确认一次比事后回滚便宜。

### 对话里怎么用

注册完就回对话里用，不用再碰命令行。工具全名形如
`mcp__server-management__sm_query`（`mcp__<服务名>__<工具名>`），**你不需要手写**——
用中文说需求，模型自己挑工具；只有配白名单时要写全名。

第一句建议先验通道：「用 sm_whoami 看我的身份」。返回 `"身份":"管理人员"` 说明地址和
token 都对上了。接着让它 bootstrap 一次，拿到实时字段清单、校验规则和你的身份——
字段名以它返回的为准，不要让模型凭记忆拼列名。

常用说法：

| 你想干什么 | 直接这么说 | 实际调用 |
|---|---|---|
| 拿字段和身份 | 「先 bootstrap 一下」 | `sm_bootstrap` |
| 查某台机器 | 「查 2026621SH8703 的完整记录」 | `sm_query(entity="servers", filters={"序列号":"2026621SH8703"})` |
| 看某柜占用 | 「王港机房 D10 现在占了哪些机位」 | `sm_query(entity="cabinets", filters={"机房":"王港机房","机柜":"D10"})` |
| 看还剩多少位置 | 「各机房的机位空余统计」 | `sm_stats` |
| 改某个字段 | 「把 2026621SH8703 的 CPU 改成 X」 | `sm_upsert(entity="servers", old_key="2026621SH8703", row={"CPU型号":"X"})` |
| 走上下架 | 「这台上架，客户 00000001，D10 18-19」 | `sm_rack_submit` |
| 处理待审批 | 「列出待审批的变更」 | `sm_pending`（仅管理人员可见） |

几个必须知道的点：

- **写操作看返回的 `applied` / `pending`，别看那句中文文案**。`applied=true` 才是真落库；
  `pending=true` 只是提交了申请（带 `change_id`），还得管理人员 `sm_review` 通过才生效。
  管理人员 token 的写操作直接生效。
- **改记录只说改哪一项**。`sm_upsert` 给 `old_key` + 要改的字段即可，工具会取回原记录合并，
  不需要回传整行。
- **删除要二次确认**：`sm_delete` 必须同时给 `keys` 和内容完全一致的 `confirm_keys`，
  否则一条都不删。这是防手滑的硬校验，模型绕不过去。删之前先 `sm_query` 确认是这几条。
- **被 403 拒了不要重试**，先 `sm_whoami` 看身份。工具列表本身也按身份裁剪：业务/机房人员
  看不到 `sm_pending`、`sm_review`、`sm_rack_review`，所以「工具少了」通常不是配置问题。
- **权限确认**：首次调某个工具会弹一次确认。非交互跑（`claude -p`）没有弹窗，必须事先
  在 settings.json 里放行，否则调用直接被拒。

## 通用片段

WorkBuddy 和 CodeBuddy 粘的都是这一份，只改两个占位符：

```json
{
  "mcpServers": {
    "server-management": {
      "type": "stdio",
      "command": "<python 绝对路径>",
      "args": ["<解压目录>/server_management_mcp/run_server.py"],
      "env": {
        "SM_BASE_URL": "<后端地址>",
        "SM_TOKEN": "<你的 token>"
      }
    }
  }
}
```

- `command` 用哪个 python 都行（零依赖，3.7+ 即可），但**必须写绝对路径**——host 不保证
  工作目录和 PATH。Windows 路径用正斜杠 `C:/...` 或双反斜杠 `C:\\...`。
- `args` 指向解压出来的 `run_server.py`，同样写绝对路径。

不想手拼 JSON，可以用脚本写配置（会先备份、原子写入）：

```bash
python server_management_mcp/install_config.py --host workbuddy \
  --base-url http://127.0.0.1:18888 --token <你的 token>
```

`--host` 可选 `claude` / `workbuddy` / `codebuddy` / `both`（前后两个）/ `all`（三个都写）；
加 `--dry-run` 只预览不落盘。注意它**整体替换** `server-management` 这一条，手改过的字段
（比如 `disabled`）会丢，所以配置已经调好时直接改文件、别重跑。

## 连不上时看这里

| 现象 | 处理 |
|---|---|
| 「后端不可达」，报错里的地址是 `127.0.0.1` | `SM_BASE_URL` 没填或填错，落到了兜底默认值；补上真实地址 |
| 改了配置但行为没变 | host 拉起的 MCP 是常驻子进程，必须重连 / 新开会话 / 重启 host |
| 工具比预期少 | 身份裁剪：业务/机房人员本来就没有 `sm_review`、`sm_pending` |
| 「token 无效或已失效」 | 去页面重新获取一枚，更新后重连 |
| 握手失败 | `command` 路径不可执行，或 python 解释器不存在 |

绕过 host 直接确认进程本身是好的（会打印工具列表）：

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | SM_BASE_URL=<后端地址> SM_TOKEN=<token> python run_server.py
```

正常输出一行 JSON，里面是全部工具名（管理人员 token 为 13 个）。
