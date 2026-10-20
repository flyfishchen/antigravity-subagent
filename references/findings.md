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

## 博客要点（https://flyfishchen.github.io/blog/llm-prompt-cache-analysis/）

- Gemini 隐式缓存 best-effort，空闲 TTL 约 30–60s；空闲后掉到只剩系统提示+工具定义的 ~5.4k 共享前缀，重算导致 5–15s 首 token 延迟。
- 作者推测原因：空闲 HTTP/2 连接关闭后请求落到没有缓存的实例。
- Anthropic 显式缓存 5 分钟 TTL、命中价 10%；Gemini 命中折扣 75%；OpenAI 50%。
- 建议：交互式迭代编码用 Claude；>200k 的大上下文载入用 Gemini。
