#!/usr/bin/env python3
"""PreToolUse gate for agy subagent runs (installed in ~/.gemini/config/hooks.json).

Active only when CC_AGY_POLICY is set (agy_run.py sets it); otherwise it answers "ask",
which leaves agy's own permission flow unchanged. Decisions:
  readonly   read/search tools + read-only shell commands
  workspace  readonly + file writes inside the workspace + shell minus dangerous patterns
  full       everything (logged only)
Every decision is appended as JSON to $CC_AGY_GATE_LOG when set.

Denials carry a kind:
  hard      never grantable below "full" (credentials, privilege escalation, pipe-to-interpreter, ...)
  handoff   left to the main agent (git history/push), never granted to agy
  approval  allowed once the user approves; the log entry carries the exact grant to pass back
Grants come from $CC_AGY_GRANTS (JSON list): "cmd:<exact command line>", "write:<file or dir>",
"tool:<tool name>". They apply to the current agy process only.

Shell filtering is pattern matching: it stops accidents, not a determined adversary.
Note: agy treats empty or invalid hook output as deny, so this script must always print a decision.
"""
import json
import os
import re
import shlex
import sys
import time

ASK = "ask"  # neutral: agy applies its own permission setting
DENY = "deny"

READ_TOOLS = {
    "view_file", "read_file", "list_dir", "find_by_name", "grep_search", "command_status",
    "read_url_content", "search_web", "read_resource", "list_resources", "list_permissions",
    "finish", "wait", "wait_5_seconds", "ask_question", "ask_permission", "ask_custom_permission",
    "manage_task", "list_plugin_accounts",
}
WRITE_TOOLS = {
    "write_to_file", "write_file", "replace_file_content", "multi_replace_file_content",
    "sed_file", "notebook_edit",
}
SHELL_TOOLS = {"run_command", "send_command_input"}
WORKSPACE_EXTRA = {"generate_image"}
# Everything else (schedule, subagents, MCP, browser, messaging, knowledge deletion, workflows,
# marketplace, notebook_execution, tools added by future agy versions) is denied below "full".

SECRET_PATTERNS = [
    r"\.ssh(/|\b)", r"\.gnupg", r"antigravity-oauth-token", r"\.credentials", r"\.netrc",
    r"\.aws/credentials", r"\.config/gcloud", r"\.docker/config\.json", r"(^|/)\.env(\.|$|\s)",
]

READONLY_CMDS = {
    "ls", "cat", "head", "tail", "wc", "grep", "egrep", "fgrep", "rg", "ag", "find", "fd", "tree",
    "stat", "file", "du", "df", "pwd", "echo", "printf", "which", "type", "whereis", "sort", "uniq",
    "cut", "tr", "jq", "yq", "diff", "cmp", "md5sum", "sha256sum", "basename", "dirname", "realpath",
    "readlink", "date", "env", "uname", "whoami", "id", "true", "false", "test", "[", "less", "more",
    "column", "nl", "awk", "sed", "xxd", "hexdump", "od", "strings", "comm", "join", "paste", "tac",
    "rev", "seq", "ps", "free", "uptime", "lsblk", "ldd", "nproc",
}
GIT_READONLY = {"status", "log", "diff", "show", "branch", "blame", "rev-parse", "ls-files", "grep",
                "describe", "shortlog", "remote", "tag", "config", "reflog", "cat-file", "ls-tree"}

HARD_CMD = [
    (r"(^|[;&|(]\s*)(sudo|doas|su|pkexec)\b", "提权命令"),
    (r"\|\s*(sudo\s+)?(sh|bash|zsh|python3?|perl|ruby|node)\b(\s|$)", "管道到解释器执行"),
    (r"\b(curl|wget)\b[^|;&]*\s(-d|--data[\w-]*|-F|--form|-T|--upload-file|--post-data|--post-file)\b", "上传数据"),
    (r"(^|[;&|(]\s*)(ssh|scp|sftp|rsync|nc|ncat|socat|telnet|ftp)\b", "远程连接/传输"),
    (r"(^|[;&|(]\s*)(eval|exec)\b", "eval/exec 动态执行"),
]
HANDOFF_CMD = [
    (r"\bgit\s+(push|commit|reset\s+--hard|clean\b|rebase|filter-branch|update-ref|tag\s+-|merge)", "git 写历史/推送由主代理执行"),
]
APPROVAL_CMD = [
    (r"\bgit\s+branch\s+-D\b|\bgit\s+checkout\s+--\s|\bgit\s+restore\b|\bgit\s+stash\s+(drop|clear)", "丢弃改动的 git 命令"),
    (r"(^|[;&|(]\s*)(systemctl|service|crontab|at|shutdown|reboot|poweroff|halt|mkfs[\w.]*|fdisk|parted|mount|umount|swapoff)\b", "系统管理命令"),
    (r"(^|[;&|(]\s*)(kill|pkill|killall)\b", "结束进程"),
    (r"\bdd\b[^;&|]*\bof=", "dd 写设备/文件"),
    (r"(^|[;&|(]\s*)(apt|apt-get|dpkg|snap|dnf|yum|pacman|flatpak)\b", "系统包管理"),
    (r"\bnpm\s+(i|install|add)\b[^;&|]*\s(-g|--global)\b|\bpip3?\s+install\b[^;&|]*--user\b", "全局安装"),
    (r"\bchmod\s+(-R\s+)?[0-7]*7[0-7]{0,2}\s+/|\bchown\s+-R\b", "大范围改权限"),
    (r"\bdocker\s+(system\s+prune|rm|rmi|volume\s+rm)\b", "删除 docker 资源"),
]


def verdict(decision, reason=None, kind=None, grant=None):
    return {"decision": decision, "reason": reason, "kind": kind, "grant": grant}


ALLOW = verdict(ASK)


def load_grants():
    grants = {"cmd": set(), "write": [], "tool": set()}
    for g in json.loads(os.environ.get("CC_AGY_GRANTS") or "[]"):
        kind, _, value = g.partition(":")
        if kind == "cmd":
            grants["cmd"].add(value.strip())
        elif kind == "write":
            grants["write"].append(norm(value))
        elif kind == "tool":
            grants["tool"].add(value)
    return grants


def emit(decision, reason=None):
    out = {"decision": decision}
    if reason:
        out["reason"] = reason
    print(json.dumps(out, ensure_ascii=False))


def log(entry):
    path = os.environ.get("CC_AGY_GATE_LOG")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def norm(p):
    return os.path.realpath(os.path.expanduser(p))


def inside(path, roots):
    rp = norm(path)
    return any(rp == r or rp.startswith(r.rstrip("/") + "/") for r in roots)


def path_args(args):
    """Collect string values of keys that look like file paths."""
    found = []
    for k, v in (args or {}).items():
        if isinstance(v, str) and re.search(r"(path|file|uri)$", k, re.I) and v.strip():
            found.append(v.removeprefix("file://"))
        elif isinstance(v, list) and re.search(r"(paths|files)$", k, re.I):
            found += [x.removeprefix("file://") for x in v if isinstance(x, str)]
    return found


def secret_hit(text):
    for pat in SECRET_PATTERNS:
        if re.search(pat, text):
            return pat
    return None


def split_segments(cmd):
    # "&" separates commands except inside redirections such as 2>&1 or &>
    return [s.strip() for s in re.split(r"\|\||&&|[;|\n]|(?<![<>])&(?!>)", cmd) if s.strip()]


def redirect_targets(cmd):
    return re.findall(r"(?<![0-9&])>{1,2}\s*([^\s;&|]+)|\btee\s+(?:-a\s+)?([^\s;&|]+)", cmd)


def check_readonly_shell(cmd):
    if re.search(r"\$\(|`", cmd):
        return "只读模式不允许命令替换 $( ) 或反引号"
    for t in redirect_targets(cmd):
        target = t[0] or t[1]
        if target not in ("/dev/null", "&1", "&2"):
            return f"只读模式不允许重定向写入 {target}"
    for seg in split_segments(cmd):
        try:
            words = shlex.split(seg)
        except ValueError:
            words = seg.split()
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        exe = os.path.basename(words[0])
        if exe in ("cd", "export"):
            continue
        if exe == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            if sub not in GIT_READONLY:
                return f"只读模式不允许 git {sub}"
            continue
        if exe not in READONLY_CMDS:
            return f"只读模式不允许命令 {exe}"
        if exe == "find" and re.search(r"\s-(exec|execdir|delete|ok|fprint)", seg):
            return "只读模式不允许 find -exec/-delete"
        if exe == "sed" and re.search(r"\s-i", seg):
            return "只读模式不允许 sed -i"
        if exe == "awk" and re.search(r"system\s*\(|>\s*\"", seg):
            return "只读模式不允许 awk 执行命令或写文件"
    return None


def check_workspace_shell(cmd, roots):
    for pat, why in APPROVAL_CMD:
        if re.search(pat, cmd):
            return f"需审批：{why}"
    for t in redirect_targets(cmd):
        target = t[0] or t[1]
        if target in ("/dev/null", "&1", "&2") or target.startswith("&"):
            continue
        if (target.startswith(("/", "~", "$HOME")) or ".." in target) and not inside(
                target.replace("$HOME", "~"), roots):
            return f"需审批：写入工作区外 {target}"
    for m in re.finditer(r"\brm\b([^;&|]*)", cmd):
        for w in m.group(1).split():
            if w.startswith("-"):
                continue
            if w in ("/", "~", "*", "/*", "~/", "$HOME") or (
                    (w.startswith(("/", "~", "$HOME")) or ".." in w) and not inside(w.replace("$HOME", "~"), roots)):
                return f"需审批：删除工作区外路径 {w}"
    return None


def decide(policy, name, args, roots, grants):
    if policy == "full":
        return ALLOW
    hit = secret_hit(" ".join(path_args(args) + [str(args.get("CommandLine") or ""), str(args.get("Url") or "")]))
    if hit:
        return verdict(DENY, f"禁止访问凭据类文件（匹配 {hit}）", "hard")
    if name in READ_TOOLS:
        return ALLOW
    if name in SHELL_TOOLS:
        if name == "send_command_input":
            if policy == "workspace" or name in grants["tool"]:
                return ALLOW
            return verdict(DENY, "只读模式不允许向运行中的命令写输入", "approval", f"tool:{name}")
        cmd = str(args.get("CommandLine") or args.get("command") or "")
        for pat, why in HARD_CMD:
            if re.search(pat, cmd):
                return verdict(DENY, f"禁止：{why}", "hard")
        for pat, why in HANDOFF_CMD:
            if re.search(pat, cmd):
                return verdict(DENY, f"{why}：不要执行，在结论里给出建议的命令", "handoff", f"cmd:{cmd.strip()}")
        if cmd.strip() in grants["cmd"]:
            return ALLOW
        approval = lambda why: verdict(DENY, why, "approval", f"cmd:{cmd.strip()}")
        cwd = args.get("Cwd") or args.get("cwd")
        if cwd and not inside(cwd, roots):
            return approval(f"需审批：命令工作目录在工作区外 {cwd}")
        if policy == "readonly":
            why = check_readonly_shell(cmd)
        else:
            why = check_workspace_shell(cmd, roots + grants["write"])
        return approval(why) if why else ALLOW
    if name in WRITE_TOOLS:
        paths = path_args(args)
        if not paths:
            return verdict(DENY, "无法确定写入路径，拒绝", "hard")
        allowed = grants["write"] + (roots if policy == "workspace" else [])
        bad = [p for p in paths if not inside(p, allowed)]
        if not bad:
            return ALLOW
        why = "只读模式不允许修改文件" if policy == "readonly" and inside(bad[0], roots) else f"需审批：写入工作区外 {bad[0]}"
        return verdict(DENY, why, "approval", f"write:{norm(bad[0])}")
    if (name in WORKSPACE_EXTRA and policy == "workspace") or name in grants["tool"]:
        return ALLOW
    return verdict(DENY, f"需审批：策略 {policy} 默认不允许工具 {name}", "approval", f"tool:{name}")


def main():
    policy = os.environ.get("CC_AGY_POLICY", "")
    raw = sys.stdin.read()
    if not policy:
        emit(ASK)
        return
    try:
        payload = json.loads(raw)
        call = payload.get("toolCall") or {}
        name, args = call.get("name", ""), call.get("args") or {}
        roots = [norm(p) for p in payload.get("workspacePaths") or []]
        if payload.get("artifactDirectoryPath"):
            roots.append(norm(payload["artifactDirectoryPath"]))
        if policy not in ("readonly", "workspace", "full"):
            v = verdict(DENY, f"未知策略 {policy}", "hard")
        else:
            v = decide(policy, name, args, roots, load_grants())
    except Exception as e:  # fail closed while a policy is active
        name, args, v = "?", {}, verdict(DENY, f"gate 内部错误：{e}", "hard")
    log({"ts": time.time(), "policy": policy, "tool": name, **v,
         "args": json.dumps(args, ensure_ascii=False)[:300]})
    emit(v["decision"], v["reason"])


if __name__ == "__main__":
    main()
