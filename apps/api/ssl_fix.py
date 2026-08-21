"""修复 Windows 下请求东财 HTTPS 时的证书校验失败。"""

from __future__ import annotations

import os
import ssl
import warnings

_applied = False
_MODE = "none"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}


def apply_ssl_fix(*, insecure: bool = False) -> str:
    """
    优先挂上 certifi CA；若 insecure=True 则关闭校验（仅建议本机研究用）。
    可重复调用，只会生效一次。
    """
    global _applied, _MODE
    if _applied:
        return _MODE

    try:
        import certifi

        ca = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
        os.environ.setdefault("CURL_CA_BUNDLE", ca)
        _MODE = "certifi"
    except Exception:
        _MODE = "system"

    if insecure:
        _MODE = "insecure"
        ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[attr-defined]
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")

    try:
        import requests

        _orig = requests.Session.request

        def _request(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
            headers = kwargs.pop("headers", None) or {}
            merged = {**_BROWSER_HEADERS, **dict(headers)}
            kwargs["headers"] = merged
            if insecure:
                kwargs.setdefault("verify", False)
            return _orig(self, method, url, **kwargs)

        requests.Session.request = _request  # type: ignore[method-assign]
    except Exception:
        pass

    _applied = True
    return _MODE
