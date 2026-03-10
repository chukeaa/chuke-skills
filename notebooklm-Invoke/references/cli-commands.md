# NotebookLM CLI command catalog (aligned to notebooklm-py)

Command prefix:

```bash
python3 {baseDir}/scripts/notebooklm.py
```

Notes:
- Global options from underlying CLI include `--storage` and `-v/--verbose`.
- Prefer `--json` when available.
- Most commands accept `-n/--notebook <id>`; if omitted, use `use <id>` first.
- IDs support partial match in many commands.

## Session / Auth

```bash
python3 {baseDir}/scripts/notebooklm.py login
python3 {baseDir}/scripts/notebooklm.py status
python3 {baseDir}/scripts/notebooklm.py use <notebook_id>
python3 {baseDir}/scripts/notebooklm.py clear
python3 {baseDir}/scripts/notebooklm.py auth check
```

## Notebooks

```bash
python3 {baseDir}/scripts/notebooklm.py list --json
python3 {baseDir}/scripts/notebooklm.py create "Research Notebook" --json
python3 {baseDir}/scripts/notebooklm.py rename <notebook_id> "New Title"
python3 {baseDir}/scripts/notebooklm.py delete <notebook_id>
python3 {baseDir}/scripts/notebooklm.py summary --notebook <notebook_id> --topics
```

## Chat

```bash
python3 {baseDir}/scripts/notebooklm.py ask "What are the top risks?" --notebook <notebook_id> --json
python3 {baseDir}/scripts/notebooklm.py configure --notebook <notebook_id> --mode concise
python3 {baseDir}/scripts/notebooklm.py history --notebook <notebook_id> --limit 20
```

## Sources

```bash
python3 {baseDir}/scripts/notebooklm.py source list --notebook <notebook_id> --json
python3 {baseDir}/scripts/notebooklm.py source add https://example.com --notebook <notebook_id> --json
python3 {baseDir}/scripts/notebooklm.py source add "Inline notes" --title "Meeting" --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source add-drive <file_id> "Drive Doc" --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source add-research "market analysis" --mode deep
python3 {baseDir}/scripts/notebooklm.py source get <source_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source guide <source_id> --notebook <notebook_id> --json
python3 {baseDir}/scripts/notebooklm.py source fulltext <source_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source rename <source_id> "New Title" --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source delete <source_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source refresh <source_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source stale <source_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source wait <source_id> --notebook <notebook_id> --timeout 300 --json
```

## Artifacts

> Slide deck best practice: target <=15 slides per generation. If more are needed, split into multiple decks and generate separately.

```bash
python3 {baseDir}/scripts/notebooklm.py generate slide-deck "Create a 10-slide executive summary" --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py artifact list --notebook <notebook_id> --json
python3 {baseDir}/scripts/notebooklm.py artifact get <artifact_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py artifact rename <artifact_id> "New Title" --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py artifact delete <artifact_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py artifact export <artifact_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py artifact suggestions --notebook <notebook_id> --json
python3 {baseDir}/scripts/notebooklm.py download slide-deck ./slides.pptx --notebook <notebook_id> --latest --format pptx
python3 {baseDir}/scripts/notebooklm.py artifact wait <artifact_id> --notebook <notebook_id> --timeout 600 --json
```

## Notes

```bash
python3 {baseDir}/scripts/notebooklm.py note create "Key points" --title "Highlights" --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py note list --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py note get <note_id> --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py note save <note_id> --content "Updated notes" --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py note rename <note_id> "New Title" --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py note delete <note_id> --notebook <notebook_id>
```

## Sharing

```bash
python3 {baseDir}/scripts/notebooklm.py share status --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py share add user@example.com --permission editor --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py share update user@example.com --permission viewer --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py share remove user@example.com --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py share public --enable --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py share view-level full --notebook <notebook_id>
```

## Research

```bash
python3 {baseDir}/scripts/notebooklm.py research status --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py research wait --notebook <notebook_id> --timeout 600
```

## Language / Skill

```bash
python3 {baseDir}/scripts/notebooklm.py language list
python3 {baseDir}/scripts/notebooklm.py language get
python3 {baseDir}/scripts/notebooklm.py language set zh_Hans
python3 {baseDir}/scripts/notebooklm.py skill status
python3 {baseDir}/scripts/notebooklm.py skill install
```
