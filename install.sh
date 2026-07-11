#!/usr/bin/env bash
# install.sh — install the claude-code-devlog system globally under ~/.claude/
set -euo pipefail

CLAUDE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILL_DIR="$CLAUDE/skills/devlog"
LOG_DIR="$CLAUDE/devlogs"

echo "=== Installing claude-code-devlog ==="
echo ""

# 1. converter
mkdir -p "$LOG_DIR"
cp render_devlog.py "$LOG_DIR/render_devlog.py"
chmod +x "$LOG_DIR/render_devlog.py"
echo "✓ converter → $LOG_DIR/render_devlog.py"

# 2. viewer + style
cp view.sh "$LOG_DIR/view.sh"
cp style.css "$LOG_DIR/style.css"
chmod +x "$LOG_DIR/view.sh"
echo "✓ viewer    → $LOG_DIR/view.sh"
echo "✓ style     → $LOG_DIR/style.css"

# 3. pandoc include files (back-to-top button for grouped view)
cat > "$LOG_DIR/devlog-head.html" <<'HEAD'
<style>
html{scroll-behavior:smooth}
.to-top{position:fixed;right:1.1rem;bottom:1.1rem;width:2.3rem;height:2.3rem;border-radius:50%;
  background:#0969da;color:#fff !important;text-align:center;line-height:2.3rem;text-decoration:none;
  font-size:1.2rem;box-shadow:0 2px 10px rgba(0,0,0,.35);opacity:0;pointer-events:none;transition:opacity .2s;z-index:9999}
.to-top.show{opacity:1;pointer-events:auto}
@media(prefers-color-scheme:dark){.to-top{background:#58a6ff}}
</style>
<script>
document.addEventListener('DOMContentLoaded',function(){
  var tt=document.getElementById('toTop');
  if(!tt)return;
  tt.addEventListener('click',function(e){e.preventDefault();window.scrollTo({top:0,behavior:'smooth'});});
  window.addEventListener('scroll',function(){if(window.scrollY>300)tt.classList.add('show');else tt.classList.remove('show');});
});
</script>
HEAD
cat > "$LOG_DIR/devlog-tail.html" <<'TAIL'
<a href="#" class="to-top" id="toTop" title="返回顶部">↑</a>
TAIL
echo "✓ includes  → $LOG_DIR/devlog-head.html + tail.html"

# 4. skill
mkdir -p "$SKILL_DIR"
cp SKILL.md "$SKILL_DIR/SKILL.md"
echo "✓ /devlog   → $SKILL_DIR/SKILL.md"

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps (manual — edit ~/.claude/settings.json):"
echo " 1. Add 'cleanupPeriodDays': 365"
echo " 2. Add the Stop hook (see settings-additions.json for exact snippet)"
echo " 3. Restart Claude Code"
echo ""
echo "Optional: add shell aliases for quick terminal access:"
echo "  alias devlog='~/.claude/devlogs/view.sh'"
echo "  alias devlograw='~/.claude/devlogs/view.sh --raw'"
echo ""
echo "File: $CLAUDE/settings.json — snippet to add:"
cat settings-additions.json