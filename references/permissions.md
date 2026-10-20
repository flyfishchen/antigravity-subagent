# agy 子代理权限模型（2026-10-20 实测，agy 1.4.2）

标注：✅ 实测验证；📄 来自 agy 二进制内嵌文档/changelog，未实测。

## 测试环境现状（Ubuntu，AppArmor 默认限制非特权 user namespace）

- ✅ CLI 全局 `~/.gemini/antigravity-cli/settings.json`：`toolPermission: always-proceed`；`trustedWorkspaces` 含 `/`（所有目录都受信任，工作区 hooks 都会加载）。
- ✅ 在此配置下，无头 `-p` 运行可以：在工作区外用写文件工具写入、shell 写 `$HOME`、访问外网（curl 200）。即**没有任何限制**。
- ✅ 终端沙箱不可用：日志 `Terminal sandbox unavailable: AppArmor restricts unprivileged user namespaces (kernel.apparmor_restrict_unprivileged_userns = 1)`。
  - `--sandbox`：**静默无效**，没有任何警告。
  - `--mode auto`：stderr 警告 "not available because the terminal sandbox is not available on this host"，退回默认模式。
  - `proceed-in-sandbox` 策略同样依赖沙箱。
- ✅ `--mode accept-edits` 在 always-proceed 下与默认无差别。`--mode plan` 只产出计划文件、不改工作区（但不确定是硬限制）。
- 全局 `~/.gemini/config/hooks.json` 里有 orca 安装的 hook，PreToolUse 对所有工具返回 `{"decision":"ask"}`；always-proceed 下 `ask` 等于放行。

## 无头模式的审批语义

- 📄 需要人工确认的工具调用在 `-p` 下会被 **soft-deny**（不挂起），stderr 打印 "...required approval that headless mode cannot prompt for, so ... auto-denied. Add an allow-rule under permissions.allow in settings.json (e.g. command(...))"。
- 📄 `--dangerously-skip-permissions`：所有工具自动批准。
- 📄 规则语法：`command(cat)`、`write_file(<path>)`、`read_file(<path>)`、`read_url(...)`、`execute_url(...)`、`mcp(<server>/<tool>)`；官方建议不要用 `command(*)` 这类通配。
- 📄 策略取值：toolPermission = `always-proceed | request-review | strict | proceed-in-sandbox`；非工作区文件访问 / 网络访问 = `allow | ask | deny`；项目级设置可覆盖全局。

## Hooks（最可靠的控制点）

- ✅ 工作区 `<cwd>/.agents/hooks.json` 在 `-p` 下加载；hook 进程继承 agy 的环境变量（可以用环境变量开关策略）。
- ✅ `PreToolUse` 返回 `{"decision":"deny","reason":"..."}`：硬拦截，模型能看到 reason。但它会**改用别的工具绕过**（shell 被拒就改用 read_file）。
- ✅ `ask` / `force_ask` 在 always-proceed 下直接放行。
- ✅ 被 deny 的调用**不会**出现在 stream-json 的 tool 步骤里，只能靠 hook 自己记日志。
- ✅ stdin 载荷（camelCase）：`toolCall.name`、`toolCall.args`（如 `CommandLine`、`Path`）、`workspacePaths`、`conversationId`、`stepIdx`、`modelName`、`transcriptPath`、`artifactDirectoryPath`。
- hooks.json 的位置：
  - ✅ `~/.gemini/config/hooks.json`：会执行。gate 装在这里。
  - ✅ `<workspace>/.agents/hooks.json`：会执行。
  - ✅ `~/.gemini/antigravity-cli/hooks.json`：日志显示已加载，但**不会执行**。
  - 📄 插件的 `hooks/hooks.json`。
  - 多个同事件 hook 依次执行，任一 deny 即拒绝。
- ✅ hook 输出 `{}` 或空输出会被当作 **deny**。中立应返回 `{"decision":"ask"}`，在 always-proceed 下等于放行。
- 📄 还支持 `overwrite`（改写参数）、`permissionOverrides`，以及 `Stop` / `PreInvocation` / `PostInvocation` / `SessionStart` 事件。
- hook 的 cwd 是 hooks.json 所在目录；超时默认 30 秒。

## 高风险工具（init 事件里的 60 个工具中）

- `run_command`、`send_command_input`：任意 shell。
- `write_to_file`、`write_file`、`replace_file_content`、`multi_replace_file_content`、`sed_file`、`notebook_edit`：写文件。
- `schedule`：创建定时/周期任务，会持久化到会话之外。
- `define_subagent`、`invoke_subagent`、`browser_subagent`、`manage_subagents`：衍生子代理，token 成本翻倍。
- `call_mcp_tool`：本机配置了 chrome-devtools、puppeteer。
- `browser_*`、`open_browser_url`、`execute_browser_javascript`：浏览器。
- `send_message`、`manage_inbox`：消息。
- `delete_knowledge`：删除知识库。

## 已实现：cc-subagent-gate

- `scripts/agy_gate.py` + `~/.gemini/config/hooks.json` 中的 `cc-subagent-gate`。只有 `CC_AGY_POLICY` 非空时才进入 Python 检查；否则用 sh 快速返回 ask。
- ✅ 端到端（2026-10-20，不告诉模型策略，让它真的尝试）：
  - workspace：工作区内写入、`ls` 放行；工作区外写入、git commit、`>> ~/.bashrc_*`、schedule 被拒。
  - readonly：`git status && ls`、view_file 放行；写文件、`python3` 被拒。
  - 未设置策略的普通 agy：写文件照常成功。
- 单元测试：`python3 -I scripts/test_gate.py`，54 个用例（含分级和授权），改动 gate 后必须跑。
- 拒绝分级：hard / approval / handoff。日志条目带 `kind` 和 `grant`；授权通过 `CC_AGY_GRANTS`（JSON 列表）传入。
- ✅ hook 阻塞 40 秒（timeout 120）期间 agy 一直等待，之后照常执行 → 实时审批可行（未实现，当前选择延后审批）。
- ✅ hook 超时（timeout 5、sleep 20）：agy 杀掉 hook 并拒绝执行（失败即拒绝）。
- ✅ 端到端：
  - 越界写入被拒 → 报告给出 `write:` 授权和 `resume:` 命令 → 带 `--grant` 续接同一会话 → 写入成功。
  - git commit 归类为交给主代理。
  - 模型读了前言后会主动跳过越界操作，此时只出现在回复的「待审批」小节。
