---
name: baiduocr
description: "Standalone Baidu OCR for images; reusable from Hermes on Linux, macOS, Windows, and WSL."
version: 0.1.0
author: OpenAI Codex via Hermes
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [baidu, ocr, images, windows, cli]
---

# BaiduOCR

Use this skill when the user wants Baidu image OCR outside the DingTalk project, or wants a portable OCR helper deployable on other machines.

## What it provides

- Standalone Python package and CLI under `baiduocr/`
- Compatible with Windows, Linux, macOS, and WSL
- Reads credentials from:
  - `BAIDU_OCR_CREDENTIALS`
  - `BAIDU_OCR_API_KEY` + `BAIDU_OCR_SECRET_KEY`
  - `~/.ding-cli/config.json` (`baidu_ocr_credentials`)
  - `~/.config/baiduocr/config.json`
- Supports multi-credential rotation and endpoint fallback when Baidu quota/rate limits are hit

## Install

```bash
cd /path/to/baiduocr
python -m pip install -e .
```

Windows PowerShell:

```powershell
cd C:\path\to\baiduocr
py -m pip install -e .
```

## Run

Single image:

```bash
baiduocr ./image.png --json --pretty
```

Directory batch:

```bash
baiduocr ./images --glob "*.png,*.jpg" --delay 0.5 --output results.json
```

## Hermes usage

In Hermes, prefer shell invocation so the result is machine-readable:

```bash
baiduocr /absolute/path/to/image.png --json --pretty
```

or without install:

```bash
python -m baiduocr.cli /absolute/path/to/image.png --json --pretty
```

## Notes

- Do not hardcode secrets into source.
- When packaging for another computer, copy the repo and install dependencies there.
- On Windows, if `python` is not available, use `py`.
