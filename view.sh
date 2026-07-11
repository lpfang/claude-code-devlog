#!/usr/bin/env bash
# devlog viewer — refresh the Markdown dev-log for a project, build a styled
# self-contained HTML, and open it in the default browser.
#
# Usage:
#   ./view.sh                     # current project ($PWD), grouped view
#   ./view.sh --raw               # chronological event trace (true order)
#   ./view.sh /path/to/project    # a specific project
#   ./view.sh --no-open           # build but don't launch a browser
#   DEVLOG_MAXOUTPUT=20000 ./view.sh   # env: per-output char budget
set -euo pipefail

OPEN=1
PROJECT=""
RAW=0
for a in "$@"; do
  case "$a" in
    --no-open) OPEN=0 ;;
    --raw) RAW=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) PROJECT="$a" ;;
  esac
done

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CONVERTER="$CLAUDE_DIR/devlogs/render_devlog.py"
STYLE="$(cd "$(dirname "$0")" && pwd)/style.css"

if [[ ! -f "$CONVERTER" ]]; then
  echo "converter not found: $CONVERTER" >&2
  echo "(restore it from this project's render_devlog.py — see README.md)" >&2
  exit 1
fi

# 1. run the converter (prints the output path). For --raw it writes a
#    standalone devlog-raw.html directly (no pandoc needed); otherwise devlog.md.
ARGS=()
[[ -n "$PROJECT" ]] && ARGS+=(--project "$PROJECT")
[[ -n "${DEVLOG_MAXOUTPUT:-}" ]] && ARGS+=(--max-output "$DEVLOG_MAXOUTPUT")
[[ "$RAW" -eq 1 ]] && ARGS+=(--raw)
OUT="$(python3 "$CONVERTER" ${ARGS[@]+"${ARGS[@]}"})"
echo "output: $OUT"

# 2. raw view is already standalone HTML; grouped view needs pandoc .md -> .html
if [[ "$OUT" == *.html ]]; then
  TARGET="$OUT"
else
  OUT_HTML="${OUT%.md}.html"
  if command -v pandoc >/dev/null 2>&1; then
    HEAD_INC="$CLAUDE_DIR/devlogs/devlog-head.html"
    TAIL_INC="$CLAUDE_DIR/devlogs/devlog-tail.html"
    PANDOC_ARGS=(pandoc -f markdown-tex_math_dollars "$OUT" -o "$OUT_HTML" --standalone --embed-resources \
      --toc --toc-depth=3 --highlight-style=tango --metadata title="Development Log")
    [[ -f "$STYLE" ]] && PANDOC_ARGS+=(--css "$STYLE")
    [[ -f "$HEAD_INC" ]] && PANDOC_ARGS+=(--include-in-header="$HEAD_INC")
    [[ -f "$TAIL_INC" ]] && PANDOC_ARGS+=(--include-after-body="$TAIL_INC")
    "${PANDOC_ARGS[@]}"
    echo "html: $OUT_HTML"
    TARGET="$OUT_HTML"
  else
    echo "pandoc not installed; opening the Markdown instead" >&2
    TARGET="$OUT"
  fi
fi

# 3. open in the default viewer
if [[ "$OPEN" -eq 1 ]]; then
  case "$(uname)" in
    Darwin) open "$TARGET" ;;
    Linux)  "${OPENER:-xdg-open}" "$TARGET" ;;
    *)      echo "open manually: $TARGET" ;;
  esac
fi
