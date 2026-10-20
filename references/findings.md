# agy 子代理实测记录（2026-10-20，agy 1.4.2）

测试目录为临时目录，任务为读/写小文本文件，上下文约 7–15k token。`input` 为未命中缓存部分，`cache_read` 单独计（两者之和 = 本步提示词总量）。

## 单进程多轮（stream-json 输入，3 轮，每轮 2 步）

| 模型 | 首步 input / cache | 后续步 input / cache | 结论 |
|---|---|---|---|
| gemini-3.8-flash-low | 7428 / 0 | 7568–8298 / 0 | 小上下文完全不命中 |
| gemini-4-argon-low | 7416 / 0 | ~2100–2300 / 5362–5739 | 只命中 ~5.3k 静态前缀（与博客中 5357 下限一致） |
| claude-sonnet-5-5-low | 11531 / 0 | 108–527 / 11243–12708 | 命中率 95%+ |

## 跨进程

- Claude：新进程首步即 `cache_read=40365`（前几分钟其它 agy 调用已写入相同前缀）；`--conversation` 续接后 `cache_read=64920`。说明 5 分钟 TTL 内跨进程、跨会话共享前缀。
- gemini-3.8-flash：未观察到跨进程命中。
- gemini-4-argon-low：约 2 分钟后 `--conversation` 续接（~17k 上下文），cache_read=12426，命中 72%；同模型单进程两轮合计 54%。命中量波动大，符合 best-effort 隐式缓存。

## 其它观察

- `result.usage` 在续接/多轮时是**会话累计值**；本次开销需累加 `step_update.usage`（封装脚本已这样做）。
- `--output-format json` 的 `response` 偶尔含未转义控制字符，解析需 `json.loads(..., strict=False)`。
- 不带 `--project` 时 shell 工具 cwd = default-cli-project 的目录（`~/.gemini/config/projects/default-cli-project.json` 里登记的文件夹，作者环境中该文件夹已被删除）；`--project outside-of-project` 后 `pwd` 返回进程 cwd。
- `--mode plan`：写 `brain/<id>/*_plan.md`，不改工作区文件，回复里请求"Proceed"。
- 一个统计文件数的小任务带 `--json-schema` 时，agy 自行启动了子代理，总消耗 58k token。
- 进程墙钟：简单单轮 4–28s；Gemini 多轮中偶发 >60s 的长尾。

## 首 token 延迟（2026-10-20，扣除 agy 启动 5–7s 后的模型本身）

| 模型 / 思考等级 | 次数 | 首 token |
|---|---|---|
| gemini-4-argon / medium，~7k 上下文 | 5 | 4.0–6.5s |
| gemini-4-argon / low | 3 | 4.8–16.5s |
| gemini-3.8-flash / medium | 3 | 3.6–4.7s（thinking 225–483） |
| claude-sonnet-5-5 / medium | 3 | 1.5–1.8s |
| argon / medium，61.5k 无缓存 | 2 | ~7s |
| argon / medium，61.5k，缓存命中 96% | 2 | 7.0–9.6s（命中没有变快） |

- Argon 报告的 thinking_tokens 始终为 0，可能没有计入。
- 从进程启动算起的端到端首 token 时间：Argon 8.7–22s，Sonnet 7–7.7s。
- 用户日常长会话里观察到 Argon 首 token 10–80s，受控测试未复现。
- 推论：Argon 适合后台、可并行、对延迟不敏感的子代理任务，不适合做交互主模型。

## 并行子代理（2026-10-20，gemini-4-argon / medium）

- 8 个互不相关的简答题，8 路并发：8/8 成功、无限流；总耗时 31.7s，各任务耗时之和 174.9s；单个任务 13.9–31.7s。
- 同一份约 61.6k token 的材料、6 个维度并发：
  - 只在提示里写"不要使用任何工具"：12 个任务里 10 个仍调用了 read_file 或 run_command，每个任务调用模型 3–8 次，单个任务 23–188s，总耗时 156.6s；先预热的一组总耗时 309.9s。
  - `--policy none`：每个任务只调用一次模型，单个任务 26–34s，总耗时 34.1s。
  - 跨任务缓存：17 个任务的首次调用 cache_read 都是 0，`--warm` 无效。任务内部多轮调用能命中自己的前缀（72–85%）。
  - 6 路同时启动时，1 个进程在加载模型配置时报 "--effort is not supported for model"，属偶发，重试即可。

## 博客要点（https://flyfishchen.github.io/blog/llm-prompt-cache-analysis/）

- Gemini 隐式缓存默认开启，但官方原话是 "no cost saving guarantee"，也没给保留时间。实测停顿几十秒到两分钟就可能掉到约 5.3k 的保底，重新预填充导致首 token 延迟 5–15s。
- 约 5.3k 的保底在 Pi 和 agy 两个系统提示完全不同的 Agent 上都出现，更可能是平台侧因素（推测）。
- 已核对的官方规格（2026-10）：
  - 缓存读价格：Gemini 3.8 Flash 0.1x、4 Argon 0.05x；Claude Opus 5.5 / Sonnet 5.5 0.05x；OpenAI GPT-5.6 及以后 0.1x。
  - Claude 默认 5 分钟、命中续期，可选 1 小时。
  - OpenAI GPT-5.6 及以后命中后至少保留 30 分钟。
  - Gemini 4 Argon 不支持显式缓存。
- 建议：交互式迭代用缓存有明确规格的模型（Claude）；一次性读大量资料、不依赖缓存的任务用 Gemini。
