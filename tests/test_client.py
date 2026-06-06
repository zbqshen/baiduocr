import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from baiduocr import client


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeRequests:
    def __init__(self, fake_post):
        self.post = fake_post


def test_load_credentials_from_env(monkeypatch):
    monkeypatch.setenv(
        "BAIDU_OCR_CREDENTIALS",
        "api1:secret1, api2:secret2\napi3:secret3",
    )
    monkeypatch.delenv("BAIDU_OCR_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_OCR_SECRET_KEY", raising=False)

    credentials = client.load_baidu_ocr_credentials()

    assert [(c.api_key, c.secret_key) for c in credentials] == [
        ("api1", "secret1"),
        ("api2", "secret2"),
        ("api3", "secret3"),
    ]


def test_baidu_ocr_rotates_when_quota_is_exhausted(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        if url == client.BAIDU_OCR_TOKEN_URL:
            calls.append(("token", data["client_id"]))
            return DummyResponse({"access_token": f"token-{data['client_id']}", "expires_in": 3600})
        token = url.rsplit("access_token=", 1)[1]
        endpoint = url.split("?", 1)[0].rsplit("/", 1)[1]
        calls.append(("ocr", endpoint, token))
        if token == "token-api1":
            return DummyResponse({"error_code": 17, "error_msg": "Open api daily request limit reached"})
        return DummyResponse({"words_result": [{"words": "轮换成功"}]})

    monkeypatch.setenv("BAIDU_OCR_CREDENTIALS", "api1:secret1,api2:secret2")
    ocr = client.BaiduOCR(request_module=FakeRequests(fake_post))

    result, _endpoint = ocr._post_ocr("dummy-image-payload")
    text, _confidence = ocr._parse_result(result)

    assert text == "轮换成功"
    assert calls == [
        ("token", "api1"),
        ("ocr", "general_basic", "token-api1"),
        ("token", "api2"),
        ("ocr", "general_basic", "token-api2"),
    ]
    assert ocr.credentials[0].is_endpoint_disabled(0) is True
    assert ocr.credentials[1].is_endpoint_disabled(0) is False


def test_baidu_ocr_switches_endpoint_after_all_credentials_exhausted(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        if url == client.BAIDU_OCR_TOKEN_URL:
            calls.append(("token", data["client_id"]))
            return DummyResponse({"access_token": f"token-{data['client_id']}", "expires_in": 3600})
        token = url.rsplit("access_token=", 1)[1]
        endpoint = url.split("?", 1)[0].rsplit("/", 1)[1]
        calls.append(("ocr", endpoint, token))
        if endpoint == "general_basic":
            return DummyResponse({"error_code": 17, "error_msg": "Open api daily request limit reached"})
        return DummyResponse({"words_result": [{"words": "接口切换成功"}]})

    monkeypatch.setenv("BAIDU_OCR_CREDENTIALS", "api1:secret1,api2:secret2")
    ocr = client.BaiduOCR(request_module=FakeRequests(fake_post))

    result, _endpoint = ocr._post_ocr("dummy-image-payload")
    text, _confidence = ocr._parse_result(result)

    assert text == "接口切换成功"
    assert calls == [
        ("token", "api1"),
        ("ocr", "general_basic", "token-api1"),
        ("token", "api2"),
        ("ocr", "general_basic", "token-api2"),
        ("ocr", "accurate_basic", "token-api1"),
    ]
    assert ocr._endpoint_current == 1
    assert all(c.disabled_endpoints == {0} for c in ocr.credentials)


def test_baidu_ocr_rotates_when_token_request_fails(monkeypatch):
    calls = []

    def fake_post(url, data=None, headers=None, timeout=None):
        if url == client.BAIDU_OCR_TOKEN_URL:
            calls.append(("token", data["client_id"]))
            if data["client_id"] == "api1":
                return DummyResponse({"error": "invalid_client", "error_description": "unknown client id"}, status_code=401)
            return DummyResponse({"access_token": f"token-{data['client_id']}", "expires_in": 3600})
        token = url.rsplit("access_token=", 1)[1]
        calls.append(("ocr", token))
        return DummyResponse({"words_result": [{"words": "token失败后轮换成功"}]})

    monkeypatch.setenv("BAIDU_OCR_CREDENTIALS", "api1:secret1,api2:secret2")
    ocr = client.BaiduOCR(request_module=FakeRequests(fake_post))

    result, _endpoint = ocr._post_ocr("dummy-image-payload")
    text, _confidence = ocr._parse_result(result)

    assert text == "token失败后轮换成功"
    assert calls == [
        ("token", "api1"),
        ("token", "api2"),
        ("ocr", "token-api2"),
    ]
    assert ocr.credentials[0].disabled is True
    assert ocr.credentials[1].disabled is False


def test_baidu_ocr_raises_when_all_endpoints_and_credentials_exhausted(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        if url == client.BAIDU_OCR_TOKEN_URL:
            return DummyResponse({"access_token": f"token-{data['client_id']}", "expires_in": 3600})
        return DummyResponse({"error_code": 18, "error_msg": "Open api qps request limit reached"})

    monkeypatch.setenv("BAIDU_OCR_CREDENTIALS", "api1:secret1,api2:secret2")
    ocr = client.BaiduOCR(request_module=FakeRequests(fake_post))

    with pytest.raises(client.BaiduOCRQuotaExhausted):
        ocr._post_ocr("dummy-image-payload")

    assert all(
        c.disabled_endpoints == set(range(len(client.BAIDU_OCR_URLS)))
        for c in ocr.credentials
    )
