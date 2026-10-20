---
name: antigravity-subagent
description: 把 Google Antigravity CLI（agy）当作子代理，以无头模式派发任务并回收结论。用于：用户要求"用 antigravity / agy / Gemini 去做…"、需要第二意见或交叉审查、需要 Gemini 的超长上下文（>200k，读整个大仓库/大日志/大文档）、需要 agy 自带的网页搜索/浏览器/生图能力、或想把可并行的独立子任务分给另一个代理并行跑。
---

# Antigravity 作为子代理

agy（Antigravity CLI，本机 `~/.local/bin/agy`，v1.4.x）本身是完整的编码代理：自带约 60 个工具（读写文件、shell、grep、浏览器、网页搜索、生图、MCP、它自己的子代理），能接 Gemini 和 Claude 两类模型。你（Claude Code）负责拆任务、写提示、验收；agy 负责执行。

## 运作模型（实测）

- **一次调用 = 一个 agy 进程 = 一个 conversation**。`-p/--print` 无头运行一轮（内部可多步工具调用），结束即退出。会话持久化在 `~/.gemini/antigravity-cli/`，用 `--conversation <id>` 续接，`--continue` 续接最近一次。
- **输出**：`--output-format json` 给最终结果；`stream-json` 给 NDJSON 事件流：`init`（模型、工具列表、权限模式）→ `step_update`（每步 `step_type`=user_input/agent_response/tool，含 `tool_name`、参数、**本步** usage）→ `result`（`status`、`response`、`structured_output`、**整个会话累计** usage）。
- **多轮常驻**：`--input-format stream-json` 从 stdin 每行读一条 `{"event":"user","message":{"role":"user","content":"..."}}`，每条跑一轮，同一会话、同一进程，每轮都发一个 `result`。注意必须写成 `--print=`（`-p` 会吞掉下一个参数当提示词）。
- **结构化输出**：`--json-schema '<schema或文件>'` → `result.structured_output`。
- **模式**：默认按 `~/.gemini/antigravity-cli/settings.json`（本机 `toolPermission: always-proceed`，即全部自动批准）；`--mode plan` 只写计划到 `~/.gemini/antigravity-cli/brain/<id>/*.md`、不改文件。本机终端沙箱不可用，`--sandbox` 静默无效、`--mode auto` 退回默认，**不要靠它们隔离**，权限由下文的 `--policy` 控制。无头模式下子代理的提问会被跳过，不会卡住。
- **工作目录坑**：不带 `--project` 时 agy 使用 `default-cli-project`，其 shell 工具的 cwd 被钉在该项目登记的文件夹；若该文件夹已不存在（作者环境即如此），`run_command` 全部失败，agy 还会悄悄改用 read_file 绕过，看起来像成功。必须用 `--project outside-of-project`，shell 才会用进程 cwd。封装脚本已默认处理。
- **自身开销**：每次请求固定前缀约 7.4k（Gemini）/ 11.5k（Claude）token（系统提示+工具定义）；进程启动+鉴权约 3–10s。简单任务 agy 也可能自己再开子代理，token 翻几倍。

## 调用原则

1. **同一任务的多轮追问放进一个进程**（`session` 子命令 / stream-json 输入），不要分成多次间隔很久的 `--conversation` 续接。
2. **Gemini 适合大输入、大输出、一次完成的任务，以及同时拉起大量子代理。** 少用或不用多轮工具调用：agy 里每一步工具调用都是一次独立请求，Argon 每次首 token 要 4–16 秒，工具链越长等得越久。需要反复迭代、多轮小改的任务交给 agy 里的 Claude 模型，或者干脆自己做。

## 怎么调用

统一用封装脚本（它会：加 `--project outside-of-project`、启用权限策略、在提示前加"工作目录/策略/无头/列出改动文件"前言、保存原始事件日志、汇总本次 token 用量、列出工具调用轨迹和被拦截的调用）。**不要绕过脚本直接调 `agy -p`**，那样没有权限限制。

```bash
S=~/.claude/skills/antigravity-subagent/scripts/agy_run.py

# 只读调研 / 审查
$S run --policy readonly "任务描述" --cwd /abs/project

# 单轮任务（按预设选模型+思考等级）
$S run "任务描述" --cwd /abs/project --profile standard [--mode plan] [--timeout 900]

# 显式指定模型与思考等级
$S run "任务描述" --cwd /abs/project --model argon --effort high

# 查看可用模型、别名、预设
$S models

# 续接上一次会话（拿上次输出里的 conversation_id）
$S run "追问" --cwd /abs/project --resume <conversation_id>

# 多轮常驻：文件里每轮用单独一行 --- 分隔
$S session /path/prompts.txt --cwd /abs/project

# 大输入：提示词写进文件，用 @ 传入（走 stdin，绕开命令行单参数 128KB 的限制；但 agy 单条消息上限约 19 万字节，脚本超过 18 万就拒绝）
$S run @/path/prompt_with_materials.txt --cwd /abs/project --policy readonly --profile deep

# 并行拉起多个子代理：文件里每个独立任务用单独一行 --- 分隔，每个任务一个 agy 进程
$S batch /path/tasks.txt --cwd /abs/project --policy readonly --profile standard --parallel 8

# 多维度探索：同一份材料放在每个任务最前面（--shared），每个任务只写不同维度的问题；材料给全了就用 none 禁用工具
$S batch /path/dimensions.txt --shared @/path/materials.txt --cwd /abs/project --policy none --profile standard --parallel 6

# 结构化结果
$S run "统计…" --cwd /abs/project --schema '{"type":"object","properties":{...},"required":[...]}'
```

## 权限策略（--policy）

agy 自身在本机等于无限制（always-proceed、无沙箱），而且它的操作不经过 Claude Code 的权限检查，所以由 `scripts/agy_gate.py` 通过 agy 的 PreToolUse hook 拦截。hook 装在 `~/.gemini/config/hooks.json`（名为 `cc-subagent-gate`），只有封装脚本设置了 `CC_AGY_POLICY` 时才生效；你自己的交互式 agy 不受影响。

| 策略 | 允许 | 拒绝 |
|---|---|---|
| `none` | 不能用任何工具（只放行 `finish`），只能根据提示里的材料一次作答 | 所有工具 |
| `readonly` | 读/搜索工具、网页读取；shell 只限只读命令（ls/cat/rg/find/git status·log·diff 等，无重定向写入、无 `$()`） | 一切写文件、其余 shell |
| `workspace`（**默认**） | 读工具；写文件仅限工作目录（及 agy 自己的 artifact 目录）；一般 shell（构建、测试、脚本） | 写工作区外、git commit/push/reset --hard 等、sudo、`curl … \| sh`、上传数据、ssh/scp/rsync、kill/pkill、systemctl/crontab、apt、全局安装、eval/exec |
| `full` | 全部（仍记录日志） | — |

`none` 用于材料已经全部放进提示的一次性任务。问题完整送达时，Argon 会照"不要使用任何工具"去做（实测 6/6 没有调用工具，单个任务 20–28 秒）。`none` 是兜底：一旦消息被截断、问题丢失，子代理会自己翻文件找任务，每个任务调用模型 3–8 次、跑 100 多秒。

所有策略（`full` 除外）都拒绝访问凭据文件（`~/.ssh`、`.env`、agy 的 oauth token 等），也拒绝 `schedule`、MCP、浏览器、再开子代理、发消息、删除知识库，以及未知的新工具。

选择：
- 调研、审查、第二意见用 `readonly`；写代码、改文件用默认 `workspace`。
- `full` 只在用户明确同意时用（例如需要 agy 的浏览器或 MCP），并告诉用户这次没有限制。
- agy 被拒后常会换工具绕路，前言里已要求它不要这样做，但仍要对照 `tools(...)` 检查。
- shell 过滤靠模式匹配，挡得住意外和越界，挡不住刻意构造的绕过；高风险仓库不要用 `full`。
- 运行时报 `gate hook missing`（例如 orca 重写了 hooks.json）：执行 `$S install-hook`。卸载用 `$S uninstall-hook`。
- 在 Claude Code 里设置权限规则时，可以按 `agy_run.py run --policy readonly` 这样的前缀区分（`--policy` 放在最前面）。

## 需确认的操作（延后审批 + 预授权）

gate 的拒绝分三类，报告里分组列出：

| 类别 | 例子 | 处理 |
|---|---|---|
| 硬拒绝 | 读凭据、sudo、`curl \| sh`、上传数据、ssh/scp、eval | 不可授权。告诉用户；只有用户明确要求才用 `--policy full` |
| 待审批 | 写工作区外的文件、结束进程、systemctl/crontab、全局安装、丢弃改动的 git 命令、schedule/MCP/浏览器/子代理等工具；readonly 下的写入和非只读命令 | 问用户，批准后用 `--grant` 放行 |
| 交给主代理 | git commit / push / reset --hard / rebase / merge | 不授权给 agy。由你审完 diff 后自己执行（走 Claude Code 自己的权限确认，并遵守"用户要求才提交"） |

授权写法（只对这一次 agy 进程有效）：
- `cmd:<命令>`：必须与 agy 发出的命令行**完全一致**。`pkill -f vite` 的授权不会放行 `pkill -f vite; rm -rf ~`。
- `write:<文件或目录>`：允许写这个文件，或这个目录下的文件；也覆盖 shell 重定向到该位置的写入。
- `tool:<工具名>`：允许该工具的所有调用。
- 硬拒绝项和交给主代理的项，授权无效。

流程：
1. **预授权**：派发前就能预见要越界的操作（例如"把报告写到 /srv/share/"），先问用户，启动时带上 `--grant`。
2. **跑完先看两处**：
   - 报告里的分组：来自 gate 实际拦下的调用，附有精确的授权写法。
   - 回复末尾的「待审批」小节：agy 读了前言后常会**主动跳过**越界操作，这些不会经过 gate，只出现在这里，格式为 `grant: …` / `handoff: …`。
3. **待审批项**用 AskUserQuestion（multiSelect）一次问完，每项写清精确内容和理由。不要替用户批准。
4. **批准后二选一**：
   - 操作嵌在 agy 的任务中间，后续步骤还依赖它：用报告里的 `resume:` 命令续接，只保留用户批准的 `--grant`。
   - 操作是独立的收尾动作：由你自己执行，更直观。
5. 续接后 agy 如果换了命令写法，会再次被拒，重复上面的流程即可。
6. **交给主代理的项**：你先 `git diff` 审改动，按用户的要求决定是否提交。agy 只提供建议的提交信息。

## 模型与思考等级

- `--model` 接受完整 id（`gemini-4-argon-high`）、基础 id（`gemini-4-argon`）或别名：`argon` `flash` `opus` `sonnet` `gpt-oss`。
- `--effort` 是思考等级，当前所有模型只有 `low|medium|high`（gpt-oss-120b 只有 medium），传 `xhigh/max` 会被 agy 拒绝。给了 `--effort` 时脚本会去掉 id 上的等级后缀，避免 agy 报 "conflicts with --effort"；不给则用该模型默认档。
- `--profile` 一次选好两者，`--model`/`--effort` 可单独覆盖其中一项：

| profile | 模型 / 等级 | 适用 |
|---|---|---|
| `quick` | gemini-3.8-flash / low | 机械性小活：查找、批量改名、格式转换、简单脚本 |
| `standard` | gemini-4-argon / medium | 一般编码/调研（= agy 默认设置） |
| `deep` | gemini-4-argon / high | 复杂推理、跨文件分析、超大上下文（>200k） |
| `iterate` | claude-sonnet-5-5 / medium | 多轮迭代修改 |
| `review` | claude-opus-5-5 / high | 第二意见、代码审查、方案把关 |

选择原则：
- 用户点名了模型或等级就照用户的来；没点名按上表选，拿不准用 `standard`。
- 想要"不同于 Claude 的视角"（交叉审查、第二意见）时优先 Gemini（`deep`）；`review` 是 agy 里的 Claude Opus，视角与你相近，适合当独立复核者而非"异构"意见。
- 思考等级越高越慢、thinking token 越多（实测 argon high 一个读文件小任务即 ~200 thinking token）；机械任务不要上 high。
- 失败或结果质量差时，先升等级（low→medium→high），再换模型。
- Argon 首 token 慢且不稳：实测模型本身 4–16 秒，降到 low 也不快；加上进程启动，端到端常在 10 秒以上。用 Argon 的任务放到后台跑、并行跑，不要指望它快速来回。
- 未指定任何模型选项时跟随 agy 设置（当前 Gemini 4 Argon Medium）；报告头部会打印实际使用的 `model` 和 `effort`。

## 运行

- 耗时较长（>1 分钟）的任务用 Bash 的 `run_in_background: true` 运行，完成后会通知你。
- 多个互不相关的子任务用 `batch` 一次并行派发：实测 8 路并发 Argon 全部成功、没有限流，总耗时 31.7 秒，各任务耗时加起来是 174.9 秒；单个任务会变慢一些（14–32 秒）。订阅额度和限流的上限没有测过，量大时从 `--parallel 4–8` 开始。
- 同时启动多个进程时，agy 偶尔在加载模型配置阶段就报错（如 "--effort is not supported"）。`batch` 对这种在会话创建前就失败的任务会自动重试，最多 2 次。
- `--shared` 让各任务共用同一份材料，只换后面的问题。
- **agy 会把单条用户消息截在约 19 万字节**，超出部分换成 `<truncated N bytes>`，不报错。问题放在材料后面时会一起丢掉。脚本对超过 18 万字节的消息直接报错；材料太多就精简，或用 `session` 分多轮发送。
- 输出中的 `tools(...)` 是 agy 实际做了什么的轨迹，`log:` 是完整 NDJSON，必要时 `jq`/python 查看。

## 写给 agy 的提示

agy 看不到你的对话上下文，每次都是冷启动：

- 写清目标、相关文件的**绝对路径**、约束（不准改哪些文件、不准 git commit/push）、完成标准、期望的输出格式。
- **给 Gemini 的任务要一次给全、一次产出**：把需要的材料直接放进提示词（大材料用 `@文件`），或者一次列全所有要读的路径；要求一次输出完整结果。不要派"自己去仓库里一步步找"这种需要多轮工具探索的活，并用 `--policy none` 硬性禁用工具。
- 只读调研用 `--policy readonly`（硬限制）；`--mode plan` 只是让 agy 先出计划，不是权限限制。
- 需要机器可读结果时用 `--schema`，不要让它"输出 JSON"然后自己解析散文。

## 验收（必须做）

agy 的结论是线索不是事实：

- 对照 `tools(...)` 轨迹确认它真的读了/跑了它声称的东西；改了文件的，自己 `git diff` 或读文件核对。
- 有时它的回复会夹带内部过程句（如 "Use read_file directly."、"Work in progress..."），汇报给用户前过滤掉。
- `status` 不是 `SUCCESS`、或 exit≠0 时，看输出里的 ERROR / stderr 和 log，不要假装成功。
- 向用户报告时说明哪些是 agy 做的、你核实了哪些。

## 故障排查

- `You are not logged into Antigravity` / `Authentication expired`：让用户执行 `! agy` 交互登录一次。
- `Shell is unusable` / `Command working directory is invalid`：没带 `--project outside-of-project`（直接调用 agy 时要自己加）。
- `unknown agent`：`--agent` 名字不存在，`agy agents` 查看。
- 正常工具全被拒绝：看 `.gate.jsonl` 日志里的 reason；hook 必须输出 decision，空输出会被 agy 当作拒绝。
- `~/.gemini/antigravity-cli/hooks.json` 里的 hook 在 agy 1.4.2 下不会执行，不要往那里装。
- 模型 429/配额：换模型（Flash ↔ Argon ↔ Claude）重试。
