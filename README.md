# baiduocr

独立的百度图片 OCR 工具，从 `/home/ubuntu/deployments/dingding/` 中提取并整理为可复用项目，不依赖 DingTalk 数据结构。目标是让它可以单独部署到其他电脑，并且能被 Hermes Agent 直接调用。

## 功能

- 独立 Python 包和命令行：`baiduocr`
- 支持 Linux、macOS、Windows、WSL
- **图片 OCR**：识别单张图片或批量扫描目录，支持多组凭据轮换，自动在额度/QPS 限流时切换凭据和 endpoint
- **文档解析**：提交 PDF/文档到百度文档解析 API，自动轮询并下载 Markdown/JSON 解析产物，支持多凭证轮替
- 输出纯文本或 JSON，适合 Hermes 调用

## 凭据配置

优先级从高到低：

1. `BAIDU_OCR_CREDENTIALS`
2. `BAIDU_OCR_API_KEY` + `BAIDU_OCR_SECRET_KEY`
3. `~/.ding-cli/config.json` 中的 `baidu_ocr_credentials`
4. `~/.config/baiduocr/config.json` 中的 `baidu_ocr_credentials`

### `BAIDU_OCR_CREDENTIALS` 格式

支持以下格式：

```text
api_key_1:secret_key_1,api_key_2:secret_key_2
```

也支持换行分隔、分号分隔和 JSON 数组。

## 安装

### Linux / macOS / WSL

```bash
cd /path/to/baiduocr
python -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .
```

### Windows PowerShell

```powershell
cd C:\path\to\baiduocr
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -U pip
py -m pip install -e .
```

## 用法

### 单图识别

```bash
baiduocr ./example.png
```

### JSON 输出

```bash
baiduocr ./example.png --json --pretty
```

### 目录批量识别

```bash
baiduocr ./images --glob "*.png,*.jpg,*.jpeg" --delay 0.5 --output results.json
```

### 指定配置文件

```bash
baiduocr ./example.png --config ~/.config/baiduocr/config.json --json
```

### 文档解析 (PDF 等)

```bash
# 独立命令
baiduocr-doc-parse ./report.pdf --out-dir ./output --json

# 或直接模块调用
python -m baiduocr.doc_parser ./report.pdf --out-dir ./output

# 多凭证 + 图表分析
baiduocr-doc-parse ./report.pdf --analysis-chart --recognize-formula

# 生成 llm-wiki ingest 目录结构
baiduocr-doc-parse ./report.pdf --write-wiki-bundle --wiki-date 2024-01-15
```

## Hermes Agent 调用

如果已经安装到环境中，Hermes 可直接调用：

```bash
baiduocr /absolute/path/to/image.png --json --pretty
```

如果未安装为命令，也可以：

```bash
python -m baiduocr.cli /absolute/path/to/image.png --json --pretty
```

建议 Hermes 调用时使用 `--json`，便于后续自动处理。

## 开发与测试

```bash
python -m pip install -e . pytest
pytest
```

## 项目结构

```text
baiduocr/
├── README.md
├── scripts/
│   ├── baiduocr_runner.py
│   └── baidu_doc_parser.py
├── src/baiduocr/
│   ├── __init__.py
│   ├── cli.py
│   ├── client.py
│   └── doc_parser.py
└── tests/
    ├── test_client.py
    └── test_doc_parser.py
```

## 说明

- 本项目没有修改 `/home/ubuntu/deployments/dingding/` 下任何文件。
- OCR 核心逻辑来源于该目录现有实现，并整理为独立、可移植版本。
- GitHub 发布前请先确认你希望使用的仓库名与远端账号。
