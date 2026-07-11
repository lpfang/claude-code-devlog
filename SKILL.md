---
name: devlog
description: Render the current project's Claude Code session transcripts into a readable, HTML-ready Markdown development log. Use when the user asks to review the development process, generate a dev-log, see what commands ran, or export the session history.
---

# devlog

Render Claude Code session transcripts (prompts, replies, and every CLI command
with its output) into a clean, HTML-ready Markdown dev-log.

Claude Code already records everything to
`~/.claude/projects/<project>/<session-id>.jsonl`. This skill runs the converter
that turns those JSONL files into a human-readable `devlog.md`.

## When to use

- User asks to "review the development process", "see the dev log",
  "export the session", "what commands did we run", or similar.
- User types `/devlog`.

## What to do

Run the **global viewer** `~/.claude/devlogs/view.sh` — it rebuilds the log for
the current project, builds the HTML, and opens it in the browser:

- **`/devlog`** (grouped view → `devlog.md` + `devlog.html`):
  ```bash
  ~/.claude/devlogs/view.sh
  ```
- **`/devlog --raw`** (chronological event-trace → standalone `devlog-raw.html`):
  ```bash
  ~/.claude/devlogs/view.sh --raw
  ```

Both do a **full rebuild across all sessions** of the current project and print
the output path — report it. (The viewer auto-opens it; add `--no-open` to suppress.)

If `view.sh` is missing, fall back to the converter directly:
`python3 ~/.claude/devlogs/render_devlog.py` (grouped → `devlog.md`) or
`... --raw` (→ `devlog-raw.html`).

### Options the user may ask for

Pass to `~/.claude/devlogs/view.sh` (or directly to `render_devlog.py`):
- **A different project:** add `--project /path/to/project`
- **Only one session (converter only):** `--session <id-prefix>`
- **Bigger / full outputs (grouped only):** env `DEVLOG_MAXOUTPUT=20000`
- **Plain code blocks, no `<details>` (grouped only):** `--no-details` (converter flag)

### Raw view (`--raw`) highlights

Events in true JSONL order, labeled by role/type (`user/prompt`, `assistant/text`,
`assistant/thinking`, `assistant/tool_use`, `user/tool_result`), with per-event
timestamps (ms) and color-coded `tool_use_id`↔`tool_result` pairing (border +
badge + pill all share the call-id color). Long content (>1000 chars) collapses
behind a `(truncated N chars)` button with collapse buttons at both ends. Top
has a session+turn list; a floating `↑` returns to top.

### After generating

- The viewer auto-opens it. If asked for a quick summary, show session count +
  the session list (from the Table of Contents) and the Bash-command count
  (`grep -c '**Bash**'`).

## Notes

- The converter only **reads** transcripts and **writes** under `~/.claude/devlogs/`.
- Dev-logs are kept out of git (global, per-project) because command outputs may
  contain secrets.
- An async `Stop` hook (configured in `~/.claude/settings.json`) refreshes the
  log automatically after each turn; this skill does an explicit full rebuild.
- To scope logging to a single project instead of all projects, move the `Stop`
  hook from `~/.claude/settings.json` into that project's `.claude/settings.json`.
