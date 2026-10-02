# /// script
# requires-python = ">=3.10"
# dependencies = ["rich"]
# ///
"""Shows how the context window of a Claude Code session fills up, turn by turn.

Exact: context size per API call (input + cache_read + cache_creation from `usage`).
Measured: thinking tokens (from `usage.output_tokens`).
Estimated: how each call's growth splits across individual contents (characters / tokens,
calibrated per session).
"""
import argparse
import glob as globmod
import json
import os
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

CHARS_PER_TOKEN = 3.6
IMAGE_TOKENS = 1600

CATEGORIES = {  # name -> color
    "System+Tools": "grey50",
    "Compact-Summary": "pink1",
    "File reads": "green",
    "Bash": "yellow",
    "Search": "cyan",
    "Web": "blue",
    "Agent": "magenta",
    "MCP/other tools": "bright_magenta",
    "Attachments": "bright_black",
    "Skills/Meta": "orange3",
    "User": "bright_white",
    "Response": "red",
    "Tool calls": "light_coral",
    "Code writing": "dark_red",
    "Thinking": "medium_purple",
    "Rest": "grey30",
}

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

TOOL_CATEGORY = {
    "Read": "File reads", "NotebookRead": "File reads",
    "Bash": "Bash", "BashOutput": "Bash",
    "Grep": "Search", "Glob": "Search", "LS": "Search",
    "WebFetch": "Web", "WebSearch": "Web",
    "Agent": "Agent", "Task": "Agent",
}


# Commands whose output is file content
FULL_READERS = {"cat", "bat", "nl", "less", "more", "tac", "xxd", "strings"}
PART_READERS = {"head", "tail", "sed", "awk", "jq", "cut", "sort", "uniq", "column", "diff", "od"}
SEARCHERS = {"grep", "egrep", "fgrep", "rg", "ag"}
NEUTRAL = {"cd", "echo", "printf", "true", ":", "wc", "ls"}
# Options that consume an argument
OPT_ARG = {
    "head": {"-n", "-c"}, "tail": {"-n", "-c"}, "sed": {"-e", "-f"}, "awk": {"-F", "-v", "-f"},
    "cut": {"-d", "-f", "-c"}, "sort": {"-k", "-t"}, "jq": {"--arg", "--argjson"},
    "grep": {"-e", "-f", "-A", "-B", "-C", "-m", "--include", "--exclude", "--exclude-dir"},
    "rg": {"-e", "-g", "-t", "-A", "-B", "-C", "-m", "--glob", "--type"},
}
# First positional argument is a script/pattern, not a file
SCRIPT_FIRST = {"sed", "awk", "jq", "grep", "egrep", "fgrep", "rg", "ag"}
PY_OPEN = re.compile(r"""(?:open|read_csv|read_parquet|read_json|load_dataset|Path)\(\s*[rf]?['"]([^'"\n]+)['"]""")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\b", re.S)


def _resolve(arg: str, cwd: str) -> list[str]:
    arg = os.path.expanduser(arg)
    full = arg if os.path.isabs(arg) else os.path.normpath(os.path.join(cwd, arg))
    if any(ch in arg for ch in "*?["):
        hits = sorted(globmod.glob(full))
        return [h for h in hits if os.path.isfile(h)][:20] or [full]
    return [full]


def _looks_like_path(a: str) -> bool:
    return not a.startswith("-") and "$" not in a and "`" not in a and (
        "/" in a or "." in a or os.path.exists(a))


def bash_file_access(cmd: str, cwd: str):
    """Heuristic: which files does a Bash command read or search?

    Returns ([(path, mode, via)], only_reading, final_cwd). mode: full | partial | searched | script
    """
    out = []
    # Scan heredoc bodies (mostly Python) separately for open(...)
    for m in HEREDOC.finditer(cmd):
        for f in PY_OPEN.findall(m.group(2)):
            out += [(p, "script", "python") for p in _resolve(f, cwd)]
    body = HEREDOC.sub("", cmd).replace("\\\n", " ")
    body = re.sub(r"\d*>{1,2}\s*(?:&\d+|[^\s;|&]+)", " ", body)  # redirections (2>&1, > out)
    for f in re.findall(r"""python3?\s+-c\s+(['"])(.*?)\1""", body, re.S):
        for g in PY_OPEN.findall(f[1]):
            out += [(p, "script", "python") for p in _resolve(g, cwd)]
    only_reading = True
    for line in body.splitlines():
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=";&|")
            lex.whitespace_split = True
            toks = list(lex)
        except ValueError:
            toks = line.split()
        # Split into statements (;, &&, ||) and pipeline stages (|)
        stmts, cur, stage = [], [], []
        for t in toks + [";"]:
            if t in (";", "&&", "||", "&", ";;"):
                if stage: cur.append(stage)
                if cur: stmts.append(cur)
                cur, stage = [], []
            elif t == "|":
                if stage: cur.append(stage)
                stage = []
            else:
                stage.append(t)
        for pipe in stmts:
            for si, st in enumerate(pipe):
                while st and (re.match(r"^\w+=", st[0]) or st[0] in ("sudo", "command", "time", "nice")):
                    st = st[1:]
                if st and st[0] == "timeout":
                    st = st[2:]
                if not st:
                    continue
                prog = os.path.basename(st[0])
                if prog == "cd" and len(st) > 1 and len(pipe) == 1:
                    cwd = _resolve(st[1], cwd)[0]
                    continue
                if prog in NEUTRAL:
                    continue
                if prog not in FULL_READERS | PART_READERS | SEARCHERS:
                    if si == 0:
                        only_reading = False
                    continue
                if si > 0:  # reads from the pipe, not from files
                    continue
                pos, skip, script_given = [], False, False
                for a in st[1:]:
                    if skip:
                        skip = False
                        continue
                    if a in OPT_ARG.get(prog, ()) or (prog in ("rg", "ag", "egrep", "fgrep") and a in OPT_ARG["grep"]):
                        skip = True
                        script_given |= a in ("-e", "-f")
                        continue
                    if a.startswith("-") and len(a) > 1:
                        continue
                    pos.append(a)
                if prog in SCRIPT_FIRST and not script_given and pos:
                    pos = pos[1:]
                if prog in SEARCHERS:
                    mode = "searched"
                elif prog in FULL_READERS and len(pipe) == 1:
                    mode = "full"
                else:
                    mode = "partial"
                for a in pos:
                    if _looks_like_path(a):
                        out += [(p, mode, prog) for p in _resolve(a, cwd)]
    return out, only_reading, cwd


SUBCMD = {"git", "npm", "npx", "uv", "pip", "pip3", "docker", "gh", "cargo", "kubectl", "pnpm", "yarn",
          "gcloud", "systemctl", "make", "poetry", "conda", "apt", "brew"}
PREFIX_SKIP = {"cd", "export", "source", ".", "set", "sleep", "echo", "printf", "true", "mkdir", "unset"}


def bash_main_command(cmd: str) -> str:
    """Grouping key, e.g. 'git status', 'uv run', 'python (script)', 'grep'."""
    heredoc = HEREDOC.search(cmd)
    body = HEREDOC.sub("", cmd)
    body = re.sub(r"\d*>{1,2}\s*(?:&\d+|[^\s;|&]+)", " ", body.replace("\\\n", " "))
    firsts = []
    for line in body.splitlines():
        for stmt in re.split(r"&&|\|\||;", line):
            stage = stmt.split("|")[0].split()
            while stage and (re.match(r"^\w+=", stage[0]) or stage[0] in ("sudo", "time", "nice", "command")):
                stage = stage[1:]
            if stage and stage[0] == "timeout":
                stage = stage[2:]
            if stage:
                firsts.append(stage)
    if not firsts:
        return "(empty)"
    st = next((f for f in firsts if os.path.basename(f[0]) not in PREFIX_SKIP), firsts[0])
    prog = os.path.basename(st[0])
    if re.match(r"python[\d.]*$", prog) or prog.endswith("/python"):
        if heredoc or "-" in st[1:2]:
            return "python (script)"
        if "-c" in st:
            return "python -c"
        if "-m" in st:
            i = st.index("-m")
            return f"python -m {st[i + 1] if i + 1 < len(st) else ''}".strip()
        return "python " + (os.path.basename(st[1]) if len(st) > 1 else "")
    if prog in ("for", "while", "until", "if", "case"):
        return f"{prog} (shell loop)" if prog != "if" else "if (conditional)"
    if prog in ("node", "bash", "sh") and heredoc:
        return f"{prog} (script)"
    if prog in SUBCMD:
        sub = next((a for a in st[1:] if not a.startswith("-")), "")
        if prog in ("uv", "npm", "pnpm", "yarn") and sub == "run":
            nxt = st[st.index("run") + 1:] if "run" in st else []
            nxt = [a for a in nxt if not a.startswith("-")]
            return f"{prog} run {os.path.basename(nxt[0]) if nxt else ''}".strip()
        return f"{prog} {sub}".strip()
    return prog


SEARCH_MODES = {"match", "match+context", "name only"}
GREP_LINE = re.compile(r"^(?P<path>[^\n:]+?)(?:(?P<sep>[:-])(?P<num>\d+)(?P=sep)|:)")


def _within(cand: str, searched: list[str]) -> bool:
    return any(cand == s_ or cand.startswith(s_.rstrip("/") + "/") for s_ in searched)


def attribute_search(output: str, searched: list[str], cwd: str, single_file_ok: bool = True):
    """Attributes grep output lines to the files that matched.

    Returns {path: (chars, mode)}. Files without matches are absent, because nothing
    from them entered the context.
    """
    hits: dict[str, list] = {}
    lines = [l for l in output.splitlines() if l.strip() and l != "--"]
    if not lines:
        return {}
    # A single file was searched: grep prints no path prefix
    s0 = searched[0]
    is_file = os.path.isfile(s0) or (not os.path.exists(s0) and "." in os.path.basename(s0))
    if single_file_ok and len(searched) == 1 and is_file \
            and not any(GREP_LINE.match(l) and _within(_resolve(GREP_LINE.match(l)["path"], cwd)[0], searched) for l in lines[:5]):
        return {searched[0]: (sum(map(len, lines)), "match")}
    for l in lines:
        m = GREP_LINE.match(l)
        if m:
            cand, mode = _resolve(m["path"], cwd)[0], "match+context" if m["sep"] == "-" else "match"
        else:  # grep -l / files_with_matches: the line is just a path
            cand, mode = _resolve(l.strip(), cwd)[0], "name only"
        if not _within(cand, searched):
            continue
        h = hits.setdefault(cand, [0, mode])
        h[0] += len(l)
        if mode == "match+context" or h[1] == "name only":
            h[1] = mode
    return {k: tuple(v) for k, v in hits.items()}


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


_calib = 1.0  # calibrated per session, see analyze()


def est(chars: int) -> float:
    return chars / CHARS_PER_TOKEN * _calib


def content_size(content) -> float:
    """Estimated tokens of a tool_result or message content."""
    if isinstance(content, str):
        return est(len(content))
    total = 0.0
    for b in content or []:
        t = b.get("type")
        if t == "text":
            total += est(len(b.get("text", "")))
        elif t == "image":
            total += IMAGE_TOKENS
        elif t == "tool_reference":
            total += 5
        else:
            total += est(len(json.dumps(b, ensure_ascii=False)))
    return total


def short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


class Item:
    __slots__ = ("cat", "label", "est", "tok", "files")

    def __init__(self, cat, label, est_, files=None):
        # files: [(path, mode, via, weight)] - tokens are split by weight
        self.cat, self.label, self.est, self.tok, self.files = cat, label, est_, 0.0, files or []


class Turn:
    def __init__(self, idx, prompt, ts):
        self.idx, self.prompt, self.ts = idx, prompt, ts
        self.calls = 0
        self.ctx_start = None
        self.ctx_end = 0
        self.items: list[Item] = []
        self.comp: dict[str, float] = {}
        self.compacted = False


def analyze(path: Path):
    """Two passes: first measure a calibration factor on calls without thinking
    (where the growth can be attributed exactly), then estimate all calls with it."""
    global _calib
    _calib = 1.0
    first = _analyze(path)
    sc = first[-1].get("scales", [])
    weight = sum(e for _, e in sc)
    _calib = min(max(sum(x * e for x, e in sc) / weight, 0.5), 3.0) if weight else 1.0
    result = _analyze(path)
    result[-1]["calib"] = _calib
    result[-1]["calib_n"] = len(sc)
    _calib = 1.0
    return result


def _analyze(path: Path):
    tool_uses = {}  # id -> (name, input)
    seen_req = set()
    pending: list[Item] = []
    comp = defaultdict(float)
    files = defaultdict(lambda: {"reads": 0, "searches": 0, "tok": 0.0, "segs": set(), "via": set(), "read_via": set(), "modes": set()})
    startup = None  # (baseline, items, ctx) of the first API call
    skills = []  # (turn, name, tokens-item)
    thinking = {}  # requestId -> [item, output_tokens, thinking_tokens, visible tokens]
    stats = {"think_measured": 0.0, "think_found": 0.0}
    tool_log = {}  # tool_use_id -> {name, input, turn, call, result, error}
    stats["tools"] = tool_log
    turns: list[Turn] = []
    turn = Turn(0, "(session start)", "")
    turns.append(turn)
    prev_ctx = None
    segment = 0
    compact_pending = False
    calls = []  # (call_idx, turn_idx, ctx)

    def new_turn(prompt, ts):
        nonlocal turn
        turn = Turn(len(turns), prompt, ts)
        turns.append(turn)

    for line in path.open():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("isSidechain"):
            continue
        typ = d.get("type")

        if typ == "system" and d.get("subtype") == "compact_boundary":
            compact_pending = True
            segment += 1
            continue

        if typ == "attachment":
            a = d.get("attachment", {})
            at = a.get("type", "?")
            if at in ("queued_command",):
                continue
            size = est(len(json.dumps(a, ensure_ascii=False))) * 0.8
            if at == "file":
                fp = a.get("filename", "?")
                pending.append(Item("File reads", f"@{fp}", size, [(fp, "full", "@", 1.0)]))
                continue
            pending.append(Item("Attachments", attachment_label(a), size))
            continue

        msg = d.get("message")
        if not isinstance(msg, dict):
            continue

        if typ == "user":
            c = msg.get("content")
            if d.get("isCompactSummary"):
                pending.append(Item("Compact-Summary", "summary", content_size(c)))
                continue
            if isinstance(c, str) or (isinstance(c, list) and c and c[0].get("type") == "text"):
                text = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c)
                if d.get("isMeta"):
                    m = re.match(r"Base directory for this skill: (\S+)", text)
                    label = f"Skill: {Path(m.group(1)).name}" if m else short(text, 50)
                    it = Item("Skills/Meta", label, content_size(c))
                    if m:
                        skills.append((turn, it))
                    pending.append(it)
                else:
                    if not text.startswith("[Request interrupted"):
                        new_turn(text, d.get("timestamp", ""))
                    pending.append(Item("User", short(text, 50), content_size(c)))
                continue
            for b in c or []:
                if b.get("type") != "tool_result":
                    continue
                name, inp = tool_uses.get(b.get("tool_use_id"), ("?", {}))
                size = content_size(b.get("content"))
                cat = TOOL_CATEGORY.get(name, "MCP/other tools")
                fl = []
                if name == "Read":
                    fp = inp.get("file_path", "?")
                    part = bool(inp.get("offset") or inp.get("limit"))
                    rng = f" [{inp.get('offset', 0)}+{inp.get('limit', '')}]" if part else ""
                    label = fp + rng
                    fl = [(fp, "partial" if part else "full", "Read", 1.0)]
                elif name == "Bash":
                    label = "$ " + short(inp.get("command", ""), 70)
                    acc, only_reading, bcwd = bash_file_access(inp.get("command", ""), d.get("cwd") or os.getcwd())
                    out = result_text(b.get("content"))
                    reads = [(p_, m_, v_) for p_, m_, v_ in acc if m_ != "searched"]
                    searched = list(dict.fromkeys(p_ for p_, m_, _ in acc if m_ == "searched"))
                    via_s = next((v_ for _, m_, v_ in acc if m_ == "searched"), "grep")
                    hits = attribute_search(out, searched, bcwd, single_file_ok=not reads) if searched else {}
                    hit_chars = sum(c_ for c_, _ in hits.values())
                    rest_chars = max(len(out) - hit_chars, 0)
                    fl = [(p_, m_, via_s, c_) for p_, (c_, m_) in hits.items()]
                    fl += [(p_, m_, v_, rest_chars / len(reads) or 1.0) for p_, m_, v_ in reads]
                    if acc and only_reading:
                        cat = "File reads" if reads else "Search"
                elif name == "Grep":
                    label = f"Grep {short(inp.get('pattern', ''), 40)}"
                    base = inp.get("path") or d.get("cwd") or os.getcwd()
                    hits = attribute_search(result_text(b.get("content")), [_resolve(base, d.get("cwd") or "/")[0]],
                                            d.get("cwd") or os.getcwd())
                    fl = [(p_, m_, "Grep", c_) for p_, (c_, m_) in hits.items()]
                elif name == "Glob":
                    label = f"Glob {short(inp.get('pattern', ''), 40)}"
                elif name in ("WebFetch", "WebSearch"):
                    label = f"{name} {short(inp.get('url') or inp.get('query', ''), 60)}"
                elif name in ("Agent", "Task"):
                    label = f"Agent: {short(inp.get('description', ''), 50)}"
                else:
                    label = name
                it = Item(cat, label, size, fl)
                pending.append(it)
                if (log := tool_log.get(b.get("tool_use_id"))) is not None:
                    log["result"], log["error"] = it, bool(b.get("is_error"))
            continue

        if typ == "assistant":
            req = d.get("requestId") or msg.get("id")
            if req not in seen_req:
                seen_req.add(req)
                u = msg.get("usage") or {}
                ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                if ctx > 0:
                    reset = prev_ctx is None or compact_pending or ctx < prev_ctx * 0.7
                    if reset:
                        comp.clear()
                        delta = ctx
                    else:
                        delta = ctx - prev_ctx
                    # Thinking is measured (output_tokens), everything else estimated from characters
                    fixed = sum(i.est for i in pending if i.cat == "Thinking")
                    var = sum(i.est for i in pending if i.cat != "Thinking")
                    t_scale, rest = 1.0, 0.0
                    if reset:
                        # Invisible baseline: system prompt + tool schemas
                        comp["System+Tools"] += max(delta - fixed - var, 0)
                        scale = t_scale = min(1.0, delta / (fixed + var)) if fixed + var else 1.0
                    elif not fixed:
                        # No thinking in between: distribute the exact growth proportionally
                        scale = delta / var if var else 1.0
                        rest = 0 if var else delta
                        if var > 200:
                            stats.setdefault("scales", []).append((scale * _calib, var / _calib))
                    elif delta - fixed >= var:
                        scale, rest = 1.0, delta - fixed - var
                    elif delta >= fixed:
                        scale = (delta - fixed) / var if var else 1.0
                    else:
                        # Less growth than measured thinking: thinking not (fully) kept in context
                        scale = min(1.0, delta / var) if var else 1.0
                        t_scale = max(delta - var * scale, 0) / fixed
                    stats["think_measured"] += fixed
                    stats["think_found"] += fixed * t_scale
                    for it in pending:
                        it.tok = it.est * (t_scale if it.cat == "Thinking" else scale)
                        comp[it.cat] += it.tok
                        wsum = sum(w for *_, w in it.files) or 1
                        for fp, mode, via, w in it.files:
                            f = files[fp]
                            f["searches" if mode in SEARCH_MODES else "reads"] += 1
                            f["tok"] += it.tok * w / wsum
                            f["segs"].add(segment)
                            f["via"].add(via)
                            if mode not in SEARCH_MODES:
                                f["read_via"].add(via)
                            f["modes"].add(mode)
                    if rest:
                        comp["Rest"] += rest
                    if startup is None:
                        startup = (comp["System+Tools"], list(pending), ctx)
                    turn.items.extend(pending)
                    pending = []
                    if turn.ctx_start is None:
                        turn.ctx_start = prev_ctx or 0
                    turn.compacted |= compact_pending and prev_ctx is not None
                    compact_pending = False
                    turn.calls += 1
                    turn.ctx_end = ctx
                    turn.comp = dict(comp)
                    calls.append((len(calls), turn.idx, ctx))
                    prev_ctx = ctx
            u = msg.get("usage") or {}
            for b in msg.get("content") or []:
                t = b.get("type")
                vis = 0.0
                if t == "tool_use":
                    name = b.get("name")
                    tool_uses[b["id"]] = (name, b.get("input") or {})
                    vis = est(len(json.dumps(b.get("input"), ensure_ascii=False)))
                    if name in WRITE_TOOLS:
                        fp = (b.get("input") or {}).get("file_path") or (b.get("input") or {}).get("notebook_path", "?")
                        call_it = Item("Code writing", f"{name} {fp}", vis)
                    else:
                        call_it = Item("Tool calls", f"→ {name}", vis)
                    pending.append(call_it)
                    tool_log[b["id"]] = {"name": name, "input": b.get("input") or {}, "turn": turn,
                                         "call": call_it, "result": None, "error": False}
                elif t == "text":
                    vis = est(len(b.get("text", "")))
                    pending.append(Item("Response", short(b.get("text", ""), 50), vis))
                elif t == "thinking":
                    # Text is empty in the log -> size from usage: thinking_tokens or output - visible
                    if req not in thinking:
                        it = Item("Thinking", "Thinking", 0)
                        tt = (u.get("output_tokens_details") or {}).get("thinking_tokens")
                        thinking[req] = [it, u.get("output_tokens", 0), tt, 0.0]
                        pending.append(it)
                if req in thinking:
                    rec = thinking[req]
                    rec[3] += vis
                    rec[0].est = rec[2] if rec[2] else max(rec[1] - rec[3], 0)

    turns = [t for t in turns if t.calls]
    return turns, files, calls, segment, startup, skills, stats


def attachment_label(a: dict) -> str:
    at = a.get("type", "?")
    if at == "skill_listing":
        return f"<skill_listing> {a.get('content', '').count(chr(10) + '- ') + 1} skills (name+description)"
    if at in ("deferred_tools_delta", "deferred_tools_record"):
        return f"<{at}> {len(a.get('addedNames') or a.get('names') or [])} tool names"
    if at == "mcp_instructions_delta":
        return f"<mcp_instructions> {', '.join(a.get('addedNames', []))}"
    if at == "agent_listing_delta":
        return f"<agent_listing> {len(a.get('addedTypes', []))} agent types"
    if at in ("nested_memory", "memory", "claude_md"):
        return f"<{at}> {a.get('path') or a.get('filename', '')}"
    if at == "edited_text_file":
        return f"<edited_text_file> {a.get('filename', '')}"
    return f"<{at}>"


def bar(comp: dict, width: int, scale_max: float) -> Text:
    total = sum(comp.values())
    n = max(1, round(width * total / scale_max)) if scale_max else 0
    if not total:
        return Text()
    # Assign cells per category independently (largest remainder) so small categories
    # don't flicker in and out depending on how their neighbours round.
    exact = {c: n * comp.get(c, 0) / total for c in CATEGORIES}
    cells = {c: int(x) for c, x in exact.items()}
    by_rest = sorted((c for c in CATEGORIES if exact[c] - cells[c] >= 0.5), key=lambda c: cells[c] - exact[c])
    for c in by_rest[: n - sum(cells.values())]:
        cells[c] += 1
    if (missing := n - sum(cells.values())) > 0:
        cells[max(exact, key=exact.get)] += missing
    out = Text()
    for cat, color in CATEGORIES.items():
        out.append("█" * cells[cat], style=color)
    return out


def fmt(n) -> str:
    n = float(n)
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def call_label(log: dict) -> str:
    inp, name = log["input"], log["name"]
    if name == "Bash":
        return "$ " + inp.get("command", "")
    for k in ("file_path", "notebook_path", "pattern", "url", "query", "description", "skill", "prompt"):
        if inp.get(k):
            return f"{name} {inp[k]}"
    return f"{name} {json.dumps(inp, ensure_ascii=False)}"


def render_tools(con: Console, tool_log: dict, top: int):
    logs = list(tool_log.values())
    if not logs:
        return
    res = lambda l: l["result"].tok if l["result"] else 0.0
    by = defaultdict(list)
    for l in logs:
        by[l["name"]].append(l)
    t = Table(title=f"Tool calls ({len(logs)} total)", title_justify="left")
    for col in ("Tool", "Count", "Errors", "Call≈", "Result≈", "Avg result", "Largest result"):
        t.add_column(col, justify="left" if col in ("Tool", "Largest result") else "right",
                     max_width=60 if col == "Largest result" else None, no_wrap=True)
    for name, ls in sorted(by.items(), key=lambda kv: -sum(res(l) + l["call"].tok for l in kv[1])):
        big = max(ls, key=res)
        err = sum(l["error"] for l in ls)
        t.add_row(name, str(len(ls)), Text(str(err), style="red") if err else "",
                  fmt(sum(l["call"].tok for l in ls)), fmt(sum(map(res, ls))), fmt(sum(map(res, ls)) / len(ls)),
                  Text(f"{fmt(res(big))} T{big['turn'].idx} {short(call_label(big), 50)}"))
    con.print(t)

    bash = [l for l in logs if l["name"] == "Bash"]
    if not bash:
        return
    groups = defaultdict(list)
    for l in bash:
        groups[bash_main_command(l["input"].get("command", ""))].append(l)
    bt = Table(title=f"Bash commands by main command ({len(bash)} calls, {len(groups)} kinds)", title_justify="left")
    for col in ("Command", "Count", "Errors", "Output≈", "Example"):
        bt.add_column(col, justify="left" if col in ("Command", "Example") else "right",
                      max_width=70 if col == "Example" else None, no_wrap=True)
    for key, ls in sorted(groups.items(), key=lambda kv: (-len(kv[1]), -sum(map(res, kv[1]))))[:max(top, 20)]:
        err = sum(l["error"] for l in ls)
        ex = min((l["input"].get("command", "") for l in ls), key=len)
        bt.add_row(Text(key), str(len(ls)), Text(str(err), style="red") if err else "",
                   fmt(sum(map(res, ls))), Text(short(ex, 70)))
    con.print(bt)
    if len(groups) > max(top, 20):
        con.print(f"[dim]… {len(groups) - max(top, 20)} more command kinds[/]")

    gt = Table(title="Largest Bash outputs", title_justify="left")
    for col in ("Turn", "Output≈", "Command"):
        gt.add_column(col, justify="left" if col == "Command" else "right", max_width=100 if col == "Command" else None,
                      no_wrap=True)
    for l in sorted(bash, key=lambda l: -res(l))[:10]:
        gt.add_row(str(l["turn"].idx), fmt(res(l)), Text(short(l["input"].get("command", ""), 100)))
    con.print(gt)


def render_calls(path: Path, stats: dict, flt: str):
    """All tool calls in chronological order."""
    con = Console()
    logs = [l for l in stats["tools"].values() if flt == "all" or l["name"].lower() == flt.lower()]
    t = Table(title=f"{path.name}: {len(logs)} tool calls" + ("" if flt == "all" else f" ({flt})"), title_justify="left")
    for col in ("#", "Turn", "Tool", "Result≈", "Call"):
        t.add_column(col, justify="left" if col in ("Tool", "Call") else "right", overflow="fold")
    for i, l in enumerate(logs, 1):
        res = l["result"].tok if l["result"] else 0
        name = Text(l["name"], style="red" if l["error"] else "")
        t.add_row(str(i), str(l["turn"].idx), name, fmt(res), Text(short(call_label(l), 160)))
    con.print(t)
    con.print("[dim]red = tool call returned an error[/]")


def hbar(value: float, max_value: float, width: int, color: str) -> Text:
    """Horizontal bar using eighth blocks for fine resolution."""
    if max_value <= 0 or value <= 0:
        return Text("")
    eighths = round(width * 8 * value / max_value)
    full, part = divmod(eighths, 8)
    return Text("█" * full + ("" if not part else " ▏▎▍▌▋▊▉"[part]), style=color)


def render(path: Path, turns, files, calls, segments, startup, skills, stats, detail: int | None, top: int):
    con = Console()
    peak = max((t.ctx_end for t in turns), default=0)
    header = (f"[bold]{escape(path.name)}[/]  ·  {len(turns)} Turns · {len(calls)} API-Calls · "
              f"{segments} Compacts · Peak {fmt(peak)} Tokens")
    legend = Text("Legend: ")
    for cat, color in CATEGORIES.items():
        legend.append("■ ", style=color).append(cat + "  ")

    if detail is not None:
        con.print(header + "\n")
        con.print(legend)
        t = next((t for t in turns if t.idx == detail), None)
        if not t:
            con.print(f"Turn {detail} not found")
            return
        con.print(f"\n[bold]Turn {t.idx}[/]: {escape(short(t.prompt, 200))}")
        tab = Table(show_lines=False)
        tab.add_column("Category"); tab.add_column("Content", overflow="fold"); tab.add_column("Tokens≈", justify="right")
        for it in sorted(t.items, key=lambda i: -i.tok):
            tab.add_row(Text(it.cat, style=CATEGORIES[it.cat]), Text(it.label), fmt(it.tok))
        con.print(tab)
        return

    # --- Top: background details (startup context, skills, tools) ---
    if startup:
        base, items, ctx = startup
        st = Table(title=f"Startup context (first API call: {fmt(ctx)} tokens, exact)", title_justify="left")
        st.add_column("Content"); st.add_column("Tokens≈", justify="right"); st.add_column("%", justify="right")
        st.add_row(Text("invisible: system prompt, tool schemas, CLAUDE.md/AGENTS.md, MCP tools", style="grey50"),
                   fmt(base), f"{100 * base / ctx:.0f}")
        for it in sorted(items, key=lambda i: -i.tok):
            st.add_row(Text(f"[{it.cat}] {it.label}"), fmt(it.tok), f"{100 * it.tok / ctx:.0f}")
        con.print(st)
    if skills:
        sk = Table(title="Loaded skills (full skill text in context)", title_justify="left")
        sk.add_column("Turn", justify="right"); sk.add_column("Skill"); sk.add_column("Tokens≈", justify="right")
        for t, it in skills:
            sk.add_row(str(t.idx), Text(it.label.removeprefix("Skill: ")), fmt(it.tok))
        con.print(sk)
    render_tools(con, stats["tools"], top)

    # --- Bottom: the most important part, visible without scrolling ---
    con.rule()
    con.print(header)
    con.print(legend)
    tab = Table(title="Context per turn (as of the end of each turn)", title_justify="left")
    tab.add_column("#", justify="right")
    tab.add_column("Prompt", max_width=34, no_wrap=True)
    tab.add_column("Calls", justify="right")
    tab.add_column("Context", justify="right")
    tab.add_column("Δ", justify="right")
    tab.add_column("Composition", no_wrap=True)
    tab.add_column("Largest addition", max_width=40, no_wrap=True)
    for t in turns:
        delta = t.ctx_end - (t.ctx_start or 0)
        big = max(t.items, key=lambda i: i.tok, default=None)
        mark = "[bold red]⟳ [/]" if t.compacted else ""
        tab.add_row(str(t.idx), mark + escape(short(t.prompt, 34)), str(t.calls), fmt(t.ctx_end),
                    ("+" if delta >= 0 else "") + fmt(delta) if delta >= 0 else f"[red]-{fmt(-delta)}[/]",
                    bar(t.comp, 40, peak),
                    Text(f"{fmt(big.tok)} {short(big.label, 30)}") if big else "")
    con.print(tab)

    last = turns[-1].comp if turns else {}
    peak_turn = max(turns, key=lambda t: t.ctx_end)
    differs = peak_turn is not turns[-1]
    pt, lt = sum(peak_turn.comp.values()) or 1, sum(last.values()) or 1
    title = f"Composition at peak (turn {peak_turn.idx})" + (" and at the end" if differs else "")
    ct = Table(title=title, title_justify="left")
    ct.add_column("Category"); ct.add_column("Peak", justify="right"); ct.add_column("%", justify="right")
    ct.add_column("Share at peak", no_wrap=True, min_width=30)
    if differs:
        ct.add_column("End", justify="right"); ct.add_column("%", justify="right")
        ct.add_column("Share at end", no_wrap=True, min_width=20)
    max_a = max(peak_turn.comp.values(), default=0)
    max_b = max(last.values(), default=0)
    for cat, color in CATEGORIES.items():
        a, b = peak_turn.comp.get(cat, 0), last.get(cat, 0)
        if not (a or b):
            continue
        row = [Text(cat, style=color), fmt(a), f"{100 * a / pt:.0f}", hbar(a, max_a, 30, color)]
        if differs:
            row += [fmt(b), f"{100 * b / lt:.0f}", hbar(b, max_b, 20, color)]
        ct.add_row(*row)
    con.print(ct)

    home = str(Path.home())
    ft = Table(title=f"Files looked at (top {top} by tokens)", title_justify="left")
    ft.add_column("File", overflow="fold"); ft.add_column("reads", justify="right")
    ft.add_column("matches", justify="right"); ft.add_column("via"); ft.add_column("extent")
    ft.add_column("Tokens≈", justify="right"); ft.add_column("segm.", justify="right")
    for fp, f in sorted(files.items(), key=lambda kv: -kv[1]["tok"])[:top]:
        reads = Text(str(f["reads"]), style="bold red" if f["reads"] > 1 else "")
        ft.add_row(Text(fp.replace(home, "~")), reads, str(f["searches"] or ""),
                   ", ".join(sorted(f["via"])), ", ".join(sorted(f["modes"])),
                   fmt(f["tok"]), ",".join(map(str, sorted(f["segs"]))))
    con.print(ft)
    read = [f for f in files.values() if f["reads"]]
    multi = [f for f in read if f["reads"] > 1]
    waste = sum(f["tok"] * (f["reads"] - 1) / (f["reads"] + f["searches"]) for f in multi)
    via_bash = sum(1 for f in read if f["read_via"] - {"Read", "@"})
    con.print(f"{len(read)} files read ({via_bash} of them via Bash/script), "
              f"{len(files) - len(read)} only via search matches, {len(multi)} read more than once. "
              f"Tokens spent on repeated reads ≈ {fmt(waste)}")

    if stats["think_measured"]:
        con.print(f"Thinking: {fmt(stats['think_measured'])} tokens generated (usage), "
                  f"{100 * stats['think_found'] / stats['think_measured']:.0f} % of it found again in context growth")
    cpt = CHARS_PER_TOKEN / stats.get("calib", 1.0)
    con.print(f"[dim]Context size exact from usage; thinking from usage.output_tokens; remaining split estimated "
              f"({cpt:.1f} chars/token, calibrated on {stats.get('calib_n', 0)} calls without thinking). "
              "Turn details: --turn N · all tool calls: --calls · invisible part today: --live-context[/]")


def live_context(cwd: str):
    """`claude -p /context` in the current configuration (not historical!)."""
    con = Console()
    try:
        r = subprocess.run(["claude", "-p", "/context"], cwd=cwd if os.path.isdir(cwd) else None,
                           capture_output=True, text=True, timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        con.print(f"[red]claude -p /context failed: {e}[/]")
        return
    rows = re.findall(r"^\|\s*([^|]+?)\s*\|\s*([\d.]+k?)\s*\|\s*([\d.]+%)\s*\|$", r.stdout, re.M)
    t = Table(title=f"/context now in {cwd} (current configuration, not historical)", title_justify="left")
    t.add_column("Category"); t.add_column("Tokens", justify="right"); t.add_column("Note")
    for cat, tok, _ in rows:
        hint = "only names in context, schemas loaded on demand" if "deferred" in cat else ""
        if cat not in ("Free space", "Autocompact buffer"):
            t.add_row(cat, tok, hint)
    con.print(t)
    mem = re.search(r"### Memory files\n(.*?)(\n###|\Z)", r.stdout, re.S | re.I)
    if mem:
        con.print(Text("Memory files (CLAUDE.md / AGENTS.md), as loaded today:\n" + mem.group(1).strip()))


def session_info(p: Path) -> dict:
    """Quick summary: title, first prompt, number of prompts, context peak."""
    title, first, prompts, peak, start = "", "", 0, 0, ""
    for line in p.open():
        if '"ai-title"' in line or '"aiTitle"' in line:
            try:
                title = json.loads(line).get("aiTitle") or title
            except json.JSONDecodeError:
                pass
        elif '"type":"user"' in line and '"isMeta"' not in line and '"tool_result"' not in line:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = d.get("message", {}).get("content")
            text = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if b.get("type") == "text")
            if text and not text.startswith(("[Request interrupted", "<")) and not d.get("isCompactSummary"):
                prompts += 1
                first = first or text
                start = start or d.get("timestamp", "")
        elif '"cache_read_input_tokens"' in line:
            m = re.search(r'"cache_read_input_tokens":(\d+),"cache_creation_input_tokens":(\d+)', line) or \
                re.search(r'"cache_creation_input_tokens":(\d+),"cache_read_input_tokens":(\d+)', line)
            if m:
                peak = max(peak, int(m.group(1)) + int(m.group(2)))
    return {"title": title, "first": first, "prompts": prompts, "peak": peak, "start": start}


def project_dir(arg: str, root: Path) -> list[Path]:
    """Project folders for a path (~/research/x) or a name fragment (energy-platform)."""
    full = os.path.abspath(os.path.expanduser(arg))
    exact = root / re.sub(r"[^A-Za-z0-9]", "-", full)
    if exact.is_dir():
        return [exact]
    return [d for d in root.iterdir() if d.is_dir() and arg in d.name]


def pick(cands: list[Path]) -> Path:
    con = Console()
    t = Table(title="Sessions (newest first)", title_justify="left")
    for col, j in [("#", "right"), ("Start", "left"), ("Project", "left"), ("Prompts", "right"),
                   ("Peak", "right"), ("Title / first prompt", "left")]:
        t.add_column(col, justify=j, no_wrap=col != "Title / first prompt")
    home = re.sub(r"[^A-Za-z0-9]", "-", str(Path.home())) + "-"
    for i, p in enumerate(cands, 1):
        info = session_info(p)
        t.add_row(str(i), info["start"][:16].replace("T", " "), p.parent.name.removeprefix(home)[:28],
                  str(info["prompts"]), fmt(info["peak"]),
                  Text(short(info["title"] or info["first"] or "(empty)", 70)))
    con.print(t)
    while True:
        try:
            choice = input(f"Choose session [1-{len(cands)}, Enter = 1, q = quit]: ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit()
        if choice.lower() == "q":
            sys.exit()
        if not choice:
            return cands[0]
        if choice.isdigit() and 1 <= int(choice) <= len(cands):
            return cands[int(choice) - 1]


def resolve(arg: str | None, list_mode: bool = False, limit: int = 30) -> Path:
    root = Path.home() / ".claude" / "projects"
    if arg and Path(arg).is_file():
        return Path(arg)
    if arg:
        dirs = project_dir(arg, root)
        cands = [p for d in dirs for p in d.glob("*.jsonl")] or \
                [p for p in root.glob("*/*.jsonl") if p.stem.startswith(arg)]
    else:
        cands = list(root.glob("*/*.jsonl"))
    cands = sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        sys.exit(f"No session found for {arg!r}")
    if list_mode and len(cands) > 1:
        return pick(cands[:limit])
    return cands[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", nargs="?",
                    help="session file, session ID prefix, project path (~/research/x) or project name fragment")
    ap.add_argument("-l", "--list", action="store_true", help="list sessions and pick one")
    ap.add_argument("-n", "--limit", type=int, default=30, help="max. sessions in the list")
    ap.add_argument("--turn", type=int, help="show details of one turn")
    ap.add_argument("--top", type=int, default=15, help="rows in the file table")
    ap.add_argument("--calls", nargs="?", const="all", metavar="TOOL",
                    help="list all tool calls chronologically (optionally only one tool, e.g. Bash)")
    ap.add_argument("--live-context", action="store_true",
                    help="also run `claude -p /context` in the session directory (current config, not historical)")
    a = ap.parse_args()
    p = resolve(a.session, a.list, a.limit)
    result = analyze(p)
    if a.calls:
        render_calls(p, result[-1], a.calls)
        return
    render(p, *result, a.turn, a.top)
    if a.live_context:
        cwd = next((json.loads(l).get("cwd") for l in p.open() if '"cwd"' in l), os.getcwd())
        live_context(cwd)


if __name__ == "__main__":
    main()
