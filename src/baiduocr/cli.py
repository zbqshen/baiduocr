from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .client import BaiduOCR, BaiduOCRQuotaExhausted, OCRResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone Baidu OCR CLI")
    parser.add_argument("inputs", nargs="+", help="Image file(s) or directories")
    parser.add_argument("--glob", default="*.png,*.jpg,*.jpeg,*.webp,*.bmp", help="Directory scan glob list")
    parser.add_argument("--delay", type=float, default=0.0, help="Delay between OCR requests in seconds")
    parser.add_argument("--config", default="", help="Optional config.json path with baidu_ocr_credentials")
    parser.add_argument("--credentials", default="", help="Inline BAIDU_OCR_CREDENTIALS override")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--no-fallback", action="store_true", help="Disable retry without preprocessing")
    parser.add_argument("--output", default="", help="Write JSON results to file")
    return parser


def expand_inputs(inputs: list[str], glob_patterns: str) -> list[Path]:
    patterns = [item.strip() for item in glob_patterns.split(",") if item.strip()]
    paths: list[Path] = []
    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_dir():
            for pattern in patterns:
                paths.extend(sorted(p for p in path.rglob(pattern) if p.is_file()))
        elif path.is_file():
            paths.append(path)
    unique: list[Path] = []
    seen = set()
    for path in paths:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def format_text_result(path: Path, result: OCRResult) -> str:
    lines = [f"==> {path}"]
    if result.error:
        lines.append(f"ERROR: {result.error}")
    lines.append(f"endpoint: {result.endpoint or '-'}")
    lines.append(f"confidence: {result.confidence:.4f}")
    lines.append(f"preprocessed: {str(result.preprocessed).lower()}")
    lines.append(result.text or "")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser() if args.config else None
    paths = expand_inputs(args.inputs, args.glob)
    if not paths:
        parser.error("No image files found")

    ocr = BaiduOCR(credentials_raw=args.credentials or None, config_path=config_path)
    results = []
    had_errors = False
    for index, path in enumerate(paths):
        if index and args.delay > 0:
            import time
            time.sleep(args.delay)
        try:
            result = ocr.recognize(path, fallback_without_preprocess=not args.no_fallback)
            row = {"path": str(path), **result.to_dict()}
        except BaiduOCRQuotaExhausted as exc:
            row = {"path": str(path), "text": "", "confidence": 0.0, "endpoint": "", "preprocessed": True, "error": str(exc)}
            had_errors = True
        except Exception as exc:
            row = {"path": str(path), "text": "", "confidence": 0.0, "endpoint": "", "preprocessed": True, "error": str(exc)}
            had_errors = True
        results.append(row)

    if args.json or args.output:
        payload = json.dumps(results, ensure_ascii=False, indent=2 if args.pretty else None)
        if args.output:
            out_path = Path(args.output).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(payload + ("\n" if not payload.endswith("\n") else ""), encoding="utf-8")
        if args.json:
            print(payload)
    else:
        for row in results:
            print(format_text_result(Path(row["path"]), OCRResult(
                text=row["text"],
                confidence=float(row["confidence"]),
                endpoint=row["endpoint"],
                preprocessed=bool(row["preprocessed"]),
                error=row["error"],
            )))

    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
