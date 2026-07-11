#!/usr/bin/env python3
"""
render_devlog.py — render Claude Code session transcripts into a readable,
HTML-ready Markdown development log.

Claude Code already records every prompt, reply, and tool call (with output)
to ~/.claude/projects/<project>/<session-id>.jsonl. This script parses those
authoritative transcripts and emits a clean Markdown dev-log designed to
convert losslessly to HTML (pandoc / markdown-it / Python-Markdown).

Architecture: parse once into typed events, then render via a pluggable
renderer (MarkdownRenderer now; HtmlRenderer later reuses the same parser).

Standard library only.
"""

import argparse
import datetime as _dt
import difflib
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional

CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
DEVLOGS_DIR = os.path.join(CLAUDE_DIR, "devlogs")

# Record types we ignore entirely (metadata / noise).
SKIP_TYPES = {
    "mode", "permission-mode", "file-history-snapshot", "attachment",
    "system", "queue-operation",
}

DEFAULT_MAX_OUTPUT = 800
DEVLOG_VERSION = "2026-07-11.1"


# --------------------------------------------------------------------------- #
# Project / path helpers
# --------------------------------------------------------------------------- #
def slugify_cwd(cwd: str) -> str:
    """Match the way Claude Code derives a project dir name from cwd."""
    cwd = cwd.rstrip("/") or "/"   # "/" stays "/", "" becomes "/"
    return cwd.replace("/", "-")


def resolve_project_dir(cwd: Optional[str], transcript_path: Optional[str]) -> Optional[str]:
    """Find the ~/.claude/projects/<slug> dir for a cwd, or from a transcript path."""
    if cwd:
        cand = os.path.join(PROJECTS_DIR, slugify_cwd(cwd))
        if os.path.isdir(cand):
            return cand
    if transcript_path:
        return os.path.dirname(os.path.abspath(transcript_path))
    return None


# --------------------------------------------------------------------------- #
# Parsing: JSONL -> typed events -> turns
# --------------------------------------------------------------------------- #
@dataclass
class Action:
    name: str
    tool_use_id: str
    input: dict
    result_text: Optional[str] = None
    is_error: bool = False


@dataclass
class Turn:
    prompt: str
    ts: Optional[str]
    replies: list = field(default_factory=list)      # Claude text blocks (str)
    actions: list = field(default_factory=list)       # Action objects


@dataclass
class Session:
    session_id: str
    title: str
    cwd: Optional[str]
    git_branch: Optional[str]
    version: Optional[str]
    start_ts: Optional[str]
    turns: list = field(default_factory=list)

    @property
    def slug_path(self) -> str:
        return self.cwd or ""


def extract_prompt(content):
    """Return the human-readable prompt text from a user message, or None if it
    is a system/injected message to skip. Slash-command invocations are rendered
    as their command (e.g. '/devlog'). If content is a list of blocks, text
    blocks are concatenated (handles structured user messages)."""
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = (b.get("text") or "").strip()
                if t:
                    parts.append(t)
        content = " ".join(parts) if parts else ""
    if not isinstance(content, str) or not content.strip():
        return None
    s = content.strip()
    m = re.search(r"<command-name>([^<]*)</command-name>", content)
    if m:
        cmd = m.group(1).strip()
        if cmd and not cmd.startswith("/"):
            cmd = "/" + cmd
        am = re.search(r"<command-args>([^<]*)</command-args>", content)
        args = am.group(1).strip() if am else ""
        return (cmd + (" " + args if args else "")).strip() or cmd
    is_system = s.startswith(("<command-message>", "<local-command-", "<system-reminder>",
                               "<user-memory", "<command-args>"))
    # <command-name> without a slash is system-injected (not a user command);
    # treat like the other system prefixes.
    if is_system or s.startswith("<command-name>"):
        return None
    return s


def _result_to_text(content) -> str:
    """tool_result.content may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "\n".join(p for p in parts if p)
    return str(content)


def parse_session(path: str) -> Optional[Session]:
    """Parse one transcript JSONL into a Session (title + ordered turns)."""
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # malformed line — skip

    if not records:
        return None

    # Pass 1: collect session-level meta + a map of tool_use_id -> result.
    title = ""
    session_id = os.path.basename(path)[: -len(".jsonl")]
    cwd = git_branch = version = None
    results: dict = {}

    for rec in records:
        rtype = rec.get("type")
        if rtype == "ai-title":
            title = rec.get("aiTitle") or title
        cwd = rec.get("cwd") or cwd
        git_branch = rec.get("gitBranch") or git_branch
        version = rec.get("version") or version
        session_id = rec.get("sessionId") or session_id

        if rtype == "user":
            content = rec.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        if tid:
                            results[tid] = {
                                "text": _result_to_text(block.get("content")),
                                "is_error": bool(block.get("is_error")),
                            }

    # Pass 2: build ordered turns.
    turns: list = []
    current: Optional[Turn] = None

    def ensure_turn(ts=None):
        nonlocal current
        current = Turn(prompt="", ts=ts, replies=[], actions=[])
        turns.append(current)
        return current

    start_ts = None
    for rec in records:
        rtype = rec.get("type")
        ts = rec.get("timestamp")
        if ts and not start_ts:
            start_ts = ts

        if rtype == "user":
            content = rec.get("message", {}).get("content")
            ptext = extract_prompt(content)
            if ptext is not None:
                # New human prompt => new turn.
                t = ensure_turn(ts)
                t.prompt = ptext
            # tool_result user messages are handled via the results map.
        elif rtype == "assistant":
            content = rec.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            if current is None:
                ensure_turn(ts)  # assistant before any prompt (rare)
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    current.replies.append(block["text"])
                elif btype == "tool_use" or btype == "server_tool_use":
                    tid = block.get("id") or block.get("tool_use_id") or ""
                    act = Action(
                        name=block.get("name", "?"),
                        tool_use_id=tid,
                        input=block.get("input", {}) or {},
                    )
                    res = results.get(tid)
                    if res:
                        act.result_text = res["text"]
                        act.is_error = res["is_error"]
                    current.actions.append(act)
                # thinking blocks: skipped by default

    # Drop empty turns (no prompt, no reply, no actions).
    turns = [t for t in turns if t.prompt or t.replies or t.actions]

    if not turns and not title:
        return None

    return Session(
        session_id=session_id,
        title=title or "(untitled session)",
        cwd=cwd,
        git_branch=git_branch,
        version=version,
        start_ts=start_ts or turns[0].ts if turns else None,
        turns=turns,
    )


# --------------------------------------------------------------------------- #
# Raw chronological event stream (for the --raw view)
# --------------------------------------------------------------------------- #
def iter_raw_events(path: str):
    """Yield transcript events in TRUE chronological order (one per meaningful
    block), for the raw view. Each event is a dict {ts, role, kind, ...} where
    kind in {prompt, text, thinking, tool_use, tool_result}. Unlike
    parse_session (which groups replies/actions per turn), this preserves the
    real interleaving of text and tool calls."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            ts = rec.get("timestamp")
            if rtype == "user":
                content = rec.get("message", {}).get("content")
                if isinstance(content, str):
                    ptext = extract_prompt(content)
                    if ptext is not None:
                        yield {"ts": ts, "role": "user", "kind": "prompt", "text": ptext}
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            yield {"ts": ts, "role": "user", "kind": "tool_result",
                                   "tool_use_id": b.get("tool_use_id"),
                                   "is_error": bool(b.get("is_error")),
                                   "text": _result_to_text(b.get("content"))}
            elif rtype == "assistant":
                content = rec.get("message", {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text" and b.get("text", "").strip():
                            yield {"ts": ts, "role": "assistant", "kind": "text", "text": b["text"]}
                        elif bt == "thinking" and b.get("thinking", "").strip():
                            yield {"ts": ts, "role": "assistant", "kind": "thinking", "text": b["thinking"]}
                        elif bt == "tool_use" or bt == "server_tool_use":
                            yield {"ts": ts, "role": "assistant", "kind": "tool_use",
                                   "name": b.get("name", "?"),
                                   "tool_use_id": b.get("id") or b.get("tool_use_id") or "",
                                   "input": b.get("input", {}) or {}}
            # other record types (mode/system/file-history-snapshot/...) are skipped


# --------------------------------------------------------------------------- #
# Markdown helpers (portability rules)
# --------------------------------------------------------------------------- #
# Display timezone for rendered timestamps.
# Transcripts store UTC. We default to the local system timezone.
# Override with env DEVLOG_TZ (e.g. DEVLOG_TZ=8 for UTC+8).
_off = _dt.datetime.now(_dt.timezone.utc).astimezone().utcoffset()
_tz_h = _off.total_seconds() / 3600 if _off else 8
if "DEVLOG_TZ" in os.environ:
    try:
        _tz_h = float(os.environ["DEVLOG_TZ"])
    except ValueError:
        pass
DISPLAY_TZ = _dt.timezone(_dt.timedelta(hours=_tz_h))
DISPLAY_TZ_LABEL = f"UTC{_tz_h:+.0f}" if _tz_h == int(_tz_h) else f"UTC{_tz_h:+.1f}"


def fmt_ts(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        # transcript timestamps are ISO 8601 UTC, e.g. 2026-06-16T01:08:51.750Z
        dt = _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
        return dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M") + f" ({DISPLAY_TZ_LABEL})"
    except Exception:
        return ts


def fmt_time(ts: Optional[str]) -> str:
    """Compact HH:MM:SS.mmm (display tz) for per-event timestamps in the raw view."""
    if not ts:
        return ""
    try:
        dt = _dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
        base = dt.astimezone(DISPLAY_TZ).strftime("%H:%M:%S")
        m = re.match(r"\.(\d+)", ts[19:])  # fractional seconds, e.g. ".889Z"
        ms = ("." + m.group(1)[:3].ljust(3, "0")) if m else ""
        return base + ms
    except Exception:
        return ts


def color_for_id(tid: str) -> str:
    """Deterministic HSL color for a tool_use_id, so a tool_use and its matching
    tool_result share the same color. Stable across runs (md5, not hash())."""
    if not tid:
        return ""
    h = int(hashlib.md5(tid.encode("utf-8"), usedforsecurity=False).hexdigest(), 16)
    return f"hsl({h % 360}, 65%, 50%)"


def md_to_html(text: str) -> str:
    """Minimal Markdown -> HTML for prose blocks in the raw view (headings,
    bold, italic, code spans, links, bullet lists, paragraphs). Output is safe
    HTML (all text is escaped). Intentionally small — not a full renderer."""
    def inline(s: str) -> str:
        s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        codes = []
        s = re.sub(r"`([^`]+)`", lambda m: (codes.append(m.group(1)), f"\x00{len(codes)-1}\x00")[1], s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", s)
        safe_url = lambda u: u.startswith(("http://", "https://", "#"))
        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
                   lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>' if safe_url(m.group(2))
                   else f'{m.group(1)} [{m.group(2)}]',
                   s)
        s = re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{codes[int(m.group(1))]}</code>", s)
        return s

    out = []
    lines = (text or "").split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            out.append(f'<div class="md-h">{inline(m.group(2))}</div>')
            i += 1
            continue
        if re.match(r"^[-*+]\s+", line):
            items = []
            while i < n and re.match(r"^[-*+]\s+", lines[i]):
                items.append(f"<li>{inline(re.sub(r'^[-*+]\s+', '', lines[i]))}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        para = []
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|[-*+]\s)", lines[i]):
            para.append(inline(lines[i]))
            i += 1
        out.append("<p>" + "<br>".join(para) + "</p>")
    return "\n".join(out)


def render_long_html(text: str, limit: int = 1000, code: bool = False) -> str:
    """HTML fragment for one content block in the raw view. If `text` is longer
    than `limit`, show the first `limit` continuously and hide the rest in an
    inline <span class="tr-rest"> within the SAME block (so expanding flows as
    one continuous whole). Two toggle buttons (top + bottom) flip .open via JS:
    collapsed -> bottom reads '(truncated N chars)'; expanded -> both read
    '(部分显示)'."""
    text = text or ""
    if len(text) <= limit:
        if code:
            return f"<pre><code>{escape_html(text)}</code></pre>"
        return md_to_html(text)
    omitted = len(text) - limit
    preview, rest = text[:limit], text[limit:]
    if code:
        body = f'<pre><code>{escape_html(preview)}<span class="tr-rest">{escape_html(rest)}</span></code></pre>'
    else:
        body = f'{md_to_html(preview)}<div class="tr-rest">{md_to_html(rest)}</div>'
    return ('<div class="tr">'
            '<button class="tr-btn tr-top" type="button"></button>'
            f'{body}'
            f'<button class="tr-btn tr-bot" type="button" data-rest="{omitted}"></button>'
            '</div>')


# CSS + JS embedded in the standalone raw HTML. The raw view emits HTML directly
# (bypassing pandoc), so it carries its own styling + the toggle script.
RAW_CSS = """:root{--fg:#1f2328;--bg:#fff;--muted:#57606a;--border:#d0d7de;--code-bg:#f6f8fa;--accent:#0969da}
@media(prefers-color-scheme:dark){:root{--fg:#e6edf3;--bg:#0d1117;--muted:#8b949e;--border:#30363d;--code-bg:#161b22;--accent:#58a6ff}}
html{-webkit-text-size-adjust:100%;scroll-behavior:smooth}
body{font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:var(--fg);background:var(--bg);max-width:980px;margin:0 auto;padding:2rem 1.25rem 6rem}
header h1{font-size:1.6rem;border-bottom:2px solid var(--border);padding-bottom:.3rem}
h2{border-bottom:1px solid var(--border);padding-bottom:.2rem;margin-top:2rem}
h3{margin-top:1.4rem;color:var(--fg)}
a{color:var(--accent)}
code,pre{font:13px/1.5 "SF Mono",Menlo,Consolas,monospace}
pre{background:var(--code-bg);padding:.7rem .9rem;border-radius:8px;overflow-x:auto;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--border)}
code{background:var(--code-bg);padding:.1em .3em;border-radius:4px}
pre code{background:none;padding:0}
.meta{color:var(--muted);font-size:.9em}
.toc{background:var(--code-bg);border:1px solid var(--border);border-radius:8px;padding:.6rem 1rem;max-height:42vh;overflow:auto}
.toc a{display:block}
.toc a.toc-s{font-weight:700;margin-top:.35rem}
.toc a.toc-t{padding-left:1.4rem;color:var(--muted);font-size:.9em;line-height:1.3}
.evt{margin:.25rem 0}
.eh{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem;padding:.2rem .55rem;border-radius:5px;background:var(--code-bg);border-left:3px solid var(--border);font-weight:600}
.eh time{font-family:"SF Mono",Menlo,monospace;font-size:.82em;color:var(--muted);font-weight:400}
.eh .role{font-size:.78em;font-weight:700;padding:.05em .5em;border-radius:10px;color:#fff;white-space:nowrap}
.eh .tid{font-family:monospace;font-size:.76em;color:#fff;padding:.06em .45em;border-radius:9px}
.eh .st{font-family:monospace;font-size:.85em;color:var(--muted)}
.evt-prompt .eh{border-left-color:#0969da}.evt-prompt .role{background:#0969da}
.evt-text .eh{border-left-color:#1a7f37}.evt-text .role{background:#1a7f37}
.evt-thinking .eh{border-left-color:#8957e5}.evt-thinking .role{background:#8957e5}
.evt-tool_use .eh{border-left-color:#d29922}.evt-tool_use .role{background:#d29922;color:#1f2328}
.evt-tool_result .eh{border-left-color:#1b7c83}.evt-tool_result .role{background:#1b7c83}
.to-top{position:fixed;right:1.1rem;bottom:1.1rem;width:2.3rem;height:2.3rem;border-radius:50%;background:var(--accent);color:#fff;text-align:center;line-height:2.3rem;text-decoration:none;font-size:1.2rem;box-shadow:0 2px 10px rgba(0,0,0,.35);opacity:0;pointer-events:none;transition:opacity .2s;z-index:50}
.to-top.show{opacity:1;pointer-events:auto}
blockquote{margin:.2rem 0 .2rem 0;padding:.1rem .8rem;border-left:.25rem solid var(--accent);background:var(--code-bg);border-radius:0 6px 6px 0;color:var(--muted)}
.prose{white-space:pre-wrap}
.md-h{font-weight:700;margin:.5rem 0 .15rem}
.evt ul{margin:.2rem 0;padding-left:1.4rem}
.evt p{margin:.2rem 0}
hr{border:none;border-top:1px solid var(--border);margin:1.2rem 0}
/* truncate toggle: continuous preview + collapsible rest, toggle at top and bottom */
.tr-rest{display:none}.tr.open span.tr-rest{display:inline}.tr.open div.tr-rest{display:block}
.tr-btn{background:none;border:none;color:var(--accent);cursor:pointer;padding:.1em .35em;font:inherit;display:inline-block;border-radius:4px}
.tr-btn:hover{text-decoration:underline}
.tr-btn.tr-top{display:none}.tr.open .tr-btn.tr-top{display:inline-block}
.tr-btn::after{content:"(truncated " attr(data-rest) " chars)"}.tr.open .tr-btn::after{content:"(部分显示)"}"""

RAW_TOGGLE_JS = """(function(){function init(){document.querySelectorAll('.tr .tr-btn').forEach(function(b){if(b.dataset.bound)return;b.dataset.bound='1';b.addEventListener('click',function(){b.closest('.tr').classList.toggle('open');});});var tt=document.getElementById('toTop');if(tt)window.addEventListener('scroll',function(){if(window.scrollY>300)tt.classList.add('show');else tt.classList.remove('show');});}if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();})()"""


def summarize(prompt: str, width: int = 80) -> str:
    """First meaningful line of a prompt, for TOC/turn headings. Skips leading
    lines that are only box-drawing/whitespace (e.g. a pasted ASCII table border)
    and strips box-drawing chars so the summary is readable."""
    if not prompt:
        return ""
    chosen = ""
    for ln in prompt.strip().splitlines():
        s = ln.strip()
        if s and re.search(r"[\w一-鿿]", s):  # has a word char or CJK
            chosen = s
            break
    if not chosen:
        first = prompt.strip().splitlines()
        chosen = first[0].strip() if first else ""
    chosen = re.sub(r"[─-╿═-╬]", " ", chosen)  # strip box-drawing chars
    chosen = re.sub(r"\s+", " ", chosen).strip()
    return chosen[:width] + ("…" if len(chosen) > width else "")


def fence_for(text: str) -> str:
    """Pick a backtick fence longer than any run of backticks in `text`."""
    runs = re.findall(r"`+", text or "")
    longest = max((len(r) for r in runs), default=0)
    return "`" * max(3, longest + 1)


def truncate(text: str, max_chars: int) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 24:
        return text[:max_chars] + "…"
    tail = min(80, max_chars // 4)
    marker = 22  # actual "\n… [truncated N chars] …\n" ~22 chars
    head = max_chars - tail - marker
    if head < tail:
        head, tail = max_chars - marker, 0
    head = max(head, 8)
    if head <= 0:
        return text[:max_chars] + "…"
    omitted = max(0, len(text) - head - tail)
    sep = f"\n… [truncated {omitted} chars] …\n"
    return text[:head].rstrip() + sep + (text[-tail:].lstrip() if tail else "")


def escape_html(text: str) -> str:
    """Escape < and > so raw HTML in transcripts cannot inject into the rendered HTML."""
    return (text or "").replace("<", "&lt;").replace(">", "&gt;")


def balance_fences(md: str) -> str:
    """Ensure fenced code blocks in `md` are balanced. If a reply's markdown
    ends while still inside a code fence (an unterminated ``` / ~~~ — easy to
    produce when prose explains markdown by example), append a matching close so
    pandoc doesn't leave the block open and swallow the following turns."""
    in_fence = False
    open_len = 0
    fch = '`'
    for line in (md or "").split("\n"):
        m = re.match(r'^ {0,3}([`~]{3,})(.*)$', line)
        if m:
            ch, ln, info = m.group(1)[0], len(m.group(1)), m.group(2)
            if not in_fence:
                in_fence, open_len, fch = True, ln, ch
            elif ch == fch and ln >= open_len and info.strip() == "":
                in_fence = False
    if in_fence:
        md = (md or "") + "\n" + fch * open_len
    return md


def demote_headings(md: str, floor: int = 5) -> str:
    """Push reply headings down to >= floor (<=6) so they never outrank the doc.
    Only touches headings OUTSIDE fenced code blocks (``` or ~~~), so a `# comment`
    inside a code block is left intact."""
    out = []
    fence_char = None   # '`' or '~' when inside a fence
    fence_len = 0
    for line in (md or "").split("\n"):
        m = re.match(r'^ {0,3}([`~]{3,})$', line)
        if m:
            ch, ln = m.group(1)[0], len(m.group(1))
            if fence_char is None:
                fence_char, fence_len = ch, ln
            elif ch == fence_char and ln >= fence_len:
                fence_char, fence_len = None, 0
            out.append(line)
            continue
        if fence_char is None:
            hm = re.match(r"^(#{1,6})(\s|$)", line)
            if hm:
                n = len(hm.group(1))
                out.append("#" * min(6, max(floor, n)) + line[n:])
                continue
        out.append(line)
    return "\n".join(out)


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# --------------------------------------------------------------------------- #
# Markdown renderer
# --------------------------------------------------------------------------- #
class MarkdownRenderer:
    def __init__(self, max_output: int = DEFAULT_MAX_OUTPUT, use_details: bool = True):
        self.max_output = max_output
        self.use_details = use_details

    # -- action renderers ---------------------------------------------------
    def render_action(self, act: Action) -> str:
        name = act.name
        inp = act.input
        blocks = []

        if name == "Bash":
            cmd = inp.get("command", "")
            desc = inp.get("description", "")
            status = "error" if act.is_error else ("exit 0" if act.result_text is not None else "")
            size = human_bytes(len(act.result_text)) if act.result_text else ""
            meta = " · ".join(p for p in (status, size) if p)
            head = f"**Bash**" + (f" · {meta}" if meta else "")
            if desc:
                head += f" — {escape_html(desc)}"
            blocks.append(head)
            f = fence_for(cmd)
            blocks.append(f"{f}bash\n$ {cmd}\n{f}")
            if act.result_text:
                blocks.append(self._render_output(act.result_text, act.is_error))

        elif name in ("Edit", "MultiEdit"):
            path = inp.get("file_path", "?")
            blocks.append(f"**{name}** · `{path}`")
            if name == "Edit":
                diffs = [(inp.get("old_string", ""), inp.get("new_string", ""))]
            else:
                diffs = [(e.get("old_string", ""), e.get("new_string", ""))
                         for e in inp.get("edits", []) if isinstance(e, dict)]
            for old, new in diffs:
                diff = "\n".join(difflib.unified_diff(
                    old.splitlines(), new.splitlines(),
                    fromfile=f"a/{os.path.basename(path)}",
                    tofile=f"b/{os.path.basename(path)}", lineterm=""))
                diff = truncate(diff, self.max_output)
                f = fence_for(diff)
                blocks.append(f"{f}diff\n{diff}\n{f}")
            if act.result_text and not diffs:
                blocks.append(self._render_output(act.result_text, act.is_error))

        elif name == "Write":
            path = inp.get("file_path", "?")
            content = inp.get("content", "")
            size = human_bytes(len(content)) if isinstance(content, str) else ""
            blocks.append(f"**Write** · `{path}`" + (f" ({size})" if size else ""))
            if act.result_text:
                blocks.append(self._render_output(act.result_text, act.is_error))

        elif name == "Read":
            path = inp.get("file_path", "?")
            blocks.append(f"**Read** · `{path}`")
            if act.result_text:
                blocks.append(self._render_output(act.result_text, act.is_error))

        else:
            # Generic tool: show name + a compact JSON preview of the input.
            preview = json.dumps(inp, ensure_ascii=False)
            preview = truncate(preview, 240)
            blocks.append(f"**{name}**")
            f = fence_for(preview)
            blocks.append(f"{f}json\n{preview}\n{f}")
            if act.result_text:
                blocks.append(self._render_output(act.result_text, act.is_error))

        return "\n\n".join(b for b in blocks if b)

    def _render_output(self, text: str, is_error: bool) -> str:
        body = truncate(text, self.max_output)
        f = fence_for(body)
        code = f"{f}\n{body}\n{f}"
        if self.use_details:
            label = "Output (error)" if is_error else "Output"
            # Blank-line padding (portability rule 1) so inner code is parsed as MD.
            return f"<details>\n<summary>{label}</summary>\n\n{code}\n\n</details>"
        return code

    # -- turn / session / doc ----------------------------------------------
    def render_turn(self, idx: int, turn: Turn, anchor_id: str = "") -> str:
        ts = fmt_ts(turn.ts)
        head = f"### Turn {idx}"
        if ts:
            head += f" · {ts}"
        if turn.prompt:
            head += f" — {escape_html(summarize(turn.prompt))}"
        if anchor_id:
            head += f" {{#{anchor_id}}}"
        parts = [head, ""]

        if turn.prompt:
            quoted = "\n".join("> " + ln if ln else ">" for ln in escape_html(turn.prompt).split("\n"))
            parts += ["#### Prompt", "", quoted, ""]

        if turn.replies:
            reply_md = escape_html("\n\n".join(turn.replies))
            parts += ["#### Reply", "", demote_headings(balance_fences(reply_md)), ""]

        if turn.actions:
            parts.append("#### Actions\n")
            for act in turn.actions:
                parts.append(self.render_action(act))
                parts.append("")
        parts.append("---")
        return "\n".join(parts)

    def render_session(self, session: Session) -> str:
        sid_short = session.session_id[:8] if session.session_id else "session"
        title = session.title
        head = f"## Session {sid_short} — {escape_html(title)} {{#session-{sid_short}}}"

        meta_rows = [("Session ID", f"`{session.session_id}`")]
        if session.start_ts:
            meta_rows.append(("Started", fmt_ts(session.start_ts)))
        if session.cwd:
            meta_rows.append(("Working dir", f"`{session.cwd}`"))
        if session.git_branch:
            meta_rows.append(("Git branch", f"`{session.git_branch}`"))
        if session.version:
            meta_rows.append(("Claude Code", f"`v{session.version}`"))
        meta_rows.append(("Turns", str(len(session.turns))))

        table = ["|  |  |", "|---|---|"]
        for k, v in meta_rows:
            table.append(f"| **{k}** | {v} |")

        out = [head, "", "\n".join(table), "", "---"]
        for i, turn in enumerate(session.turns, 1):
            out.append("")
            out.append(self.render_turn(i, turn, f"turn-{sid_short}-{i}"))
        return "\n".join(out)

    def render_doc(self, project_name: str, sessions: list) -> str:
        lines = []
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines += [
            "---",
            f"project: {project_name}",
            f"generated_at: {now}",
            "generator: claude-code-devlog v1",
            "version: " + DEVLOG_VERSION,
            f"sessions: {len(sessions)}",
            "---",
            "",
            f"# Development Log · {project_name}",
            f"*v{DEVLOG_VERSION} · generated {now}*",
            "",
            "Auto-generated from Claude Code session transcripts.",
            "",
            "## Table of Contents",
            "",
        ]
        for s in sessions:
            sid_short = s.session_id[:8] if s.session_id else "session"
            date = (fmt_ts(s.start_ts).split(" ")[0]) if s.start_ts else ""
            slabel = f"Session {sid_short} — {escape_html(s.title)}" + (f" · {date}" if date else "")
            lines.append(f"**[{slabel}](#session-{sid_short})**")
            lines.append("")  # blank line: make pandoc render turns as a list, not a run-on paragraph
            for i, turn in enumerate(s.turns, 1):
                thead = f"Turn {i}"
                if turn.ts:
                    thead += f" · {fmt_ts(turn.ts)}"
                if turn.prompt:
                    thead += f" — {escape_html(summarize(turn.prompt, 50))}"
                lines.append(f"  - [{thead}](#turn-{sid_short}-{i})")
            lines.append("")
        lines += ["", "---", ""]

        for s in sessions:
            lines.append(self.render_session(s))
            lines.append("")

        return "\n".join(lines)


def head_for_toc(s: Session) -> str:
    sid_short = s.session_id[:8] if s.session_id else "session"
    return f"Session {sid_short} — {s.title}"


# --------------------------------------------------------------------------- #
# Raw renderer — chronological event trace (true order, type-labeled)
# --------------------------------------------------------------------------- #
class RawRenderer:
    """Emits a STANDALONE HTML doc (bypassing pandoc) of the raw chronological
    event trace: every event in true order, labeled by role/type, with
    per-event timestamps (ms), color-coded tool_use_id pairing, and long content
    behind a top+bottom toggle (continuous preview + collapsible rest)."""

    def event_html(self, ev: dict) -> str:
        t = escape_html(fmt_time(ev.get("ts")))
        role = ev.get("role", "")
        kind = ev.get("kind", "")
        if kind == "prompt":
            quote = "<br>".join(escape_html(ev["text"]).split("\n"))
            return (f'<div class="evt evt-{kind}"><div class="eh"><time>{t}</time> '
                    f'<b class="role">user/prompt</b></div><blockquote>{quote}</blockquote></div>')
        if kind in ("text", "thinking"):
            return (f'<div class="evt evt-{kind}"><div class="eh"><time>{t}</time> '
                    f'<b class="role">{escape_html(role)}/{escape_html(kind)}</b></div>'
                    f'{render_long_html(ev.get("text", ""))}</div>')
        if kind == "tool_use":
            name = ev.get("name", "?")
            inp = ev.get("input", {}) or {}
            tid = ev.get("tool_use_id", "")
            idcolor = color_for_id(tid)
            idspan = (f'<span class="tid" style="background:{idcolor}">id={escape_html(tid)}</span>'
                      if tid else "")
            ehstyle = f' style="border-left-color:{idcolor}"' if tid else ""
            if name == "Bash":
                body = render_long_html("$ " + inp.get("command", ""), code=True)
            else:
                body = render_long_html(json.dumps(inp, ensure_ascii=False, indent=2), code=True)
            return (f'<div class="evt evt-{kind}"><div class="eh"{ehstyle}><time>{t}</time> '
                    f'<b class="role" style="background:{idcolor};color:#fff">{escape_html(role)}/tool_use · {escape_html(name)}</b> {idspan}</div>'
                    f'{body}</div>')
        if kind == "tool_result":
            tid = ev.get("tool_use_id", "")
            status = "error" if ev.get("is_error") else "ok"
            idcolor = color_for_id(tid)
            idspan = (f'<span class="tid" style="background:{idcolor}">→ {escape_html(tid)}</span>'
                      if tid else "")
            ehstyle = f' style="border-left-color:{idcolor}"' if tid else ""
            body = render_long_html(ev.get("text", "") or "", code=True)
            return (f'<div class="evt evt-{kind}"><div class="eh"{ehstyle}><time>{t}</time> '
                    f'<b class="role" style="background:{idcolor};color:#fff">{escape_html(role)}/tool_result</b> {idspan} '
                    f'<span class="st">{status}</span></div>{body}</div>')
        return ""

    def session_html(self, path: str) -> str:
        meta = parse_session(path)
        if not meta:
            return ""
        sid = meta.session_id[:8] if meta.session_id else "session"
        out = [f'<section class="session"><h2 id="s-{sid}">Session {sid} — {escape_html(meta.title)}</h2>']
        turn_idx = 0
        events_count = 0
        body = []
        for ev in iter_raw_events(path):
            events_count += 1
            if ev.get("kind") == "prompt":
                turn_idx += 1
                body.append(f'<h3 id="t-{sid}-{turn_idx}">Turn {turn_idx} · {escape_html(fmt_ts(ev.get("ts")))} — '
                            f'{escape_html(summarize(ev["text"]))}</h3>')
            body.append(self.event_html(ev))
        meta_bits = []
        if meta.start_ts:
            meta_bits.append(f"started {escape_html(fmt_ts(meta.start_ts))}")
        if meta.cwd:
            meta_bits.append(f"cwd <code>{escape_html(meta.cwd)}</code>")
        meta_bits.append(f"{turn_idx} turns")
        meta_bits.append(f"{events_count} events")
        out.append(f'<p class="meta">{" · ".join(meta_bits)}</p>')
        out.extend(body)
        out.append("</section>")
        return "\n".join(out)

    def render_html_doc(self, project_name: str, project_dir: str, session_filter: Optional[str]) -> str:
        files = sorted(glob.glob(os.path.join(project_dir, "*.jsonl")), key=os.path.getmtime)
        if session_filter:
            files = [f for f in files if session_filter in os.path.basename(f)]
        metas = []
        for f in files:
            m = parse_session(f)
            if m:
                metas.append((f, m))
        metas.sort(key=lambda fm: fm[1].start_ts or "")
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        toc_parts = []
        for f, m in metas:
            sid = m.session_id[:8] if m.session_id else "session"
            toc_parts.append(f'<a class="toc-s" href="#s-{sid}">Session {sid} — {escape_html(m.title)}</a>')
            ti = 0
            for ev in iter_raw_events(f):
                if ev.get("kind") == "prompt":
                    ti += 1
                    label = f"Turn {ti}"
                    if ev.get("ts"):
                        label += f" · {fmt_ts(ev.get('ts'))}"
                    label += f" — {summarize(ev['text'])}"
                    toc_parts.append(f'<a class="toc-t" href="#t-{sid}-{ti}">{escape_html(label)}</a>')
        toc = "".join(toc_parts)
        sessions_html = "\n".join(self.session_html(f) for f, m in metas)
        return (
            '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Development Log (raw) · {escape_html(project_name)}</title>'
            f'<style>{RAW_CSS}</style></head><body>'
            f'<header><h1 id="top">Development Log (raw) · {escape_html(project_name)}</h1>'
            f'<p class="meta">Chronological event trace · {len(metas)} sessions · generated {now} · v{DEVLOG_VERSION}</p>'
            f'<nav class="toc">{toc}</nav></header>'
            f'{sessions_html}'
            f'<script>{RAW_TOGGLE_JS}</script>'
            '<a href="#top" class="to-top" id="toTop" title="返回顶部">↑</a>'
            '</body></html>'
        )


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def gather_sessions(project_dir: str, session_filter: Optional[str]) -> list:
    files = sorted(glob.glob(os.path.join(project_dir, "*.jsonl")), key=os.path.getmtime)
    if session_filter:
        files = [f for f in files if session_filter in os.path.basename(f)]
    sessions = []
    for f in files:
        s = parse_session(f)
        if s:
            sessions.append(s)
    # Chronological by start timestamp (fall back to file mtime order).
    def key(s):
        return s.start_ts or ""
    sessions.sort(key=key)
    return sessions


def render_to_file(sessions: list, project_name: str, out_dir: str,
                   max_output: int, use_details: bool, raw: bool = False,
                   project_dir: Optional[str] = None,
                   session_filter: Optional[str] = None) -> str:
    if raw:
        md = RawRenderer().render_html_doc(project_name, project_dir, session_filter)
        out_name = "devlog-raw.html"
    else:
        md = MarkdownRenderer(max_output=max_output, use_details=use_details).render_doc(project_name, sessions)
        out_name = "devlog.md"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, out_name)
    # Atomic write: render to a temp file in the same dir, then rename. Prevents
    # a concurrent reader (e.g. pandoc in view.sh) or a concurrent Stop-hook
    # render from ever seeing a partial devlog.md.
    fd, tmp = tempfile.mkstemp(prefix=".devlog-", suffix=".tmp", dir=out_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(md)
        os.replace(tmp, out_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out_path


def project_name_from_dir(project_dir: str) -> str:
    slug = os.path.basename(project_dir).lstrip("-")
    return slug if "-" in slug else slug


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Render Claude Code transcripts to a Markdown dev-log.")
    p.add_argument("--stdin", action="store_true", help="hook mode: read hook JSON from stdin")
    p.add_argument("--project", help="project cwd to render")
    p.add_argument("--session", help="render only a session whose id contains this substring")
    p.add_argument("--max-output", type=int, default=DEFAULT_MAX_OUTPUT, help="max chars per command output")
    p.add_argument("--no-details", action="store_true", help="emit plain code blocks instead of <details>")
    p.add_argument("--raw", action="store_true", help="chronological event trace (true order, type-labeled)")
    p.add_argument("--both", action="store_true", help="render both grouped (devlog.md) and raw (devlog-raw.md)")
    p.add_argument("--format", choices=["markdown", "html"], default="markdown")
    p.add_argument("--out", help="output directory (default: ~/.claude/devlogs/<slug>/)")
    args = p.parse_args(argv)

    if args.format == "html":
        print("HTML renderer is not implemented yet (Markdown is HTML-ready; "
              "use: pandoc devlog.md -o devlog.html --standalone --toc).", file=sys.stderr)
        return 2

    cwd = args.project
    transcript_path = None

    if args.stdin:
        try:
            data = json.load(sys.stdin)
        except Exception:
            data = {}
        cwd = cwd or data.get("cwd") or data.get("projectPath")
        transcript_path = data.get("transcript_path")
        # NOTE: do NOT narrow to data["session_id"] — the Stop hook must
        # regenerate the FULL project devlog (all sessions), not just the
        # current one. --session is a manual CLI filter only.

    if not cwd:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("PWD") or os.getcwd()

    project_dir = resolve_project_dir(cwd, transcript_path)
    if not project_dir or not os.path.isdir(project_dir):
        print(f"No transcript project dir found for cwd={cwd!r}", file=sys.stderr)
        return 1

    sessions = gather_sessions(project_dir, args.session)
    if not sessions:
        print(f"No renderable sessions found in {project_dir}", file=sys.stderr)
        return 1

    project_name = (os.path.basename(sessions[0].cwd.rstrip("/"))
                    if sessions and sessions[0].cwd else project_name_from_dir(project_dir))
    slug = os.path.basename(project_dir)
    out_dir = args.out or os.path.join(DEVLOGS_DIR, slug)

    targets = [False, True] if args.both else [args.raw]
    for is_raw in targets:
        out_path = render_to_file(sessions, project_name, out_dir, args.max_output,
                                  not args.no_details, raw=is_raw, project_dir=project_dir,
                                  session_filter=args.session)
        print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
