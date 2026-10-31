"""server-management 的 MCP server（stdio）。

零第三方依赖：只用 Python 标准库。这样 CentOS 7 + 任意 conda 环境都能直接
``python -m sm_mcp`` 起来，不需要 pip install 任何东西。

host 无关：只实现 tools 能力，不依赖 resources / prompts / InitializeResult.instructions
这些各家支持度不一致的部分；「拿最新 skill」由 ``sm_bootstrap`` 这个普通工具完成，
所以 Claude Code CLI 与 WorkBuddy 桌面/CLI 用的是同一份代码、同一套行为。
"""
__version__ = "1.0.0"
