# notebooklm-Invoke 快速上手（中文）

> 目标：用最少步骤跑通 notebooklm skill（安装 → 认证 → 提问 → 生成 PPT）。

## 1. 安装依赖

```bash
python3 -m pip install --user -U notebooklm-py
python3 -m pip install --user -U playwright
python3 -m playwright install chromium
```

如果 Ubuntu 提示 externally-managed-environment：

```bash
python3 -m pip install --user -U notebooklm-py --break-system-packages
python3 -m pip install --user -U playwright --break-system-packages
```

Linux 额外系统依赖：

```bash
sudo playwright install-deps
```

---

## 2. 配置 PATH

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

校验：

```bash
notebooklm --version
```

---

## 3. 登录认证

### 有图形界面（推荐）
```bash
python3 {baseDir}/scripts/notebooklm.py login
```
按提示在浏览器完成 Google 登录。

### 无图形界面服务器（推荐流程）
1) 在本地电脑先 `login`
2) 将本地 `~/.notebooklm/storage_state.json` 复制到服务器同路径
3) 在服务器验证：

```bash
python3 {baseDir}/scripts/notebooklm.py auth check
python3 {baseDir}/scripts/notebooklm.py list --json
```

---

## 4. 常用命令

```bash
# 列出 notebooks
python3 {baseDir}/scripts/notebooklm.py list --json

# 设置默认 notebook
python3 {baseDir}/scripts/notebooklm.py use <notebook_id>

# 提问
python3 {baseDir}/scripts/notebooklm.py ask "请总结这份材料的重点" --json

# 添加 source（URL/文本）
python3 {baseDir}/scripts/notebooklm.py source add https://example.com --notebook <notebook_id>
python3 {baseDir}/scripts/notebooklm.py source add "这里是文本内容" --title "说明" --notebook <notebook_id>
```

---

## 5. 生成 PPT（Slide Deck）

> 规则：单次生成默认不超过 **15 页**。超过 15 页时请拆成多个 deck（如 Part 1/2/3）分别生成。

```bash
# 1) 生成
python3 {baseDir}/scripts/notebooklm.py generate slide-deck "做一份 10 页执行汇报" --notebook <notebook_id>

# 2) 查看/等待
python3 {baseDir}/scripts/notebooklm.py artifact list --notebook <notebook_id> --json
python3 {baseDir}/scripts/notebooklm.py artifact wait <artifact_id> --notebook <notebook_id> --timeout 600 --json

# 3) 下载（优先 .pptx）
python3 {baseDir}/scripts/notebooklm.py download slide-deck ./slides.pptx --notebook <notebook_id> --latest --format pptx

# 可选：下载 PDF
python3 {baseDir}/scripts/notebooklm.py download slide-deck ./slides.pdf --notebook <notebook_id> --latest --format pdf
```

---

## 6. 常见问题

### 1) `Playwright not installed`
```bash
python3 -m pip install --user -U playwright
python3 -m playwright install chromium
```

### 2) `Missing X server or $DISPLAY`
当前环境无图形界面。请走“本地登录 + 复制 storage_state.json”方案。

### 3) `notebooklm: command not found`
PATH 未配置好，先执行：

```bash
$(python3 -m site --user-base)/bin/notebooklm --version
```

若能输出版本，再把该目录加到 PATH。
