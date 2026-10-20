# antigravity-subagent

一个 [Claude Code](https://claude.com/claude-code) skill：把 Google Antigravity CLI（`agy`）当作 Claude Code 的子代理，以无头模式派发任务、回收结论，并用 PreToolUse hook 给它加上权限边界。

背景和实测数据见博客：[大模型上下文缓存（Prompt Cache）架构与实测表现分析报告](https://flyfishchen.github.io/blog/llm-prompt-cache-analysis/)。

## 能做什么

- **派发任务**：单轮 `run`、同一进程多轮 `session`、`--resume` 续接会话、`--schema` 结构化输出。
- **选模型和思考等级**：`--model` / `--effort`，或者用 `--profile` 预设（quick / standard / deep / iterate / review）。
- **汇总报告**：本次 token 用量、工具调用轨迹、被拦截的调用。
- **权限策略** `--policy none | readonly | workspace | full`（默认 `workspace`），由 `scripts/agy_gate.py` 通过 agy 的 PreToolUse hook 强制执行；`none` 禁用所有工具，用于材料已经给全的一次性任务。
- **并行子代理**：`batch` 并行派发多个独立任务；`--shared` 把同一份材料放在每个任务最前面，适合用不同问题从多个维度探索。
- **需确认的操作**：拒绝分三类——硬拒绝、待审批、交给主代理。待审批的操作经用户同意后，用 `--grant` 精确放行并续接会话；git commit/push 一律交回 Claude 执行。

## 前提

- agy（Antigravity CLI）≥ 1.4.2，已登录。
- Python ≥ 3.9。
- Claude Code。

## 安装

```bash
git clone https://github.com/flyfishchen/antigravity-subagent ~/.claude/skills/antigravity-subagent
~/.claude/skills/antigravity-subagent/scripts/agy_run.py install-hook
```

`install-hook` 会在 `~/.gemini/config/hooks.json` 里加一个名为 `cc-subagent-gate` 的条目，已有的其它 hook 不受影响。只有通过 `agy_run.py` 启动（设置了 `CC_AGY_POLICY`）时它才生效；你平时交互式使用 agy 不受影响。卸载：`agy_run.py uninstall-hook`。

## 用法

```bash
S=~/.claude/skills/antigravity-subagent/scripts/agy_run.py

$S run --policy readonly "审查 src/ 下的错误处理" --cwd /abs/project --profile deep
$S run "实现 X 并补测试" --cwd /abs/project --profile standard
$S run --policy workspace --grant 'write:/srv/share/report.md' --resume <conversation_id> --cwd /abs/project "已获批准，继续"
$S models
```

通常不需要自己敲这些命令：Claude Code 加载 skill 后会按 `SKILL.md` 调用。

## 实测要点（agy 1.4.2，Ubuntu）

- **工作目录**：不带 `--project` 时，shell 工具的工作目录会被钉在 `default-cli-project` 登记的文件夹，所以脚本默认加 `--project outside-of-project`。
- **消息长度**：agy 会把单条用户消息截在约 19 万字节，不报错；封装脚本超过 18 万字节就直接拒绝。
- **沙箱**：Ubuntu 默认的 AppArmor 策略会让 agy 的终端沙箱不可用。此时 `--sandbox` 静默无效，`--mode auto` 退回默认模式，所以权限只能靠 hook 控制。
- **hook**：
  - `~/.gemini/antigravity-cli/hooks.json` 会被加载，但里面的 hook 不执行；`~/.gemini/config/hooks.json` 和 `<workspace>/.agents/hooks.json` 会执行。
  - hook 输出为空或 `{}` 时按拒绝处理；hook 超时也按拒绝处理；hook 阻塞时 agy 会一直等待。

详见 `references/findings.md` 和 `references/permissions.md`。

## 限制

- **shell 过滤靠模式匹配**：挡得住误操作和越界，挡不住刻意构造的绕过。需要强隔离，请在容器里运行 agy。
- **agy 会绕路**：agy 被拒后常改用别的工具绕过，所以要对照报告里的工具轨迹核实。
- **授权必须精确**：`cmd:` 授权要求命令完全一致，agy 换了写法就会再次被拒。

## 测试

```bash
python3 -I scripts/test_gate.py
```
