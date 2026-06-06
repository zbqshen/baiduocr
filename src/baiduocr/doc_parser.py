#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度智能云「文档解析」模块 (Doc Parser API)

百度文档解析接口（异步）：提交 PDF/文档 → 轮询结果 → 下载 Markdown/JSON 产物。
与图片 OCR (client.py) 共用同一套多凭证轮替机制。

功能：
- 支持本地 PDF/文档通过 file_data(base64) 提交解析任务
- 支持多组 API_KEY/SECRET_KEY 轮替（复用 baiduocr 凭证体系）
- 一组鉴权/提交失败或额度类错误时自动切换下一组
- 自动轮询任务结果，并下载 markdown/json 解析产物
- Markdown 清洗工具（复盘哥类内容页眉/广告/留言截断）
- llm-wiki ingest bundle 生成

用法：
    # 命令式（独立 entry point）
    baiduocr-doc-parse document.pdf --out-dir ./output
    
    # 或 python -m
    python -m baiduocr.doc_parser document.pdf --out-dir ./output
    
    # Python API
    from baiduocr.doc_parser import BaiduDocParser
    from baiduocr.client import load_baidu_ocr_credentials
    creds = load_baidu_ocr_credentials()
    parser = BaiduDocParser(creds)
    result = parser.parse_file(Path("doc.pdf"), Path("./out"), {}, 8, 1800)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date as dt_date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from .client import BaiduOCRCredential, load_baidu_ocr_credentials

# 百度「文档解析」API 端点
DOC_PARSER_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
DOC_PARSER_SUBMIT_URL = "https://aip.baidubce.com/rest/2.0/brain/online/v2/parser/task"
DOC_PARSER_QUERY_URL = "https://aip.baidubce.com/rest/2.0/brain/online/v2/parser/task/query"

# 百度通用错误码中表明“换 key 可能恢复”的情形，与 client.py 保持一致但专用于文档解析。
DOC_PARSER_ROTATE_ERROR_KEYWORDS = (
    "quota", "limit", "qps", "permission", "denied", "IAM", "No permission",
    "Open api daily request limit reached", "rate",
    "余额", "额度", "限额", "欠费", "无权限", "鉴权", "权限",
)
DOC_PARSER_ROTATE_ERROR_CODES = {
    4,      # Open api request limit reached
    6,      # No permission to access data
    17,     # Open api daily request limit reached
    18,     # Open api qps request limit reached
    19,     # Open api total request limit reached
    100,    # invalid parameter/access token sometimes
    110,    # Access token invalid or no longer valid
    111,    # Access token expired
}


class DocParserError(RuntimeError):
    """文档解析 API 错误。"""
    def __init__(self, stage: str, data: Dict[str, Any]):
        self.stage = stage
        self.data = data
        super().__init__(f"{stage}: {_sanitize_json(data)}")


class DocParserQuotaExhausted(RuntimeError):
    """所有凭证均无法完成文档解析。"""
    pass


class BaiduDocParser:
    """百度文档解析器：提交→轮询→下载，支持多凭证轮替。"""

    def __init__(self, credentials: List[BaiduOCRCredential], timeout: int = 60):
        if not credentials:
            raise ValueError("至少需要一组百度 API 凭证")
        self.credentials = credentials
        self.timeout = timeout
        self.session = requests.Session()
        self._token_cache: Dict[int, str] = {}

    def _get_access_token(self, idx: int, force_refresh: bool = False) -> str:
        if idx in self._token_cache and not force_refresh:
            return self._token_cache[idx]
        cred = self.credentials[idx]
        resp = self.session.post(
            DOC_PARSER_TOKEN_URL,
            params={
                "grant_type": "client_credentials",
                "client_id": cred.api_key,
                "client_secret": cred.secret_key,
            },
            timeout=self.timeout,
        )
        data = _safe_json(resp)
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"获取 access_token 失败: {_sanitize_json(data)}")
        self._token_cache[idx] = str(token)
        return str(token)

    def _post_form(self, url: str, payload: Dict[str, Any], cred_idx: int, force_refresh_token: bool = False) -> Dict[str, Any]:
        token = self._get_access_token(cred_idx, force_refresh=force_refresh_token)
        resp = self.session.post(
            f"{url}?access_token={token}",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data=payload,
            timeout=self.timeout,
        )
        return _safe_json(resp)

    def _should_rotate(self, exc: Exception) -> bool:
        """判断异常是否需要换一组凭证重试。"""
        if isinstance(exc, DocParserError):
            code = exc.data.get("error_code")
            if isinstance(code, int) and code in DOC_PARSER_ROTATE_ERROR_CODES:
                return True
            text = _sanitize_json(exc.data)
        else:
            text = str(exc)
        return any(k.lower() in text.lower() for k in DOC_PARSER_ROTATE_ERROR_KEYWORDS)

    def submit(self, file_path: Path, cred_idx: int, options: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """提交文档解析任务，返回 (task_id, 原始响应)。"""
        b64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
        payload: Dict[str, Any] = {
            "file_data": b64,
            "file_name": file_path.name,
            "recognize_formula": str(options.get("recognize_formula", False)),
            "analysis_chart": str(options.get("analysis_chart", False)),
            "angle_adjust": str(options.get("angle_adjust", False)),
            "parse_image_layout": str(options.get("parse_image_layout", False)),
            "language_type": options.get("language_type", "CHN_ENG"),
            "switch_digital_width": options.get("switch_digital_width", "auto"),
            "html_table_format": str(options.get("html_table_format", True)),
        }
        if options.get("return_doc_chunks"):
            payload["return_doc_chunks"] = json.dumps(options["return_doc_chunks"], ensure_ascii=False)
        data = self._post_form(DOC_PARSER_SUBMIT_URL, payload, cred_idx)
        ec = data.get("error_code")
        if ec is not None and int(ec) != 0:
            raise DocParserError("submit", data)
        task_id = data.get("result", {}).get("task_id")
        if not task_id:
            raise DocParserError("submit_no_task_id", data)
        return str(task_id), data

    def query(self, task_id: str, cred_idx: int) -> Dict[str, Any]:
        """查询文档解析任务状态/结果。"""
        data = self._post_form(DOC_PARSER_QUERY_URL, {"task_id": task_id}, cred_idx)
        ec = data.get("error_code")
        if ec is not None and int(ec) != 0:
            access_token_errors = {110, 111}
            if int(ec) in access_token_errors:
                data = self._post_form(DOC_PARSER_QUERY_URL, {"task_id": task_id}, cred_idx, force_refresh_token=True)
                ec2 = data.get("error_code")
                if ec2 is not None and int(ec2) != 0:
                    raise DocParserError("query", data)
            else:
                raise DocParserError("query", data)
        return data

    def parse_file(
        self,
        file_path: Path,
        out_dir: Path,
        options: Dict[str, Any],
        poll_interval: int = 8,
        max_wait: int = 1800,
    ) -> Dict[str, Any]:
        """完整流程：多凭证尝试 → 提交 → 轮询 → 下载产物。
        
        Returns:
            {
                "credential_index": int,
                "task_id": str,
                "submit_response": dict,
                "query_response": dict,
                "saved_files": {"markdown_url": str, "parse_result_url": str, "query_response": str}
            }
        """
        last_errors: List[str] = []
        for idx, cred in enumerate(self.credentials):
            print(f"[INFO] 使用凭证 #{idx + 1}", file=sys.stderr)
            try:
                task_id, submit_resp = self.submit(file_path, idx, options)
                print(f"[INFO] 提交成功 task_id={task_id}", file=sys.stderr)
                result = self._poll_until_done(task_id, idx, poll_interval, max_wait)
                saved = self._download_outputs(result, out_dir, file_path.stem)
                return {
                    "credential_index": idx,
                    "task_id": task_id,
                    "submit_response": submit_resp,
                    "query_response": result,
                    "saved_files": saved,
                }
            except Exception as e:
                msg = f"凭证 #{idx + 1} 失败: {e}"
                last_errors.append(msg)
                print(f"[WARN] {msg}", file=sys.stderr)
                if idx == len(self.credentials) - 1:
                    break
                if self._should_rotate(e):
                    print("[INFO] 尝试下一组凭证...", file=sys.stderr)
                    continue
                print("[INFO] 当前凭证报错，尝试下一组凭证...", file=sys.stderr)
                continue
        raise DocParserQuotaExhausted("所有凭证均解析失败:\n" + "\n".join(last_errors))

    def _poll_until_done(self, task_id: str, cred_idx: int, poll_interval: int, max_wait: int) -> Dict[str, Any]:
        deadline = time.time() + max_wait
        attempt = 0
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            attempt += 1
            data = self.query(task_id, cred_idx)
            last = data
            result = data.get("result") or {}
            status = result.get("status")
            print(f"[INFO] 轮询 {attempt}: status={status}", file=sys.stderr)
            if status == "success":
                return data
            if status == "failed":
                raise DocParserError("task_failed", data)
            time.sleep(poll_interval)
        raise TimeoutError(f"等待任务超时，最后响应: {_sanitize_json(last)}")

    def _download_outputs(self, query_resp: Dict[str, Any], out_dir: Path, stem: str) -> Dict[str, str]:
        out_dir.mkdir(parents=True, exist_ok=True)
        result = query_resp.get("result") or {}
        saved: Dict[str, str] = {}
        # 保存查询响应
        query_path = out_dir / f"{stem}.query_response.json"
        query_path.write_text(json.dumps(query_resp, ensure_ascii=False, indent=2), encoding="utf-8")
        saved["query_response"] = str(query_path)
        for key, suffix in (("markdown_url", ".md"), ("parse_result_url", ".parse_result.json")):
            url = result.get(key)
            if not url:
                continue
            if key == "markdown_url":
                ext = ".md"
            elif key == "parse_result_url":
                ext = ".parse_result.json"
            else:
                ext = _guess_ext_from_url(url) or suffix
            target = out_dir / f"{stem}{ext if ext.startswith('.') else '.' + ext}"
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            target.write_bytes(r.content)
            saved[key] = str(target)
            print(f"[INFO] 已下载 {key} -> {target}", file=sys.stderr)
        return saved


# ── Markdown 清洗工具 ────────────────────────────────────────────────

def normalize_markdown_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def clean_fupange_markdown(
    markdown_body: str,
    extra_truncate_markers: Optional[List[str]] = None,
) -> Tuple[str, List[str]]:
    """清洗复盘哥类公众号 PDF 转 Markdown 的常见干扰内容。

    Returns:
        (cleaned_text, notes_list)
    """
    text = normalize_markdown_text(markdown_body)
    notes: List[str] = []

    removal_patterns: List[Tuple[str, str]] = [
        (r"^原创.*看懂龙头股.*今天\n?", "移除公众号页眉"),
        (r"^扫一扫上面的二维码图案，加我微信\n?", "移除二维码引导"),
        (r"^一手文章&直播&课程，订阅微信[：:]?\S+\n?", "移除订阅广告"),
        (r"^今年专栏开篇就先把机构票玩法给大家讲清楚.*订阅微信[：:]?\S+\n?", "移除整段课程推广"),
    ]
    for pattern, note in removal_patterns:
        new_text, n = re.subn(pattern, "", text, flags=re.M)
        if n:
            text = new_text
            notes.append(note)

    truncate_markers = [
        "精选留言",
        "喜欢此内容的人还喜欢",
    ]
    if extra_truncate_markers:
        truncate_markers.extend([m for m in extra_truncate_markers if m])

    cut_positions = []
    for marker in truncate_markers:
        idx = text.find(f"\n{marker}\n")
        if idx == -1 and text.startswith(marker + "\n"):
            idx = 0
        if idx != -1:
            cut_positions.append((idx, marker))
    if cut_positions:
        cut_at, marker = min(cut_positions, key=lambda x: x[0])
        text = text[:cut_at].rstrip() + "\n"
        notes.append(f"从“{marker}”起截断留言/附页")

    text = normalize_markdown_text(text)
    return text, notes


def extract_first_heading(markdown_body: str) -> str:
    """从 Markdown 中提取第一个 # 标题内容。"""
    for line in normalize_markdown_text(markdown_body).splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()
        return ""


def compute_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_parser_note(base_note: str, clean_notes: Optional[List[str]] = None) -> str:
    if not clean_notes:
        return base_note
    return base_note + " 已执行清洗：" + "；".join(clean_notes) + "。"


def build_raw_source_markdown(
    file_path: Path,
    markdown_body: str,
    ingested: str,
    sha256_hex: str,
    parser_note: str,
) -> str:
    return (
        f"---\n"
        f"source_url: local-file:///{file_path.as_posix()}\n"
        f"ingested: {ingested}\n"
        f"sha256: {sha256_hex}\n"
        f"parser: baidu-doc-parser\n"
        f"---\n\n"
        f"> {parser_note}\n\n"
        f"{markdown_body.rstrip()}\n"
    )


def write_wiki_ingest_bundle(
    *,
    file_path: Path,
    out_dir: Path,
    saved_files: Dict[str, str],
    ingested: str,
    slug: str,
    clean_markdown: bool,
    truncate_markers: Optional[List[str]] = None,
) -> Dict[str, str]:
    """生成适合 llm-wiki ingest 的 raw/articles/YYYY-MM-DD 目录结构。"""
    bundle_dir = out_dir / "raw" / "articles" / ingested
    bundle_dir.mkdir(parents=True, exist_ok=True)

    bundle_saved: Dict[str, str] = {}
    parser_note = "本文由 baiduocr 文档解析模块转换为 markdown。"
    clean_notes: List[str] = []
    title_hint = ""

    markdown_src = saved_files.get("markdown_url")
    if markdown_src:
        markdown_path = Path(markdown_src)
        markdown_body = markdown_path.read_text(encoding="utf-8", errors="replace")
        markdown_body = normalize_markdown_text(markdown_body)
        if clean_markdown:
            markdown_body, clean_notes = clean_fupange_markdown(markdown_body, extra_truncate_markers=truncate_markers)
        title_hint = extract_first_heading(markdown_body)
        sha256_hex = compute_text_sha256(markdown_body)
        raw_target = bundle_dir / f"{slug}.md"
        raw_target.write_text(
            build_raw_source_markdown(
                file_path,
                markdown_body,
                ingested,
                sha256_hex,
                build_parser_note(parser_note, clean_notes),
            ),
            encoding="utf-8",
        )
        bundle_saved["raw_source"] = str(raw_target)
    else:
        sha256_hex = compute_file_sha256(file_path)

    parse_src = saved_files.get("parse_result_url")
    if parse_src:
        parse_target = bundle_dir / f"{slug}.parse_result.json"
        parse_target.write_bytes(Path(parse_src).read_bytes())
        bundle_saved["parse_result"] = str(parse_target)

    query_src = saved_files.get("query_response")
    if query_src:
        query_target = bundle_dir / f"{slug}.query_response.json"
        query_target.write_bytes(Path(query_src).read_bytes())
        bundle_saved["query_response"] = str(query_target)

    manifest = {
        "source_file": str(file_path),
        "ingested": ingested,
        "slug": slug,
        "sha256": sha256_hex,
        "parser": "baidu-doc-parser",
        "title_hint": title_hint,
        "clean_markdown": clean_markdown,
        "clean_notes": clean_notes,
        "saved_files": bundle_saved,
    }
    manifest_target = bundle_dir / f"{slug}.manifest.json"
    manifest_target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle_saved["manifest"] = str(manifest_target)
    return bundle_saved


# ── 内部工具 ────────────────────────────────────────────────────

def _safe_json(resp: requests.Response) -> Dict[str, Any]:
    resp.encoding = "utf-8"
    try:
        data = resp.json()
    except Exception:
        snippet = resp.text[:1000]
        raise RuntimeError(f"HTTP {resp.status_code} 非 JSON 响应: {snippet}")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {_sanitize_json(data)}")
    return data


def _sanitize_json(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=None)
    text = re.sub(r'(?i)(access_token["\']?\s*[:=]\s*["\']?)[^"\',}\s]+', r'\1****', text)
    text = re.sub(
        r'(?i)((api_key|secret_key|client_secret|client_id)["\']?\s*[:=]\s*["\']?)[^"\',}\s]+',
        r'\1****',
        text,
    )
    return text[:3000]


def _guess_ext_from_url(url: str) -> str:
    p = urlparse(url).path
    name = Path(p).name
    if "." in name:
        return Path(name).suffix
    return ""


def slugify_filename(value: str) -> str:
    text = Path(value).stem.strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9\-_.\u4e00-\u9fff]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text or "document"


# ── CLI ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="百度 OCR 文档解析：提交本地文档并下载解析结果")
    p.add_argument("file", help="待解析文档路径（PDF 等百度文档解析接口支持的格式）")
    p.add_argument("--out-dir", default="doc_parser_output", help="输出目录")
    p.add_argument("--credential", action="append", help="凭证，格式 api_key:secret_key；可重复")
    p.add_argument("--credential-file", help="凭证 JSON 文件路径")
    p.add_argument("--poll-interval", type=int, default=8, help="轮询间隔秒")
    p.add_argument("--max-wait", type=int, default=1800, help="单个任务最大等待秒数")
    p.add_argument("--language-type", default="CHN_ENG")
    p.add_argument("--recognize-formula", action="store_true")
    p.add_argument("--analysis-chart", action="store_true")
    p.add_argument("--angle-adjust", action="store_true")
    p.add_argument("--parse-image-layout", action="store_true")
    p.add_argument("--html-table-format", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--switch-digital-width", default="auto", choices=["auto", "half", "full"])
    p.add_argument("--chunks", action="store_true", help="请求返回 doc chunks")
    p.add_argument("--chunk-size", type=int, default=-1)
    p.add_argument("--write-wiki-bundle", action="store_true", help="额外生成 llm-wiki ingest 目录结构")
    p.add_argument("--wiki-date", help="llm-wiki 目录日期，格式 YYYY-MM-DD；默认今天")
    p.add_argument("--slug", help="输出文件 slug；默认使用文件名规范化")
    p.add_argument("--clean-markdown", action=argparse.BooleanOptionalAction, default=True,
                   help="写 wiki bundle 时按常见规则清洗页眉/广告/留言区")
    p.add_argument("--truncate-marker", action="append", default=[], help="追加正文截断标记")
    p.add_argument("--verbose-error", action="store_true")
    return p


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    file_path = Path(args.file).expanduser()
    if not file_path.exists():
        print(f"[ERROR] 文件不存在: {file_path}", file=sys.stderr)
        return 2

    # 加载凭证 — 复用 baiduocr 的 load_baidu_ocr_credentials
    creds = load_baidu_ocr_credentials(
        raw=os.getenv("BAIDU_OCR_CREDENTIALS") if not (args.credential or args.credential_file) else None,
    )
    # command-line 凭证合并
    if args.credential_file:
        file_data = json.loads(Path(args.credential_file).read_text(encoding="utf-8"))
        if isinstance(file_data, dict):
            file_data = file_data.get("credentials", [])
        for item in file_data:
            if isinstance(item, dict):
                ak = item.get("api_key") or item.get("client_id", "")
                sk = item.get("secret_key") or item.get("client_secret", "")
                if ak and sk:
                    creds.append(BaiduOCRCredential(api_key=str(ak), secret_key=str(sk)))
    if args.credential:
        for c in args.credential:
            if ":" in c:
                ak, sk = c.split(":", 1)
                if ak and sk:
                    creds.append(BaiduOCRCredential(api_key=ak, secret_key=sk))

    options: Dict[str, Any] = {
        "language_type": args.language_type,
        "recognize_formula": args.recognize_formula,
        "analysis_chart": args.analysis_chart,
        "angle_adjust": args.angle_adjust,
        "parse_image_layout": args.parse_image_layout,
        "html_table_format": args.html_table_format,
        "switch_digital_width": args.switch_digital_width,
    }
    if args.chunks:
        options["return_doc_chunks"] = {"switch": True, "split_type": "chunk", "chunk_size": args.chunk_size}

    parser = BaiduDocParser(creds)
    try:
        result = parser.parse_file(file_path, Path(args.out_dir), options, args.poll_interval, args.max_wait)
    except DocParserQuotaExhausted as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        if args.verbose_error:
            traceback.print_exc()
        return 1

    wiki_bundle: Dict[str, str] = {}
    if args.write_wiki_bundle:
        wiki_date = args.wiki_date or dt_date.today().isoformat()
        bundle_slug = args.slug or slugify_filename(file_path.name)
        wiki_bundle = write_wiki_ingest_bundle(
            file_path=file_path,
            out_dir=Path(args.out_dir),
            saved_files=result["saved_files"],
            ingested=wiki_date,
            slug=bundle_slug,
            clean_markdown=args.clean_markdown,
            truncate_markers=args.truncate_marker,
        )
        result["wiki_bundle"] = wiki_bundle

    print(json.dumps({
        "task_id": result["task_id"],
        "credential_index": result["credential_index"],
        "saved_files": result["saved_files"],
        "wiki_bundle": wiki_bundle,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
