import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baiduocr import doc_parser
from baiduocr.doc_parser import (
    BaiduDocParser,
    DocParserError,
    DocParserQuotaExhausted,
    clean_fupange_markdown,
    extract_first_heading,
    normalize_markdown_text,
    slugify_filename,
    write_wiki_ingest_bundle,
    build_parser_note,
    build_raw_source_markdown,
    compute_text_sha256,
    compute_file_sha256,
)
from baiduocr.client import BaiduOCRCredential


# ── 工具函数测试 ──────────────────────────────────────────────

class TestNormalizeMarkdown:
    def test_removes_carriage_returns(self):
        assert normalize_markdown_text("a\r\nb\r\nc") == "a\nb\nc\n"

    def test_collapses_excessive_newlines(self):
        assert normalize_markdown_text("a\n\n\n\n\nb") == "a\n\nb\n"


class TestCleanFupangeMarkdown:
    def test_removes_header(self):
        text = "原创 看懂龙头股 今天\n正文内容"
        cleaned, notes = clean_fupange_markdown(text)
        assert "原创" not in cleaned
        assert "正文内容" in cleaned
        assert "公众号页眉" in " ".join(notes)

    def test_truncates_at_liked_content(self):
        text = "# 标题\n正文\n喜欢此内容的人还喜欢\n附页内容"
        cleaned, notes = clean_fupange_markdown(text)
        assert "喜欢此内容的人还喜欢" not in cleaned
        assert "附页内容" not in cleaned
        assert "正文" in cleaned
        assert any("截断" in n for n in notes)

    def test_extra_truncate_markers(self):
        text = "# 标题\n正文\n广告分隔线\n更多内容"
        cleaned, notes = clean_fupange_markdown(text, extra_truncate_markers=["广告分隔线"])
        assert "广告分隔线" not in cleaned
        assert "更多内容" not in cleaned

    def test_empty_text(self):
        cleaned, notes = clean_fupange_markdown("")
        assert cleaned == "\n"
        assert notes == []


class TestExtractFirstHeading:
    def test_finds_h1(self):
        assert extract_first_heading("# 我的标题\n正文") == "我的标题"

    def test_finds_h2(self):
        assert extract_first_heading("## 副标题\n正文") == "副标题"

    def test_no_heading(self):
        assert extract_first_heading("正文内容") == ""


class TestSlugifyFilename:
    def test_basic(self):
        assert slugify_filename("复盘哥-2024-01-15.pdf") == "复盘哥-2024-01-15"

    def test_special_chars(self):
        assert slugify_filename("foo bar (1).pdf") == "foo-bar-1"

    def test_empty_stem(self):
        assert slugify_filename(".pdf") or True  # should not crash


class TestComputeHash:
    def test_text_sha256(self):
        h = compute_text_sha256("hello")
        assert len(h) == 64
        assert isinstance(h, str)

    def test_file_sha256(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello")
        h = compute_file_sha256(f)
        assert len(h) == 64


class TestBuildParserNote:
    def test_no_cleaning(self):
        assert build_parser_note("base") == "base"

    def test_with_cleaning(self):
        note = build_parser_note("base", ["移除页眉"])
        assert "base" in note
        assert "移除页眉" in note


class TestBuildRawSourceMarkdown:
    def test_contains_metadata(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_text("fake pdf")
        result = build_raw_source_markdown(f, "# Header\nBody", "2024-01-15", "abc123", "note")
        assert "source_url: local-file:///" in result
        assert "ingested: 2024-01-15" in result
        assert "sha256: abc123" in result
        assert "parser: baidu-doc-parser" in result
        assert "# Header" in result
        assert "> note" in result


# ── 清理测试 ──────────────────────────────────────────────

class TestCleanFupangeEdgeCases:
    def test_wechat_subscription_ad(self):
        text = "一手文章&直播&课程，订阅微信：abc123\n正文"
        cleaned, notes = clean_fupange_markdown(text)
        assert "订阅微信" not in cleaned
        assert "正文" in cleaned

    def test_qr_code_guide(self):
        text = "扫一扫上面的二维码图案，加我微信\n正文"
        cleaned, notes = clean_fupange_markdown(text)
        assert "二维码" not in cleaned
        assert "正文" in cleaned


# ── BaiduDocParser mock 测试 ────────────────────────────────────

class FakeResponse:
    def __init__(self, data, status_code=200):
        self.payload = data
        self.status_code = status_code
        self.content = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self.payload, dict):
            return self.payload
        raise RuntimeError("not a json response")


def test_doc_parser_parse_file_success(tmp_path):
    """模拟完整成功流程：token→submit→poll→download。"""
    query_count = [0]  # mutable counter

    class FakeSession:
        def post(self, url, data=None, headers=None, params=None, timeout=None):
            if "oauth/2.0/token" in url:
                return FakeResponse({"access_token": "test-token", "expires_in": 3600})
            if "parser/task/query" in url:
                query_count[0] += 1
                if query_count[0] < 2:
                    return FakeResponse({"result": {"status": "running"}})
                return FakeResponse({
                    "result": {
                        "status": "success",
                        "markdown_url": "https://example.com/output.md",
                    }
                })
            if "parser/task" in url:
                # submit
                return FakeResponse({"result": {"task_id": "task-123"}})
            return FakeResponse({"error_code": -1})

        def get(self, url, timeout=None):
            return FakeResponse(b"# Test Output\n\nHello World")

    creds = [BaiduOCRCredential(api_key="ak1", secret_key="sk1")]
    parser = BaiduDocParser(creds, timeout=5)
    parser.session = FakeSession()

    src_file = tmp_path / "test.pdf"
    src_file.write_text("fake pdf content")

    result = parser.parse_file(src_file, tmp_path, {}, poll_interval=0.1, max_wait=5)

    assert result["task_id"] == "task-123"
    assert result["credential_index"] == 0
    assert "markdown_url" in result["saved_files"]
    assert "query_response" in result["saved_files"]

    # Verify downloaded markdown
    md_path = result["saved_files"]["markdown_url"]
    assert Path(md_path).read_text() == "# Test Output\n\nHello World"


def test_doc_parser_rotates_on_quota_error(tmp_path):
    """quota 错误时自动切换凭证。"""
    creds = [
        BaiduOCRCredential(api_key="ak1", secret_key="sk1"),
        BaiduOCRCredential(api_key="ak2", secret_key="sk2"),
    ]

    token_count = [0]  # token requests per credential
    submit_count = [0]

    class FakeSession:
        def post(self, url, data=None, headers=None, params=None, timeout=None):
            if "oauth/2.0/token" in url:
                client_id = (data or {}).get("client_id", "")
                token_count[0] += 1
                if client_id == "ak1":
                    return FakeResponse({"access_token": "token-ak1", "expires_in": 3600})
                return FakeResponse({"access_token": "token-ak2", "expires_in": 3600})
            if "parser/task/query" in url:
                return FakeResponse({"result": {"status": "success", "markdown_url": "https://example.com/md"}})
            if "parser/task" in url:
                # submit
                submit_count[0] += 1
                # First submit attempt (credential #1) returns quota error
                if submit_count[0] == 1:
                    return FakeResponse({"error_code": 17, "error_msg": "Open api daily request limit reached"})
                return FakeResponse({"result": {"task_id": "task-456"}})
            return FakeResponse({"error_code": -1})

        def get(self, url, timeout=None):
            return FakeResponse(b"# Result")

    parser = BaiduDocParser(creds, timeout=5)
    parser.session = FakeSession()

    src_file = tmp_path / "test.pdf"
    src_file.write_text("fake pdf")

    result = parser.parse_file(src_file, tmp_path, {}, poll_interval=0.1, max_wait=5)
    assert result["credential_index"] == 1  # second credential succeeded
    assert result["task_id"] == "task-456"
    assert submit_count[0] == 2  # two submit attempts (one failed, one succeeded)


def test_doc_parser_all_credentials_exhausted(tmp_path):
    """所有凭证均失败时抛出 DocParserQuotaExhausted。"""

    class FailingSession:
        def post(self, url, data=None, headers=None, params=None, timeout=None):
            if "oauth/2.0/token" in url:
                return FakeResponse({"access_token": "token", "expires_in": 3600})
            return FakeResponse({"error_code": 17, "error_msg": "quota exhausted"})

    creds = [
        BaiduOCRCredential(api_key="ak1", secret_key="sk1"),
        BaiduOCRCredential(api_key="ak2", secret_key="sk2"),
    ]
    parser = BaiduDocParser(creds, timeout=5)
    parser.session = FailingSession()

    src_file = tmp_path / "test.pdf"
    src_file.write_text("fake pdf")

    with pytest.raises(DocParserQuotaExhausted):
        parser.parse_file(src_file, tmp_path, {}, poll_interval=0.1, max_wait=5)


def test_extract_first_heading_empty():
    assert extract_first_heading("") == ""


def test_slugify_unicode():
    slug = slugify_filename("A股复盘-2025年.pdf")
    assert "A" in slug or "a" in slug
    assert slug[-1] != "."


def test_write_wiki_ingest_bundle_no_markdown(tmp_path):
    """没有 markdown 产物时只生成 manifest。"""
    saved = {"query_response": str(tmp_path / "dummy.json")}
    (tmp_path / "dummy.json").write_text("{}")
    src_file = tmp_path / "source.pdf"
    src_file.write_text("fake pdf")  # need source file for sha256

    result = write_wiki_ingest_bundle(
        file_path=src_file,
        out_dir=tmp_path,
        saved_files=saved,
        ingested="2024-01-15",
        slug="test-doc",
        clean_markdown=True,
    )
    assert "manifest" in result
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert manifest["slug"] == "test-doc"
    assert manifest["ingested"] == "2024-01-15"
