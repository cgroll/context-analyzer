# context-analyzer

See where the context window of a Claude Code session went.

`ctxview.py` reads a past Claude Code session from `~/.claude/projects/` and reconstructs, turn by turn, how large the context was and what filled it: files the agent read (including via `cat`, `sed`, `grep` in Bash), command output, web results, skills, thinking, code it wrote, and the invisible startup block (system prompt, tool schemas, `CLAUDE.md` / `AGENTS.md`).

It answers questions like:

- Which turn blew up the context, and why?
- Which files did the agent actually look at, how much of them, and which ones several times?
- How much of the window was gone before I typed anything?
- What did the agent do: which tools and Bash commands, and which outputs were expensive?
- What survived a compaction, and what had to be re-read afterwards?

Everything runs locally, read-only, on the JSONL files Claude Code already writes. Nothing is sent anywhere.

## Usage

Requires Python ≥ 3.10 and [`rich`](https://github.com/Textualize/rich). With [uv](https://docs.astral.sh/uv/) the dependency is resolved automatically from the inline script metadata:

```bash
uv run ctxview.py              # or: python3 ctxview.py (with rich installed)
```

### Picking a session

```bash
python3 ctxview.py                          # newest session across all projects
python3 ctxview.py -l                       # list the 30 newest sessions and pick one
python3 ctxview.py ~/research/my-project -l # list sessions of one project and pick one
python3 ctxview.py my-project               # newest session of a project (name fragment works)
python3 ctxview.py f6da6ae5                 # a session by ID prefix
python3 ctxview.py path/to/session.jsonl    # a session file directly
```

The list shows start time, project, number of prompts, context peak and the session title (or first prompt).

### Drilling down

```bash
python3 ctxview.py f6da6ae5 --turn 13       # everything that entered the context in turn 13
python3 ctxview.py f6da6ae5 --calls         # every tool call in order, with result size
python3 ctxview.py f6da6ae5 --calls Bash    # only one tool
python3 ctxview.py f6da6ae5 --top 30        # more rows in the file table
python3 ctxview.py f6da6ae5 --live-context  # also run `claude -p /context` in the project (see caveats)
python3 ctxview.py f6da6ae5 | less -R       # scroll with colors
```

### What the output shows

The output is ordered so that the most important part ends up at the bottom of the terminal, visible without scrolling.

**Top (scroll up for background):**

1. **Startup context**: the exact size of the context at the first API call, split into the invisible block (system prompt, tool schemas, memory files, MCP tools) and the visible attachments (skill listing, deferred tool names, MCP instructions, agent listing, …).
2. **Loaded skills**: every skill whose full text was injected, with turn and size.
3. **Tool calls**: per tool: count, errors, tokens for the call itself (e.g. the code passed to `Write`/`Edit`) and for the result.
4. **Bash commands by main command**: grouped as `git status`, `npm run dev`, `python (script)`, `grep`, …, with count, errors, output size and an example.
5. **Largest Bash outputs**: the ten most expensive individual commands.

**Bottom (visible right away):**

6. **Context per turn**: per prompt: number of API calls, context size at the end of the turn, growth, a stacked bar of the composition (scaled to the session peak) and the largest single addition. `⟳` marks a turn with a compaction.
7. **Composition at peak (and at the end)**: tokens and share per category, with bars relative to the largest category. The "end" columns only appear when they differ, e.g. after a compaction.
8. **Files looked at**: per file: how often it was read, how often it showed up in search matches, via which tool (`Read`, `cat`, `sed`, `grep`, `@`, …), the extent (`full`, `partial`, `match`, `name only`, `script`), tokens, and the compaction segments it was read in. Files read more than once are highlighted.

### Categories

| Category | Contents |
|---|---|
| System+Tools | Invisible baseline at session start (and after a compaction): system prompt, tool schemas, MCP tools, `CLAUDE.md`/`AGENTS.md` |
| Compact-Summary | The summary that replaces the history after a compaction |
| File reads | `Read` results, `@`-mentions, and Bash commands that only read files (`cat`, `head`, `sed -n`, …) |
| Bash | Output of all other shell commands (builds, tests, git, scripts, logs) |
| Search | `grep`/`rg`/Glob output, and Bash commands that only search |
| Web | `WebSearch` / `WebFetch` results |
| Agent | Final answers of subagents (their own work happens in a separate context) |
| MCP/other tools | MCP tool results, `Skill`, `ToolSearch`, task tools, … |
| Attachments | What Claude Code injects between messages: skill/agent listings, deferred tool names, reminders, changed-file notices, … |
| Skills/Meta | Full skill texts when a skill is invoked, other hidden meta messages |
| User | Your prompts (images count as ~1.6k tokens each) |
| Response | Text the model wrote to you |
| Tool calls | Inputs of tool calls: commands, paths, queries |
| Code writing | Inputs of `Write`/`Edit`/`MultiEdit`/`NotebookEdit`, i.e. the code the model wrote |
| Thinking | Thinking tokens, measured from `usage` |
| Rest | Growth that could not be attributed to anything visible |

The input of a tool call (e.g. `cat foo.py`) counts as *Tool calls*; only its result goes to *File reads*, *Bash*, *Search*, etc.

## How it works, and what is fuzzy

Claude Code writes every session to `~/.claude/projects/<encoded-project-path>/<session-id>.jsonl`: user messages, assistant messages (text, thinking, tool calls), tool results, attachments, compaction markers and the API `usage` for every model call. The log is the full history even after `/compact`; compaction only appends a `compact_boundary` marker and a summary.

The tool walks this log and, for each API call, attributes the growth of the context since the previous call to whatever was added in between. Different numbers have very different reliability:

### Exact

- **Context size per API call**: `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` from `usage`. Checked against Claude Code's own `preTokens` at a compaction: within 0.3 %. (`input_tokens` on its own is usually ~1, because almost everything is cached; the sum is what counts. Entries are deduplicated by `requestId`, since one API call is logged as several lines.)
- **What happened**: which tools were called with which inputs, which files were passed to `Read`, which skills were loaded, when compactions happened. This is all recorded in the log.

### Measured, with a small caveat

- **Thinking**: the thinking text is empty in the log (only an encrypted signature is stored), but its size is known from `usage.output_tokens_details.thinking_tokens`, or else `output_tokens` minus the visible output. In all sessions tested, the measured thinking was fully found again in the next call's context growth, i.e. thinking blocks stay in the context.

### Estimated

- **The split of each call's growth across contents.** Per-item token counts are not logged, so each item is sized by its character count and scaled. The characters-per-token ratio is **calibrated per session** on calls without thinking, where the growth can be attributed exactly (typically 2.1–2.7 chars/token rather than the commonly quoted ~4). Calls without thinking are then scaled exactly to the measured growth; in calls with thinking, the remainder goes to *Rest*. Rest is usually 3–6 % and consists of per-message framing overhead and differences in how code, JSON and prose tokenize.
- **The invisible startup block** is only known as a size (exact total minus everything visible), not by content. Which memory files it contained is not in the log.
- **Images** are counted as a flat ~1.6k tokens.

### Heuristic: files touched via Bash

Agents often read files with `cat`, `head`, `sed -n` or `grep` instead of the `Read` tool. These are recovered by parsing the command:

- Commands are split into statements (`;`, `&&`, `||`) and pipeline stages (`|`); redirections are stripped, `cd` is followed so relative paths resolve, wrappers like `sudo`/`timeout`/`VAR=x` are skipped.
- Only the **first stage** of a pipeline reads files; `cat a | head` means `a` was read *partially*.
- `cat`/`bat`/`nl` without a pipe → `full`; `head`, `tail`, `sed`, `awk`, `jq`, `cut`, … → `partial`; paths in `open("…")`, `read_csv("…")`, `Path("…")` inside Python heredocs or `python -c` → `script`.
- A Bash call is categorised as *File reads* or *Search* only if every command in it just reads or searches (`cd`, `echo`, `ls`, `wc` are ignored). As soon as anything else runs (`npm`, `python`, `git`, …), the whole output stays under *Bash*; the files still appear in the file table.

**grep only puts its output into the context, not the searched file.** So a searched file only counts if it appears in the output: lines like `path:12:code` count as `match`, `path-13-code` (from `-A/-B/-C`) as `match+context`, bare paths (`grep -l`, the Grep tool's default mode) as `name only`. Files without matches are not counted at all. Tokens are split by how many output characters belong to each file.

**Not detected:** file names held in variables or loops (`for f in *.py; do cat "$f"; done`), files read internally by programs (`pytest`, `npm`, a Python script reading a computed path), `grep -h` across several files (no file names in the output), and globs whose files no longer exist.

### Not covered

- **Subagent transcripts** (`<session>/subagents/*.jsonl`) are not analysed; only the subagent's final answer, which enters the main context, is counted.
- **Historical memory files.** Whether `AGENTS.md`, `CLAUDE.md` or `@`-imports were loaded at the start of a past session is not recorded. The tool deliberately does not reconstruct this from loading rules, because that would show what *should* have happened, not what did. `--live-context` runs `claude -p /context` in the project directory and shows what Claude Code loads *today* (system prompt, tools, MCP, skills, memory files); it is labelled as current, not historical.
- **The JSONL format is internal** to Claude Code and undocumented. It changes between versions; the parser skips what it does not recognise rather than failing.

To see the exact content of every request, including the system prompt and memory files, the requests have to be captured while the session runs, e.g. with a logging proxy via `ANTHROPIC_BASE_URL` or a tool like [claude-trace](https://github.com/badlogic/lemmy/tree/main/apps/claude-trace). That only works for future sessions.

## Related tools

- [claude-devtools](https://github.com/matt1398/claude-devtools): desktop/web app with per-turn token attribution, compaction and subagent views.
- [claude-code-log](https://github.com/daaain/claude-code-log): renders transcripts to HTML/Markdown.
- [ccusage](https://github.com/ccusage/ccusage): token and cost reports.
- [cc-viewer](https://github.com/weiesky/cc-viewer): captures live API requests.
