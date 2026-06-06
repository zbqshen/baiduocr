from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from PIL import Image, ImageOps

BAIDU_OCR_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_OCR_URLS = [
    "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic",
    "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic",
    "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate",
    "https://aip.baidubce.com/rest/2.0/ocr/v1/general",
    "https://aip.baidubce.com/rest/2.0/ocr/v1/webimage",
]

BAIDU_OCR_ROTATE_ERROR_CODES = {17, 18, 19, 216630, 216631}
BAIDU_OCR_ROTATE_ERROR_HINTS = (
    "daily request limit",
    "qps",
    "quota",
    "limit reached",
    "no permission",
    "quota exhausted",
    "request limit",
    "调用量",
    "额度",
    "限流",
    "次数",
)

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".ding-cli" / "config.json",
    Path.home() / ".config" / "baiduocr" / "config.json",
]


@dataclass
class BaiduOCRCredential:
    api_key: str
    secret_key: str
    token: str | None = None
    expires: float = 0
    disabled: bool = False
    disabled_endpoints: set[int] | None = None

    def is_endpoint_disabled(self, endpoint_idx: int) -> bool:
        return bool(self.disabled_endpoints and endpoint_idx in self.disabled_endpoints)

    def disable_endpoint(self, endpoint_idx: int) -> None:
        if self.disabled_endpoints is None:
            self.disabled_endpoints = set()
        self.disabled_endpoints.add(endpoint_idx)


@dataclass
class OCRResult:
    text: str
    confidence: float = 0.0
    endpoint: str = ""
    preprocessed: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "endpoint": self.endpoint,
            "preprocessed": self.preprocessed,
            "error": self.error,
        }


class BaiduOCRQuotaExhausted(RuntimeError):
    pass


def _parse_credentials_blob(raw: str | None) -> list[tuple[str, str]]:
    raw = (raw or "").strip()
    pairs: list[tuple[str, str]] = []
    if not raw:
        return pairs
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        for item in re.split(r"[\n,;]+", raw):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                api_key, secret_key = item.split(":", 1)
            elif "|" in item:
                api_key, secret_key = item.split("|", 1)
            else:
                continue
            api_key = api_key.strip()
            secret_key = secret_key.strip()
            if api_key and secret_key:
                pairs.append((api_key, secret_key))
        return pairs

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                api_key = str(item.get("api_key") or item.get("client_id") or "").strip()
                secret_key = str(item.get("secret_key") or item.get("client_secret") or "").strip()
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                api_key = str(item[0]).strip()
                secret_key = str(item[1]).strip()
            else:
                continue
            if api_key and secret_key:
                pairs.append((api_key, secret_key))
    return pairs


def _read_credentials_from_config(config_path: Path | None = None) -> str:
    candidates = [config_path] if config_path else DEFAULT_CONFIG_PATHS
    for path in candidates:
        if not path:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in ("baidu_ocr_credentials", "baiduOcrCredentials"):
            value = str(data.get(key, "")).strip()
            if value:
                return value
    return ""


def load_baidu_ocr_credentials(raw: str | None = None, config_path: Path | None = None) -> list[BaiduOCRCredential]:
    blob = (raw if raw is not None else os.getenv("BAIDU_OCR_CREDENTIALS", "")).strip()
    pairs: list[tuple[str, str]] = []
    if blob:
        pairs.extend(_parse_credentials_blob(blob))
    else:
        config_blob = _read_credentials_from_config(config_path=config_path)
        if config_blob:
            pairs.extend(_parse_credentials_blob(config_blob))
        else:
            api_key = os.getenv("BAIDU_OCR_API_KEY", "").strip()
            secret_key = os.getenv("BAIDU_OCR_SECRET_KEY", "").strip()
            if api_key and secret_key:
                pairs.append((api_key, secret_key))

    seen = set()
    credentials: list[BaiduOCRCredential] = []
    for api_key, secret_key in pairs:
        ident = (api_key, secret_key)
        if ident in seen:
            continue
        seen.add(ident)
        credentials.append(BaiduOCRCredential(api_key=api_key, secret_key=secret_key))
    return credentials


class BaiduOCR:
    def __init__(self, credentials_raw: str | None = None, config_path: Path | None = None, request_module=None):
        self.credentials = load_baidu_ocr_credentials(credentials_raw, config_path=config_path)
        self._current = 0
        self._endpoint_current = 0
        self._requests = request_module

    @property
    def requests(self):
        if self._requests is None:
            import requests
            self._requests = requests
        return self._requests

    def _credential(self, endpoint_idx: int | None = None) -> BaiduOCRCredential:
        endpoint_idx = self._endpoint_current if endpoint_idx is None else endpoint_idx
        if not self.credentials:
            raise BaiduOCRQuotaExhausted("未配置百度 OCR 凭据")
        for offset in range(len(self.credentials)):
            idx = (self._current + offset) % len(self.credentials)
            cred = self.credentials[idx]
            if not cred.disabled and not cred.is_endpoint_disabled(endpoint_idx):
                self._current = idx
                return cred
        raise BaiduOCRQuotaExhausted("当前百度 OCR 接口下所有凭据均已用完或被限流")

    def _get_token(self) -> str:
        now = time.time()
        attempts = 0
        while attempts < len(self.credentials):
            cred = self._credential()
            if cred.token and now < cred.expires - 60:
                return cred.token
            try:
                response = self.requests.post(
                    BAIDU_OCR_TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": cred.api_key,
                        "client_secret": cred.secret_key,
                    },
                    timeout=15,
                )
                response.raise_for_status()
                data = response.json()
            except Exception:
                cred.disabled = True
                self._current = (self._current + 1) % len(self.credentials)
                attempts += 1
                continue
            if data.get("error") or data.get("error_code"):
                cred.disabled = True
                self._current = (self._current + 1) % len(self.credentials)
                attempts += 1
                continue
            cred.token = data["access_token"]
            cred.expires = now + int(data.get("expires_in", 2592000))
            return cred.token
        raise BaiduOCRQuotaExhausted("所有百度 OCR 凭据均无法获取 token 或已被限流")

    @staticmethod
    def _should_rotate(result: dict) -> bool:
        code = result.get("error_code")
        msg = str(result.get("error_msg") or result.get("error_description") or "").lower()
        return code in BAIDU_OCR_ROTATE_ERROR_CODES or any(hint in msg for hint in BAIDU_OCR_ROTATE_ERROR_HINTS)

    def _post_ocr(self, payload: str) -> tuple[dict, str]:
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        for endpoint_idx in range(self._endpoint_current, len(BAIDU_OCR_URLS)):
            self._endpoint_current = endpoint_idx
            endpoint = BAIDU_OCR_URLS[endpoint_idx]
            attempted = 0
            while attempted < len(self.credentials):
                try:
                    token = self._get_token()
                except BaiduOCRQuotaExhausted:
                    break
                response = self.requests.post(
                    f"{endpoint}?access_token={token}",
                    data=payload,
                    headers=headers,
                    timeout=30,
                )
                response.raise_for_status()
                result = response.json()
                if result.get("error_code") and self._should_rotate(result):
                    self.credentials[self._current].disable_endpoint(endpoint_idx)
                    self._current = (self._current + 1) % len(self.credentials)
                    attempted += 1
                    continue
                return result, endpoint
        raise BaiduOCRQuotaExhausted("所有百度 OCR 接口与凭据均已用完或被限流")

    @staticmethod
    def _parse_result(result: dict) -> tuple[str, float]:
        if result.get("error_code"):
            return "", 0.0
        lines: list[str] = []
        confidences: list[float] = []
        for item in result.get("words_result", []):
            text = item.get("words", "").strip()
            if text:
                lines.append(text)
            avg = item.get("probability", {}).get("average")
            if avg is not None:
                confidences.append(float(avg))
        confidence = sum(confidences) / len(confidences) if confidences else 0.0
        return "\n".join(lines), confidence

    def _encode_image(self, image_path: str | Path, preprocess: bool) -> str:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            if preprocess:
                img = img.convert("L")
                img = ImageOps.autocontrast(img)
                if min(img.size) < 1200:
                    img = img.resize((img.width * 2, img.height * 2))
            elif img.mode == "RGBA":
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="PNG")
            img_bytes = buf.getvalue()
        encoded = quote(base64.b64encode(img_bytes).decode("utf-8"))
        return f"image={encoded}&language_type=CHN_ENG&detect_direction=true&paragraph=true&probability=true"

    def _recognize_once(self, image_path: str | Path, preprocess: bool) -> OCRResult:
        payload = self._encode_image(image_path, preprocess=preprocess)
        result, endpoint = self._post_ocr(payload)
        text, confidence = self._parse_result(result)
        return OCRResult(
            text=text,
            confidence=confidence,
            endpoint=endpoint,
            preprocessed=preprocess,
            error=str(result.get("error_msg") or "") if result.get("error_code") else "",
        )

    def recognize(self, image_path: str | Path, fallback_without_preprocess: bool = True) -> OCRResult:
        first = self._recognize_once(image_path, preprocess=True)
        if first.text or not fallback_without_preprocess:
            return first
        try:
            second = self._recognize_once(image_path, preprocess=False)
        except BaiduOCRQuotaExhausted:
            raise
        except Exception as exc:
            return OCRResult(text="", error=str(exc), preprocessed=False)
        return second if second.text else first

    def recognize_many(self, paths: Iterable[str | Path], delay: float = 0.0) -> list[dict]:
        results = []
        for index, path in enumerate(paths):
            if index and delay > 0:
                time.sleep(delay)
            try:
                result = self.recognize(path)
                row = {"path": str(path), **result.to_dict()}
            except Exception as exc:
                row = {
                    "path": str(path),
                    "text": "",
                    "confidence": 0.0,
                    "endpoint": "",
                    "preprocessed": True,
                    "error": str(exc),
                }
            results.append(row)
        return results
