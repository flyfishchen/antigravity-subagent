#!/usr/bin/env python3
"""Run Google Antigravity CLI (agy) headlessly as a subagent and print a compact report.

Usage:
  agy_run.py run "<prompt>" | @FILE | - [--policy none|readonly|workspace|full] [--grant G ...] [--cwd DIR] [--profile P | --model M] [--effort E]
                            [--mode plan|accept-edits|auto]
                            [--resume CONV_ID | --continue] [--schema JSON_OR_FILE]
                            [--add-dir DIR ...] [--sandbox] [--project ID] [--timeout SEC] [--raw-preamble]
  agy_run.py session PROMPTS_FILE [same options]   # one process, one turn per prompt (turns separated by a line "---")
  agy_run.py batch TASKS_FILE [--parallel N] [--shared @FILE] [--warm] [same options]
                                                    # independent tasks (separated by a line "---"), one agy
                                                    # process each, N at a time; --shared puts the same
                                                    # materials first in every task so the prefix is identical,
                                                    # --warm runs task 1 alone before fanning out the rest
  agy_run.py models                                 # available models, aliases and profiles
  agy_run.py install-hook | uninstall-hook          # manage the PreToolUse gate in ~/.gemini/config/hooks.json

--policy (default workspace) is enforced by agy_gate.py through agy's PreToolUse hook; the gate is
inert unless CC_AGY_POLICY is set, so interactive agy sessions are unaffected.
--grant (repeatable) pre-approves one operation for this run: cmd:<exact command line>,
write:<file or dir>, tool:<tool name>. Denied operations are reported with the grant that would allow them.

--model takes a full id (gemini-4-argon-high), a base id (gemini-4-argon) or an alias (argon);
--effort (thinking level) replaces any level suffix on the model id. --profile picks both.

By default runs with --project outside-of-project so agy's shell uses --cwd instead of the
"default-cli-project" folder, and prefixes the prompt with a workspace preamble (absolute cwd).
Raw NDJSON events are saved under $AGY_SUBAGENT_LOG_DIR (default ~/.local/state/agy-subagent).
"""
import argparse
import json
import os
import shlex
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
import sys
import time
from pathlib import Path

AGY = os.environ.get("AGY_BIN", "agy")
LOG_DIR = Path(os.environ.get("AGY_SUBAGENT_LOG_DIR",
                              Path.home() / ".local/state/agy-subagent"))

MODEL_ALIASES = {
    "argon": "gemini-4-argon",
    "flash": "gemini-3.8-flash",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "gpt-oss": "gpt-oss-120b",
}
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# name: (model, effort, when to use)
PROFILES = {
    "quick":   ("flash", "low", "机械性小活：查找、批量改名、格式转换、简单脚本"),
    "standard": ("argon", "medium", "一般编码/调研任务（与 agy 默认设置一致）"),
    "deep":    ("argon", "high", "复杂推理、跨文件分析、读超大上下文（>200k）"),
    "iterate": ("sonnet", "medium", "多轮迭代修改，缓存命中高、续接便宜"),
    "review":  ("opus", "high", "第二意见/代码审查/方案把关"),
}

GATE = Path(__file__).resolve().with_name("agy_gate.py")
# ~/.gemini/antigravity-cli/hooks.json is loaded but its hooks never run in print mode (agy 1.4.2)
HOOK_FILE = Path.home() / ".gemini/config/hooks.json"
HOOK_NAME = "cc-subagent-gate"
# agy treats empty hook output as deny, so the inert path must still answer "ask"
HOOK_COMMAND = (f'if [ -n "$CC_AGY_POLICY" ]; then exec python3 -I {GATE}; fi; '
                'cat >/dev/null; printf \'%s\\n\' \'{"decision":"ask"}\'')

POLICY_NOTES = {
    "none": "权限策略 none：本任务禁止使用任何工具（包括读文件和 shell），所需材料都已在提示中，直接一次作答。",
    "readonly": "权限策略 readonly：只读。不得创建/修改/删除任何文件，shell 只能用只读命令。",
    "workspace": "权限策略 workspace：只能修改工作目录内的文件；禁止提权、远程连接、结束进程、系统管理命令、"
                 "定时任务、MCP、浏览器、再开子代理、访问凭据文件。git commit/push 由主代理执行，不要自己运行，"
                 "需要时在结论里给出建议的命令和提交信息。",
    "full": "权限策略 full：不限制，但所有工具调用都会被记录。",
}

PREAMBLE = (
    "[工作目录: {cwd}]\n"
    "{policy_note}{grant_note}被策略拒绝的操作不要换其它工具或写法绕过：跳过它、继续完成其余部分，"
    "并在结论末尾的「待审批」小节逐条列出被拒操作，每条一行写成 `grant: cmd:<精确命令>` / `grant: write:<绝对路径>` / "
    "`grant: tool:<工具名>` 加理由（git commit/push 写成 `handoff: <命令>`）。\n"
    "所有相对路径都相对于上述工作目录；不要使用其它项目目录。"
    "你是被另一个代理调用的子代理，处于无人值守的无头模式：不要提问、不要等待确认，"
    "信息不足时做出合理假设并在结论中注明。完成后用简洁的要点给出结论，"
    "并列出你修改/创建的文件（绝对路径）。\n\n"
)


def resolve_model(model, effort, profile):
    """Return (model, effort) as agy expects; agy rejects a suffixed id combined with --effort."""
    if profile:
        p_model, p_effort, _ = PROFILES[profile]
        model, effort = model or p_model, effort or p_effort
    if not model:
        return None, effort
    model = MODEL_ALIASES.get(model, model)
    if effort:
        for level in EFFORTS:
            if model.endswith("-" + level):
                model = model[: -len(level) - 1]
                break
    return model, effort


def list_models():
    p = subprocess.run([AGY, "models"], capture_output=True, text=True)
    print("== agy models (id\t显示名) ==")
    print("\n".join(l for l in p.stdout.splitlines() if "\t" in l) or p.stderr.strip())
    print("\n== 别名（基础 id，配合 --effort 选思考等级 low|medium|high；不带 --effort 用模型默认档） ==")
    for k, v in MODEL_ALIASES.items():
        print(f"{k:8} -> {v}")
    print("\n== --profile 预设 ==")
    for k, (m, e, why) in PROFILES.items():
        print(f"{k:9} {MODEL_ALIASES[m] + ' / ' + e:28} {why}")
    return p.returncode


def build_cmd(a, stream_input):
    cmd = [AGY, "--output-format", "stream-json", "--project", a.project]
    model, effort = resolve_model(a.model, a.effort, a.profile)
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if a.mode:
        cmd += ["--mode", a.mode]
    if a.resume:
        cmd += ["--conversation", a.resume]
    elif a.cont:
        cmd += ["--continue"]
    if a.schema:
        cmd += ["--json-schema", a.schema]
    if a.sandbox:
        cmd += ["--sandbox"]
    for d in [a.cwd] + (a.add_dir or []):
        cmd += ["--add-dir", d]
    if a.timeout:
        cmd += ["--print-timeout", f"{a.timeout}s"]
    if stream_input:
        cmd += ["--input-format", "stream-json", "--print="]
    return cmd


def wrap(a, prompt):
    if a.raw_preamble:
        return prompt
    grant_note = ("本次已获用户批准的操作（仅限完全一致的命令/路径/工具）：" + "；".join(a.grant) + "。\n") if a.grant else ""
    return PREAMBLE.format(cwd=a.cwd, policy_note=POLICY_NOTES[a.policy], grant_note=grant_note) + prompt


def load_hooks():
    try:
        return json.loads(HOOK_FILE.read_text())
    except FileNotFoundError:
        return {}


def hook_installed():
    try:
        hooks = (load_hooks().get(HOOK_NAME) or {}).get("PreToolUse") or []
    except (json.JSONDecodeError, AttributeError):
        return False
    return any(h.get("command") == HOOK_COMMAND for g in hooks for h in g.get("hooks", []))


def install_hook():
    data = load_hooks()
    data[HOOK_NAME] = {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": HOOK_COMMAND, "timeout": 10}]}]}
    HOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOOK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"installed {HOOK_NAME} into {HOOK_FILE}")
    return 0


def uninstall_hook():
    data = load_hooks()
    if data.pop(HOOK_NAME, None) is not None:
        HOOK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"removed {HOOK_NAME} from {HOOK_FILE}")
    return 0


def run(a, prompts):
    # prompts always go through stdin: a single argv string is capped at 128 KiB on Linux,
    # which large-input Gemini tasks easily exceed
    cmd = build_cmd(a, stream_input=True)
    stdin_data = "".join(
        json.dumps({"event": "user", "message": {"role": "user", "content": wrap(a, p) if i == 0 else p}},
                   ensure_ascii=False) + "\n"
        for i, p in enumerate(prompts))

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    tmp_log = LOG_DIR / f"run-{stamp}.ndjson"
    gate_log = LOG_DIR / f"run-{stamp}.gate.jsonl"
    env = os.environ | {"CC_AGY_POLICY": a.policy, "CC_AGY_GATE_LOG": str(gate_log),
                        "CC_AGY_GRANTS": json.dumps(a.grant or [], ensure_ascii=False)}
    t0 = time.time()
    hard_timeout = (a.timeout + 120) if a.timeout else None
    try:
        p = subprocess.run(cmd, cwd=a.cwd, input=stdin_data, capture_output=True,
                           text=True, timeout=hard_timeout, env=env)
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err, rc = "hard timeout", 124
    tmp_log.write_text(out)

    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            # agy occasionally emits raw control characters inside strings
            events.append(json.loads(line, strict=False))
        except json.JSONDecodeError:
            pass

    conv_id, model, results, tools, usage = None, None, [], [], {"input": 0, "cache_read": 0, "output": 0, "thinking": 0}
    for ev in events:
        kind = ev.get("event")
        if kind == "init":
            conv_id = ev.get("conversation_id")
            model = (ev.get("init") or {}).get("model")
        elif kind == "result":
            results.append(ev["result"])
            conv_id = ev["result"].get("conversation_id") or conv_id
        elif kind == "step_update":
            s = ev["step_update"]
            if s.get("state") != "DONE":
                continue
            if s.get("step_type") == "tool":
                params = (s.get("tool_info") or {}).get("parameters") or {}
                hint = next((str(v) for k, v in params.items()
                             if k.lower() in ("path", "commandline", "command", "url", "query", "absolutepath",
                                              "targetfile", "directorypath", "searchpath")), "")
                tools.append(f"{s.get('tool_name')}({hint[:120]})" if hint else s.get("tool_name"))
            u = s.get("usage")
            if u:  # per-step usage; result.usage is cumulative over the whole conversation
                usage["input"] += u.get("input_tokens", 0)
                usage["cache_read"] += u.get("cache_read_tokens", 0)
                usage["output"] += u.get("output_tokens", 0)
                usage["thinking"] += u.get("thinking_tokens", 0)

    gate = []
    if gate_log.exists():
        gate = [json.loads(l) for l in gate_log.read_text().splitlines() if l.strip()]
    denied = [g for g in gate if g["decision"] == "deny"]

    if conv_id:
        final_log = LOG_DIR / f"{conv_id}-{int(t0)}.ndjson"
        tmp_log.rename(final_log)
        tmp_log = final_log
        if gate_log.exists():
            gate_log = gate_log.rename(final_log.with_suffix(".gate.jsonl"))

    prompt_total = usage["input"] + usage["cache_read"]
    hit = (usage["cache_read"] / prompt_total * 100) if prompt_total else 0.0
    status = results[-1].get("status") if results else "NO_RESULT"

    print(f"conversation_id: {conv_id}")
    req_model, req_effort = resolve_model(a.model, a.effort, a.profile)
    print(f"model: {model or req_model or '(agy 默认)'}  effort: {req_effort or '(模型默认)'}")
    print(f"status: {status}  exit: {rc}  wall: {time.time() - t0:.1f}s  turns: {len(results)}")
    print(f"usage(this run): prompt={prompt_total} (uncached={usage['input']}, cache_read={usage['cache_read']}, "
          f"hit={hit:.0f}%) output={usage['output']} thinking={usage['thinking']}")
    print(f"tools({len(tools)}): " + ("; ".join(tools) if tools else "-"))
    print(f"policy: {a.policy}  grants: {len(a.grant or [])}  gate checks: {len(gate)}  denied: {len(denied)}")
    groups = {"hard": [], "approval": [], "handoff": []}
    for g in denied:
        groups.get(g.get("kind"), groups["hard"]).append(g)
    labels = {"hard": "硬拒绝（不可授权，只有 --policy full 能放行）",
              "approval": "待审批（经用户同意后用 --grant 放行）",
              "handoff": "交给主代理执行（不授权给 agy）"}
    for kind, items in groups.items():
        if items:
            print(f"  {labels[kind]}:")
        seen = set()
        for g in items:
            key = g.get("grant") or g["args"]
            if key in seen:
                continue
            seen.add(key)
            what = g["grant"] if g.get("grant") else f"{g['tool']} {g['args'][:150]}"
            if kind == "handoff":
                what = what.removeprefix("cmd:")
            print(f"    - {what}  -- {g['reason']}")
    approvals = list(dict.fromkeys(g["grant"] for g in groups["approval"] if g.get("grant")))
    if approvals and conv_id:
        flags = " ".join(f"--grant {shlex.quote(x)}" for x in approvals)
        print(f"  resume: {sys.argv[0]} run --policy {a.policy} {flags} --resume {conv_id} --cwd {shlex.quote(a.cwd)} "
              "\"已获批准，继续完成被拒绝的部分\"")
    print(f"log: {tmp_log}")
    for i, r in enumerate(results, 1):
        tag = f"--- response (turn {i}/{len(results)}) ---" if len(results) > 1 else "--- response ---"
        print(tag)
        if r.get("error"):
            print(f"ERROR: {r['error']}")
        print((r.get("response") or "").rstrip())
        if "structured_output" in r:
            print("--- structured_output ---")
            print(json.dumps(r["structured_output"], ensure_ascii=False, indent=2))
    if not results:
        print("--- stderr (tail) ---")
        print("\n".join(err.strip().splitlines()[-20:]))
    return 0 if status == "SUCCESS" else 1


def forward_args(a):
    """Rebuild the per-task options for child `run` invocations of a batch."""
    out = ["--policy", a.policy, "--cwd", a.cwd, "--project", a.project, "--timeout", str(a.timeout)]
    for flag in ("model", "effort", "profile", "mode", "schema"):
        if getattr(a, flag):
            out += [f"--{flag}", getattr(a, flag)]
    for g in a.grant or []:
        out += ["--grant", g]
    for d in a.add_dir or []:
        out += ["--add-dir", d]
    out += ["--sandbox"] * a.sandbox + ["--raw-preamble"] * a.raw_preamble
    return out


def batch(a, tasks, shared=""):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    common = forward_args(a)

    def one(task):
        with tempfile.NamedTemporaryFile("w", dir=LOG_DIR, prefix="task-", suffix=".txt", delete=False) as f:
            # shared materials first, task last: identical bytes up to the question keep the prefix cacheable
            f.write(f"{shared}\n\n---\n\n{task}" if shared else task)
        try:
            t = time.time()
            for attempt in range(3):
                p = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "run", "@" + f.name, *common],
                                   capture_output=True, text=True)
                # agy occasionally fails to load model config when many processes start at once
                # ("--effort is not supported ..."); such runs die before a conversation exists, so retry
                if "conversation_id: None" not in p.stdout:
                    break
                time.sleep(3 + 3 * attempt)
            return p.returncode, round(time.time() - t, 1), p.stdout + (p.stderr if p.returncode else "")
        finally:
            os.unlink(f.name)

    t0 = time.time()
    results = [one(tasks[0])] if a.warm else []
    with ThreadPoolExecutor(max_workers=a.parallel) as pool:
        results += list(pool.map(one, tasks[len(results):]))
    print(f"batch: {len(tasks)} tasks, parallel {a.parallel}, wall {time.time() - t0:.1f}s, "
          f"sum of task walls {sum(r[1] for r in results):.1f}s")
    for i, (rc, wall, out) in enumerate(results, 1):
        status = next((l for l in out.splitlines() if l.startswith("status:")), "status: ?").split("  ")[0]
        usage = next((l for l in out.splitlines() if l.startswith("usage(this run):")), "")
        hit = usage.split("hit=")[1].split(")")[0] if "hit=" in usage else "?"
        print(f"  #{i} rc={rc} {wall}s {status} cache-hit {hit}  -- {tasks[i - 1].splitlines()[0][:50]}")
    for i, (rc, wall, out) in enumerate(results, 1):
        print(f"\n===== task {i}/{len(tasks)} =====")
        print(out.rstrip())
    return 0 if all(r[0] == 0 for r in results) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["run", "session", "batch", "models", "install-hook", "uninstall-hook"])
    ap.add_argument("prompt", nargs="?", help="prompt text, @file or '-' for stdin (run); prompts file with turns separated by a line '---' (session)")
    ap.add_argument("--policy", choices=list(POLICY_NOTES), default="workspace",
                    help="tool permission policy enforced by the PreToolUse gate (default workspace)")
    ap.add_argument("--grant", action="append", metavar="G",
                    help="pre-approve one operation: cmd:<exact command>, write:<path>, tool:<name>")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--model", help="full id, base id or alias: " + ", ".join(MODEL_ALIASES))
    ap.add_argument("--effort", choices=EFFORTS, help="thinking level; current models accept low|medium|high")
    ap.add_argument("--profile", choices=list(PROFILES), help="model+effort preset; --model/--effort override it")
    ap.add_argument("--mode", choices=["plan", "accept-edits", "auto"])
    ap.add_argument("--resume", metavar="CONV_ID")
    ap.add_argument("--continue", dest="cont", action="store_true")
    ap.add_argument("--schema", help="JSON schema string or path to schema file")
    ap.add_argument("--add-dir", action="append")
    ap.add_argument("--sandbox", action="store_true")
    # agy's default-cli-project pins run_command's cwd to its own folder; outside-of-project uses our cwd
    ap.add_argument("--project", default="outside-of-project")
    ap.add_argument("--timeout", type=int, default=900, help="seconds; 0 = no limit")
    ap.add_argument("--raw-preamble", action="store_true", help="do not prepend the workspace preamble")
    ap.add_argument("--parallel", type=int, default=4, help="batch: concurrent agy processes (default 4)")
    ap.add_argument("--shared", metavar="@FILE", help="batch: materials placed before every task (same prefix)")
    ap.add_argument("--warm", action="store_true", help="batch: run task 1 alone first to warm the shared prefix")
    a = ap.parse_args()
    if a.command == "models":
        sys.exit(list_models())
    if a.command == "install-hook":
        sys.exit(install_hook())
    if a.command == "uninstall-hook":
        sys.exit(uninstall_hook())
    if not hook_installed():
        sys.exit(f"gate hook missing from {HOOK_FILE}; run: {sys.argv[0]} install-hook")
    if a.prompt is None:
        ap.error("prompt is required")
    a.cwd = str(Path(a.cwd).resolve())
    for g in a.grant or []:
        if g.partition(":")[0] not in ("cmd", "write", "tool") or not g.partition(":")[2].strip():
            ap.error(f"invalid --grant {g!r}; use cmd:<command>, write:<path> or tool:<name>")

    if a.prompt == "-":
        text = sys.stdin.read()
    elif a.command in ("session", "batch") or a.prompt.startswith("@"):
        text = Path(a.prompt.removeprefix("@")).read_text()
    else:
        text = a.prompt
    if a.command in ("session", "batch"):
        prompts = [p.strip() for p in text.split("\n---\n")]
    else:
        prompts = [text.strip()]
    prompts = [p for p in prompts if p]
    if not prompts:
        sys.exit("empty prompt")
    if a.command == "batch":
        if a.resume or a.cont:
            ap.error("batch tasks are independent; --resume/--continue are not supported")
        shared = Path(a.shared.removeprefix("@")).read_text().strip() if a.shared else ""
        sys.exit(batch(a, prompts, shared))
    sys.exit(run(a, prompts))


if __name__ == "__main__":
    main()
