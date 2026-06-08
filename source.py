"""推文抓取层：调用第三方 X(Twitter) 数据 API，归一化成 Tweet。

默认实现对接的接口地址见下方 URL 常量；媒体字段各家结构不一，这里对几种常见结构都做了容错解析。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from config import TwitterCfg
from util import ApiError, http_json

# 所用第三方 API 免费档限速 1 请求/5 秒。全局节流：保证两次调用间隔 ≥ 该值，避免 429。
# 付费档可调小（如 0.2）。轮询间隔通常远大于此，只有翻页/多博主时才真正生效。
_MIN_REQUEST_GAP = 5.2
_last_request_at = [0.0]


def _throttle() -> None:
    wait = _MIN_REQUEST_GAP - (time.time() - _last_request_at[0])
    if wait > 0:
        time.sleep(wait)
    _last_request_at[0] = time.time()

_LAST_TWEETS_URL = "https://api.twitterapi.io/twitter/user/last_tweets"
_USER_INFO_URL = "https://api.twitterapi.io/twitter/user/info"
_ADV_SEARCH_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"


@dataclass
class Tweet:
    id: str
    username: str          # 作者 @handle
    author_name: str       # 作者昵称
    text: str
    url: str
    created_at: datetime | None
    photos: list[str] = field(default_factory=list)  # 图片直链
    videos: list[str] = field(default_factory=list)  # 视频 mp4 直链（最佳变体）
    has_video: bool = False
    media_links: list[str] = field(default_factory=list)  # 文中的媒体 t.co 短链（发送成功才剥离）
    media_status: str = "none"   # none/ok/failed：渲染页脚时用（failed 则补回媒体链接）
    is_reply: bool = False
    is_retweet: bool = False


def fetch_user_status(cfg: TwitterCfg, username: str) -> int | None:
    """便宜探针：调 user/info（约 1 条计费）取 statusesCount（发推总数）。

    用作"有没有新动态"的变化信号——只有它变大才值得去调昂贵的 last_tweets。失败抛 ApiError。
    """
    _throttle()
    data = http_json(
        _USER_INFO_URL,
        headers={"X-API-Key": cfg.api_key},
        params={"userName": username},
        timeout=20,
    )
    status = data.get("status")
    if status and status != "success":
        raise ApiError(f"推文 API user/info: {data.get('message') or status}")
    user = data.get("data") if isinstance(data.get("data"), dict) else data
    count = user.get("statusesCount")
    try:
        return int(count) if count is not None else None
    except (TypeError, ValueError):
        return None


def fetch_window(cfg: TwitterCfg, username: str, since_ts: int, until_ts: int,
                 max_pages: int = 3) -> list[Tweet]:
    """用 advanced_search 查 [since_ts, until_ts) 时间窗内该博主的推文（最新在前）。

    这是监控主用接口：时间窗增量查询，空闲窗口返回 0 条、最省。
    回复/转推按配置在查询端就过滤掉（`-filter:replies` / `include:nativeretweets`），不为它们付费。
    失败抛 ApiError。
    """
    query = f"from:{username} since_time:{int(since_ts)} until_time:{int(until_ts)}"
    if not cfg.include_replies:
        query += " -filter:replies"
    if cfg.include_retweets:
        query += " include:nativeretweets"

    out: list[Tweet] = []
    cursor = ""
    for _ in range(max_pages):
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        _throttle()
        data = http_json(_ADV_SEARCH_URL, headers={"X-API-Key": cfg.api_key}, params=params, timeout=30)
        _raise_if_error(data)
        out.extend(_parse_and_filter(_locate_tweets(data), cfg, username))
        if not data.get("has_next_page") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
    return out


def _raise_if_error(data: dict) -> None:
    if data.get("status") == "error" or data.get("error"):
        raise ApiError(f"推文 API advanced_search: {data.get('message') or data.get('error')}")


def fetch_latest(cfg: TwitterCfg, username: str, cache_path: Path | None = None) -> list[Tweet]:
    """拉取该博主最近的推文（约 20 条计费，最新在前）。若给 cache_path 则把原始响应存盘。失败抛 ApiError。"""
    _throttle()
    data = http_json(
        _LAST_TWEETS_URL,
        headers={"X-API-Key": cfg.api_key},
        params={
            "userName": username,
            "includeReplies": "true" if cfg.include_replies else "false",
        },
        timeout=30,
    )
    status = data.get("status")
    if status and status != "success":
        raise ApiError(f"推文 API: {data.get('message') or status}")
    if cache_path is not None:
        _write_cache(cache_path, data)
    return _parse_and_filter(_locate_tweets(data), cfg, username)


def load_cached(cfg: TwitterCfg, username: str, cache_path: Path) -> list[Tweet] | None:
    """从缓存文件还原推文（不调用远端 API）。无缓存/损坏返回 None。"""
    path = Path(cache_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _parse_and_filter(_locate_tweets(data), cfg, username)


def _parse_and_filter(raw: list, cfg: TwitterCfg, username: str) -> list[Tweet]:
    tweets: list[Tweet] = []
    for item in raw:
        t = _parse_tweet(item, username)
        if t is None:
            continue
        if t.is_retweet and not cfg.include_retweets:
            continue
        if t.is_reply and not cfg.include_replies:
            continue
        tweets.append(t)
    return tweets


def _write_cache(cache_path: Path, data: dict) -> None:
    try:
        Path(cache_path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # 缓存只是优化，写失败不致命


def _locate_tweets(data: dict) -> list:
    """不同版本可能是 data.tweets 或 data.data.tweets，都兼容。"""
    if isinstance(data.get("tweets"), list):
        return data["tweets"]
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("tweets"), list):
        return inner["tweets"]
    if isinstance(inner, list):
        return inner
    return []


def _parse_tweet(item: dict, fallback_user: str) -> Tweet | None:
    if not isinstance(item, dict) or item.get("id") is None:
        return None
    tid = str(item["id"])
    text = item.get("text") or item.get("full_text") or ""

    author = item.get("author") or {}
    handle = author.get("userName") or author.get("screen_name") or fallback_user
    name = author.get("name") or handle
    url = item.get("url") or f"https://x.com/{handle}/status/{tid}"

    is_reply = bool(item.get("isReply") or item.get("inReplyToId") or item.get("in_reply_to_user_id"))
    is_retweet = bool(item.get("retweeted_tweet") or item.get("retweeted_status")) or text.startswith("RT @")

    photos, has_video, media_links, videos = _extract_media(item)

    return Tweet(
        id=tid,
        username=handle,
        author_name=name,
        text=text,
        url=url,
        created_at=_parse_time(item.get("createdAt") or item.get("created_at")),
        photos=photos,
        videos=videos,
        has_video=has_video,
        media_links=media_links,
        is_reply=is_reply,
        is_retweet=is_retweet,
    )


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    # Twitter 风格："Tue Dec 10 07:00:30 +0000 2024"
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_media(item: dict) -> tuple[list[str], bool, list[str], list[str]]:
    """抽取媒体：图片直链、是否含视频、文中媒体 t.co 短链、视频 mp4 直链。"""
    nodes = _first_media_list(item)
    photos: list[str] = []
    links: list[str] = []
    videos: list[str] = []
    has_video = False
    for m in nodes:
        if not isinstance(m, dict):
            continue
        tco = m.get("url")  # 出现在推文正文里的 t.co 短链
        if isinstance(tco, str) and tco.startswith("http"):
            links.append(tco)
        mtype = (m.get("type") or "photo").lower()
        if mtype in ("video", "animated_gif"):
            has_video = True
            v = _best_video(m)
            if v:
                videos.append(v)
            continue
        img = m.get("media_url_https") or m.get("media_url")
        if isinstance(img, str) and img.startswith("http"):
            photos.append(img)
    return _uniq(photos)[:10], has_video, _uniq(links), _uniq(videos)[:10]


def _best_video(m: dict) -> str | None:
    """从 media 项里取最佳 mp4 直链（最高码率）。"""
    info = m.get("video_info") or m.get("videoInfo") or {}
    variants = info.get("variants") or []
    mp4 = [v for v in variants
           if isinstance(v, dict) and v.get("content_type") == "video/mp4" and v.get("url")]
    if not mp4:
        return None
    mp4.sort(key=lambda v: v.get("bitrate", 0) or 0, reverse=True)
    return mp4[0]["url"]


def _uniq(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    return [x for x in seq if not (x in seen or seen.add(x))]


def _first_media_list(item: dict) -> list:
    for path in (
        ("extendedEntities", "media"),
        ("extended_entities", "media"),
        ("entities", "media"),
        ("media",),
    ):
        node = item
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, list) and node:
            return node
    return []
