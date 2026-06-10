"""极简 HTTP/JSON 工具，仅用标准库，避免任何第三方依赖。"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "twitter2telegram/1.0 (+local)"

# Telegram 接口路径形如 /bot<token>/method，报错信息会带 URL——把 token 段打码，避免机密进日志
_BOT_TOKEN_RE = re.compile(r"/bot[^/]+")


class ApiError(RuntimeError):
    """任何上游接口返回的错误，统一抛这个，便于上层按轮询粒度兜底。"""


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    params: dict | None = None,
    body: dict | None = None,
    timeout: int = 30,
) -> dict:
    """发一个请求并把响应当 JSON 解析。非 2xx 抛 ApiError（含响应片段，便于排查）。"""
    if params:
        query = urllib.parse.urlencode(params)
        url += ("&" if "?" in url else "?") + query

    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ApiError(f"HTTP {exc.code} {method} {_host(url)}: {detail}") from None
    except urllib.error.URLError as exc:
        raise ApiError(f"网络错误 {method} {_host(url)}: {exc.reason}") from None

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(f"非 JSON 响应 {_host(url)}: {raw[:200]}") from None


def _host(url: str) -> str:
    """报错用的 URL 摘要（scheme://host/path，去掉查询串）；bot token 段打码。"""
    try:
        p = urllib.parse.urlparse(url)
        url = f"{p.scheme}://{p.netloc}{p.path}"
    except Exception:
        pass
    return _BOT_TOKEN_RE.sub("/bot***", url)
