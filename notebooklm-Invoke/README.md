# notebooklm-Invoke

NotebookLM skill wrapper using **Python** (`notebooklm-py`).

- Wrapper entry: `scripts/notebooklm.py`
- Command form: `python3 {baseDir}/scripts/notebooklm.py <command> [args...]`

---

## 1) Requirements

### Core
- Python 3.10+
- `notebooklm-py`
- Playwright + Chromium (required for `login`)

### Install (Linux/macOS)
```bash
python3 -m pip install --user -U notebooklm-py
python3 -m pip install --user -U playwright
python3 -m playwright install chromium
```

If your Python is PEP668-managed (common on Ubuntu), use:
```bash
python3 -m pip install --user -U notebooklm-py --break-system-packages
python3 -m pip install --user -U playwright --break-system-packages
```

### Linux system deps (Playwright runtime)
```bash
sudo playwright install-deps
```
Or install required libs manually (`libatk`, `libpango`, `libasound`, etc.).

---

## 2) PATH setup

If `notebooklm` is not found, add user bin to PATH.

### macOS (zsh)
```bash
echo 'export PATH="$(python3 -m site --user-base)/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Linux (bash)
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

Verify:
```bash
notebooklm --version
```

---

## 3) Authentication

### Interactive login (requires GUI browser)
```bash
python3 {baseDir}/scripts/notebooklm.py login
```

> `login` needs a visible browser session. In pure headless servers (no X server / no `$DISPLAY`), interactive login will fail.

### Headless/server workflow (recommended)
1. Login on your local machine.
2. Copy `~/.notebooklm/storage_state.json` to server `~/.notebooklm/storage_state.json`.
3. Validate on server:
```bash
python3 {baseDir}/scripts/notebooklm.py auth check
python3 {baseDir}/scripts/notebooklm.py list --json
```

---

## 4) Quick start (MVP)

> PPT policy: one deck should target **<=15 pages**. If more pages are needed, split into multiple decks and generate in batches.

```bash
# Check auth
python3 {baseDir}/scripts/notebooklm.py auth check

# List notebooks
python3 {baseDir}/scripts/notebooklm.py list --json

# Set active notebook
python3 {baseDir}/scripts/notebooklm.py use <notebook_id>

# Ask question
python3 {baseDir}/scripts/notebooklm.py ask "Summarize key points" --json

# Generate slide deck
python3 {baseDir}/scripts/notebooklm.py generate slide-deck "Create 10 slides for executives" --notebook <notebook_id>

# Wait for artifact and download
python3 {baseDir}/scripts/notebooklm.py artifact list --notebook <notebook_id> --json
python3 {baseDir}/scripts/notebooklm.py artifact wait <artifact_id> --notebook <notebook_id> --timeout 600 --json

# Preferred download format: PPTX
python3 {baseDir}/scripts/notebooklm.py download slide-deck ./slides.pptx --notebook <notebook_id> --latest --format pptx

# Optional: PDF
python3 {baseDir}/scripts/notebooklm.py download slide-deck ./slides.pdf --notebook <notebook_id> --latest --format pdf
```

---

## 5) Common issues

### `Playwright not installed`
Install:
```bash
python3 -m pip install --user -U playwright
python3 -m playwright install chromium
```

### `Missing X server or $DISPLAY`
You are on headless server. Do local login and copy `storage_state.json` to server.

### `notebooklm: command not found`
Fix PATH (see section 2), or invoke directly:
```bash
$(python3 -m site --user-base)/bin/notebooklm --version
```

### `auth check` passes but commands fail
- Re-login locally and replace `storage_state.json`
- Confirm account has access to target notebook
- Re-run with verbose mode for diagnostics:
```bash
notebooklm -vv list
```

---

## 6) Security notes

- `storage_state.json` contains sensitive session cookies. Keep permission strict (`chmod 600`) and never commit to git.
- Use dedicated account/session for automation where possible.
