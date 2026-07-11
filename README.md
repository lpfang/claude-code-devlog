# Claude Code 开发日志系统（devlog）

把 Claude Code 会话里发生的一切——**你的指令、Claude 的回复、CLI 命令及其输出**——自动整理成一份可读、可转 HTML 的 Markdown 开发日志，方便回顾整个开发过程。

## Quick Install (English)

```bash
git clone https://github.com/lpfang/claude-code-devlog.git
cd claude-code-devlog
./install.sh
# Then edit ~/.claude/settings.json — add cleanupPeriodDays + the Stop hook
# (see settings-additions.json for the exact JSON snippet)
# Restart Claude Code. Done.
```

After install:
- **Auto:** After every turn (once CC restarted), `devlog-raw.html` + `devlog.md` are refreshed per project.
- **In Claude Code:** `/devlog` or `/devlog --raw` in any project.
- **In terminal:** `~/.claude/devlogs/view.sh` or `~/.claude/devlogs/view.sh --raw`

Optionally add shell aliases:
```bash
alias devlog='~/.claude/devlogs/view.sh'
alias devlograw='~/.claude/devlogs/view.sh --raw'
```

---

本目录是这套系统的**备份 + 说明 + 阅读入口**。

---

## 1. 核心原理：Claude Code 本来就记录了一切

每个会话都会被完整写到：

```
~/.claude/projects/<项目slug>/<session-id>.jsonl
```

每行是一个 JSON 事件，已经包含你要的全部内容：

| 你想记录的 | JSONL 里的位置 |
|---|---|
| 你的指令 | `type:"user"`，`message.content` 是字符串 |
| Claude 的回复 | `type:"assistant"`，`message.content[]` 里的 `text` 块 |
| CLI 命令 + 输出 | `tool_use` 块（`name:"Bash"`、`input.command`）↔ `tool_result` 块，按 `tool_use_id` 配对 |

所以「记录」从来不是问题。真正的痛点是：JSONL 不好读、按会话分散、默认 **30 天自动删除**。本系统只做三件事——**可读化、汇总、保久**。

---

## 2. 架构：解析与渲染分离

`render_devlog.py` 分两层：

- **解析器**：把 JSONL 解析成一组类型化事件（`Prompt` / `Reply` / `BashCall` / `ToolResult` / `FileEdit` / `FileWrite` / `FileRead` …）。
- **渲染器**：把事件输出成 Markdown。`MarkdownRenderer` 已实现；将来要加 HTML 渲染器，只需新增一个复用同一解析器的类，**不用重新解析**。

> 关键：把「理解 JSONL」和「产出格式」解耦，方便以后扩展（HTML、JSON、过滤、搜索……）。

---

## 3. 三个组件

| 组件 | 位置 | 作用 |
|---|---|---|
| 转换器 | `~/.claude/devlogs/render_devlog.py` | JSONL → Markdown |
| `/devlog` 技能 | `~/.claude/skills/devlog/SKILL.md` | 在 Claude Code 里输入 `/devlog` 全量重建并打开 |
| 配置 | `~/.claude/settings.json` | `cleanupPeriodDays:365` + `Stop` 钩子 |

---

## 4. Hook 激活时机（重点）

Claude Code 的**钩子（hooks）**在特定事件发生时执行你配置的命令。本系统用的是 **`Stop`** 钩子。

### `Stop` 什么时候触发？
- **Claude 每一轮回答结束、把控制权交还给你之前**触发一次。
- 本系统配置了 `async: true`，意味着转换器在**后台**运行，**不拖慢你的对话**。
- 钩子通过 **stdin** 收到一段 JSON（含 `transcript_path` / `cwd` / `session_id`），转换器据此刷新**当前项目**的日志。钩子用 `--both`，每轮**同时**产出 `devlog.md`（分组视图）和 `devlog-raw.html`（原始时序视图），两者都自动保持最新。

### 钩子什么时候加载？
- **Claude Code 启动时**读取 `settings.json` 里的钩子。
- 所以**新增 / 修改钩子后，需要重启 Claude Code 才生效**。
  - 重启后，每一轮结束都会自动刷新日志；重启前，请用 `/devlog` 手动生成。

### 其他常见钩子时机（供参考 / 二次开发）

| 钩子事件 | 触发时机 | 能拿到什么 |
|---|---|---|
| `SessionStart` | 会话开始 / 恢复 | session 元信息 |
| `UserPromptSubmit` | 你提交指令、Claude 处理之前 | 你的 `prompt` 文本 |
| `PreToolUse` | 工具执行之前（可阻断） | `tool_name`、`tool_input`（如 Bash 的 command） |
| `PostToolUse` | 工具执行之后 | `tool_input` + `tool_response`（含 stdout/exit_code） |
| `Stop` | 主 agent 每轮结束 ← **本系统用这个** | `transcript_path`、`last_assistant_message` |
| `SubagentStop` | 子 agent 结束 | 同上，针对子 agent |

> 钩子契约：stdin 收 JSON；退出码 `0`=成功、`2`=阻断（stderr 反馈给 Claude）、其它=非阻断告警；stdout 可回传 JSON 控制行为。

### 为什么用 `Stop` 而不是 `PostToolUse`？
- `PostToolUse` 在**每条命令后**都触发，太频繁。
- `Stop` 在**每轮结束**触发一次，频次合适，且此时本轮所有命令+输出都已落盘到 JSONL，一次性渲染即可。

---

## 5. 生成的 Markdown 结构（为转 HTML 而设计）

只用**标准、可移植**的 Markdown，每个结构都能干净地映射成 HTML：

```markdown
---                              ← YAML front-matter → HTML <head> 元信息
project: logCC
generated_at: 2026-07-09T...
---

# Development Log · logCC        ← h1 项目

## Table of Contents             ← 目录，锚点可深链

## Session 5717d1e5 — <标题>     ← h2 会话（含元信息表）

### Turn 1 · 2026-07-09 19:47 (UTC+8) — <指令摘要>   ← h3 每一轮（时间固定显示 UTC+8，见下方说明）

#### Prompt                      ← h4 角色
> 你的指令（blockquote）

#### Reply
Claude 的回复（回复里的标题会被降级，不破坏大纲）

#### Actions
**Bash** · exit 0 · 1.2 KB
​```bash
$ <命令>
​```
<details><summary>Output</summary>     ← 可折叠输出

​```
<stdout/stderr，超长则截断>
​```

</details>

**Edit** · `src/app.py`
​```diff                            ← 编辑以 diff 呈现（红删绿增）
- old
+ new
​```
```

### 转换器强制遵守的 5 条可移植规则
1. `<details>` 内容前后**留空行**，确保 pandoc / markdown-it / Python-Markdown 都能解析内部代码。
2. **降级回复里的标题**（`#`/`##` → 至少 `#####`），不抢文档大纲、不破坏 TOC。
3. **栅栏转义**：输出里若含 ``` ``` ```，外层围栏用 4 个以上反引号，避免代码块断裂。
4. **一个事件一块**，块之间用空行 + `---` 分隔。
5. **标题唯一**（含轮次号 + 时间戳），锚点不撞车。

---

## 6. 文件位置

| | 路径 |
|---|---|
| 转换器（在线） | `~/.claude/devlogs/render_devlog.py` |
| 技能（在线） | `~/.claude/skills/devlog/SKILL.md` |
| 配置 | `~/.claude/settings.json` |
| **生成的日志** | `~/.claude/devlogs/<项目slug>/devlog.md` |
| 本项目的日志 | `~/.claude/devlogs/-Users-ping-Documents-playground-logCC/devlog.md` |

> 日志刻意**全局存放、不进 git**——因为命令输出里可能含密钥（token、env、密文等）。

---

## 7. 怎么用 / 怎么读

### 方式 A：一键阅读（推荐）
```bash
./view.sh                     # 刷新当前项目 md → 生成带样式 HTML → 浏览器打开
./view.sh /path/to/other      # 看别的项目
./view.sh --no-open           # 只生成，不弹浏览器
./view.sh --raw               # 原始时序视图（见下方「原始视图」）
```
`view.sh` 会用 `style.css` + pandoc 生成**单文件、自带样式、可折叠、语法高亮、带目录**的 HTML，离线可看。

### 方式 B：在 Claude Code 里
输入 `/devlog` —— 全量重建并提示打开。

### 方式 C：直接用转换器
```bash
python3 ~/.claude/devlogs/render_devlog.py                 # 当前项目，全量
python3 ~/.claude/devlogs/render_devlog.py --session 5717  # 只渲染某会话
python3 ~/.claude/devlogs/render_devlog.py --max-output 20000   # 放大输出
python3 ~/.claude/devlogs/render_devlog.py --no-details    # 不用 <details>
python3 ~/.claude/devlogs/render_devlog.py --raw           # 原始时序视图
python3 ~/.claude/devlogs/render_devlog.py --project /path # 指定项目
```

### 原始视图（`--raw`）：真实时序的事件流
默认视图把每轮的「回复」和「动作」**分组归类**了。加 `--raw` 则按 **JSONL 的真实顺序**逐条列出事件，每条带**角色/类型标签 + 时间戳 + tool_use_id 配对**，适合需要研究交互轨迹的场景：

```
- `09:12:03` **user/prompt**           ← 你的指令
- `09:12:15` **assistant/thinking**    ← 思考（截断）
- `09:12:15` **assistant/text**        ← 文字（动作之前）
- `09:12:16` **assistant/tool_use · Read**  `id=call_1618…`
- `09:12:16` **user/tool_result**  `→ call_1618…`  ok
- `09:12:23` **assistant/thinking**
- `09:12:29` **assistant/tool_use · Edit**  `id=call_2ad6…`
- `09:12:29` **user/tool_result**  `→ call_2ad6…`  ok
- `09:12:43` **assistant/text**        ← 文字（动作之后）
```

可以看到文字与工具调用**真实交错**（动作前后都有文字），`tool_use` 的 `id=` 与 `tool_result` 的 `→ id=` 一一对应（同 id 同色）。

**长内容折叠**：超过 1000 字符的块默认只显示前 1000，末尾有 `(truncated N chars)` 按钮；点击展开**全部**（文字连续、非分块），展开后**顶部和底部都有** `(部分显示)` 按钮，方便从头或尾收起。原始视图直接生成**独立的 `devlog-raw.html`**（自带样式+脚本，不走 pandoc）。

### 手动转 HTML
```bash
pandoc ~/.claude/devlogs/<slug>/devlog.md \
  -o devlog.html --standalone --embed-resources --toc \
  --highlight-style=tango --css style.css
```

---

## 8. 备份与恢复

本目录是这套文件的**备份点**。要恢复到另一台机器或重装：

```bash
# 1. 转换器
mkdir -p ~/.claude/devlogs && cp render_devlog.py ~/.claude/devlogs/

# 2. 技能
mkdir -p ~/.claude/skills/devlog && cp SKILL.md ~/.claude/skills/devlog/

# 3. 配置：把 settings-additions.json 的内容合并进 ~/.claude/settings.json
#    （注意 Stop 是「追加」到已有数组，别覆盖原有的 hook）
```

> 本目录的 `settings-additions.json` 只含**增量**，**不含密钥**——可以安全提交 git。

---

## 9. 注意事项

- **`cleanupPeriodDays`**：已设为 `365`（保留 1 年）。
  - **千万不要设成 `0`**——`0` 会**完全停止**写 JSONL 日志（不是「永久保留」）。
  - 想永久保留就设大数，比如 `9999`。
- **隐私**：日志含命令输出，可能含密钥 → 全局存放、不进 git。如确需进项目，先检查/过滤敏感内容。
- **钩子生效**：改完 `settings.json` 要**重启 Claude Code**。
- **时区**：JSONL 里存的是 UTC，渲染器固定显示 **UTC+8**（北京时间）。要改时区，改 `render_devlog.py` 顶部的 `DISPLAY_TZ`（如 `hours=0` 回到 UTC、`hours=-7` 用 PDT）。
- **示例文件**：`sample-devlog.md/.html`（分组视图）与 `sample-devlog-raw.html`（原始时序视图，独立 HTML）是一次生成的样例，可随时删除或用 `./view.sh` / `./view.sh --raw` 重新生成。
- **健壮性加固（对抗性审阅后修复）**：以下边界情况已处理，留档备查：
  - **Stop 钩子渲染全项目**：`--stdin` 不再用 `session_id` 过滤——钩子每轮刷新的是该项目的**所有** session，不会把别的会话覆盖丢掉。且钩子用 `--both`，**同时**刷新分组视图（`devlog.md`）和原始时序视图（`devlog-raw.html`）。
  - **原子写**：`devlog.md` 先写临时文件再 `os.replace` 原子替换；并发（钩子 + 手动 `view.sh`）或中途中断都不会留下半个文件。
  - **HTML 注入防护**：所有来自会话记录、放在正文（非代码块/非反引号）的文本都做了 HTML 转义——`<script>` 等只会出现在代码块内（pandoc 自动转义），浏览器里不会执行注入脚本。
  - **围栏感知的标题降级**：`demote_headings` 只改代码块**外**的标题，不会把代码块里的 `# 注释` 改坏。
  - **截断不为负**：`--max-output` 很小时也不再出现 `truncated -N chars`，且不超预算。
  - **路径容错**：`--project` 带尾斜杠能正常工作。
  - **view.sh 容错**：`style.css` 缺失时不再让 pandoc 在 `set -e` 下中止（退回无样式 HTML）。
  - **项目名**：取自会话 `cwd` 的末段目录名，带连字符的目录名（如 `my-project`）不会被截断。

---

## 10. 文件清单

| 文件 | 说明 |
|---|---|
| `README.md` | 本文档 |
| `render_devlog.py` | 转换器（备份副本，与 `~/.claude/devlogs/` 下的一致） |
| `SKILL.md` | `/devlog` 技能（备份副本） |
| `settings-additions.json` | 对 `settings.json` 的增量（无密钥） |
| `view.sh` | 一键阅读脚本 |
| `style.css` | HTML 样式 |
| `sample-devlog.md` | 示例日志（Markdown，分组视图） |
| `sample-devlog.html` | 示例日志（带样式 HTML，分组视图） |
| `sample-devlog-raw.html` | 示例日志（原始时序，独立 HTML） |
