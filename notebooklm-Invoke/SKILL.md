---
name: notebooklm
description: NotebookLM CLI wrapper via `python3 {baseDir}/scripts/notebooklm.py` (backed by notebooklm-py). Use for auth, notebooks, chat, sources, notes, sharing, research, and artifact generation/download.
---

# NotebookLM CLI Wrapper (Python)

## Required parameters
- `python3` available.
- `notebooklm-py` installed (CLI binary: `notebooklm`).
- NotebookLM authenticated (`login`).

## Quick start
- Wrapper script: `scripts/notebooklm.py`.
- Command form: `python3 {baseDir}/scripts/notebooklm.py <command> [args...]`.

```bash
python3 {baseDir}/scripts/notebooklm.py login
python3 {baseDir}/scripts/notebooklm.py list
python3 {baseDir}/scripts/notebooklm.py use <notebook_id>
python3 {baseDir}/scripts/notebooklm.py status
python3 {baseDir}/scripts/notebooklm.py ask "Summarize the key takeaways" --notebook <notebook_id>
```

## Output guidance
- Prefer `--json` for machine-readable output where supported.
- Long-running waits are handled by native commands like:
  - `source wait`
  - `artifact wait`
  - `research wait`

## ⚡ Sub-Agent Delegation (Anti-Blocking)

### Problem
NotebookLM operations like `source wait`, `artifact wait`, `research wait`, `generate slide-deck`, and `source add-research` can take **minutes** to complete. Running them in the main session blocks the conversation.

### Strategy
For any operation expected to take >30 seconds, **delegate to a sub-agent** via `sessions_spawn`:

1. **Main session**: Acknowledge the user's request, then spawn a sub-agent with a clear task description.
2. **Sub-agent**: Executes the long-running NotebookLM commands, waits for completion, and reports back.
3. **Main session**: Remains responsive. The sub-agent auto-announces completion.

### Which operations to delegate

| Operation | Delegate? | Reason |
|-----------|-----------|--------|
| `login`, `status`, `list`, `use`, `clear` | ❌ No | Fast (<5s) |
| `ask` (chat) | ❌ No | Usually fast (~10s) |
| `source list`, `source get`, `note list` | ❌ No | Fast reads |
| `source add` (URL/text) | ⚠️ Maybe | Fast to submit, but `source wait` after is slow |
| `source add-research` | ✅ Yes | Deep research can take 2-5 min |
| `source wait` | ✅ Yes | Polling wait, unpredictable duration |
| `generate slide-deck` + `artifact wait` | ✅ Yes | Generation takes 1-5 min |
| `research wait` | ✅ Yes | Can take several minutes |
| `download slide-deck` | ⚠️ Maybe | Usually fast, but can be slow for large files |
| Multi-step workflows (add sources → wait → generate → wait → download) | ✅ Yes | Compound long tasks |

### How to spawn

```
sessions_spawn:
  task: |
    You are a NotebookLM task runner. Execute the following NotebookLM operations
    and report results when done.

    Notebook ID: <notebook_id>
    Commands to run (in order):
    1. <command 1>
    2. <command 2>
    ...

    Use the CLI wrapper: python3 ~/.openclaw/skills/notebooklm-Invoke/scripts/notebooklm.py
    Prefer --json output where supported.
    If any step fails, report the error and stop.
    When complete, summarize what was accomplished and any output files created.
  mode: run
  label: notebooklm-<short-description>
```

### Example: Generate slide deck

**User**: "帮我用 notebook X 生成一个 PPT"

**Main session response**:
> 好的，我派了一个后台任务去生成 PPT，完成后会通知你 ✧

**Spawn**:
```
sessions_spawn:
  task: |
    NotebookLM task: Generate a slide deck from notebook.

    Steps:
    1. python3 ~/.openclaw/skills/notebooklm-Invoke/scripts/notebooklm.py generate slide-deck "Create a comprehensive slide deck" --notebook <id>
    2. python3 ~/.openclaw/skills/notebooklm-Invoke/scripts/notebooklm.py artifact wait <artifact_id> --notebook <id> --timeout 600 --json
    3. python3 ~/.openclaw/skills/notebooklm-Invoke/scripts/notebooklm.py download slide-deck ./output.pptx --notebook <id> --latest --format pptx

    Report: artifact details, file path, any errors.
  mode: run
  label: notebooklm-slide-deck
```

### Example: Add research source

**User**: "在 notebook Y 里加一个关于碳足迹的深度研究"

**Spawn**:
```
sessions_spawn:
  task: |
    NotebookLM task: Add deep research source.

    Steps:
    1. python3 ~/.openclaw/skills/notebooklm-Invoke/scripts/notebooklm.py source add-research "碳足迹最新研究进展" --mode deep --notebook <id>
    2. python3 ~/.openclaw/skills/notebooklm-Invoke/scripts/notebooklm.py research wait --notebook <id> --timeout 600
    3. python3 ~/.openclaw/skills/notebooklm-Invoke/scripts/notebooklm.py source list --notebook <id> --json

    Report: research status, new sources added, any errors.
  mode: run
  label: notebooklm-research
```

### Guidelines

- **Always tell the user** you're delegating to a background task before spawning.
- **Use `mode: run`** (one-shot) — no need for persistent sessions.
- **Use descriptive labels** like `notebooklm-slide-deck`, `notebooklm-research-carbon` for easy tracking.
- **Include all context in the task** — the sub-agent has no conversation history.
- **Error handling**: Instruct the sub-agent to report errors clearly so you can relay them.
- **File paths**: Use absolute paths for output files so the main session can find them.
- **Compound workflows**: Bundle related steps (add → wait → generate → wait → download) into a single sub-agent task rather than spawning multiple.

## PPT generation policy
- A single generated slide deck should target **at most 15 pages**.
- If user requirements exceed 15 pages, split into multiple decks (e.g., Part 1/2/3) and generate separately.
- After generation, provide downloadable **`.pptx`** output when possible:
  - `download slide-deck ... --format pptx`

## References
- `README.md` (installation, requirements, troubleshooting)
- `QUICKSTART_CN.md`（中文快速上手）
- `references/cli-commands.md`

## Assets
- None.
