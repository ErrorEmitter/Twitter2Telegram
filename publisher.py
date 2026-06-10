"""发布层：Telegram Bot API（异步占位填充）。

顺序固定：翻译 → 原文 → 解读。流程：
  ① post_placeholders() 先发原文，译文位「翻译中…」、解读位「📖 解读中…」占位；有图则图片置底。
  ② fill_translation() 译文就绪 → 替换译文占位。
  ③ fill_explanation() 解读就绪 → 替换解读占位（无术语可解释则删掉解读那条/段）。
布局：原文短 → 1 条消息(译文+原文+解读)；原文长 → 2 条(① 译文+原文+页脚 ② 解读)。图片始终最后一条。
作者署名做成 X 主页链接（@handle 不被当 TG 用户）；时间注明 PDT/PST。
"""
from __future__ import annotations

import html
from zoneinfo import ZoneInfo

from config import TelegramCfg
from source import Tweet
from util import ApiError, http_json

_API = "https://api.telegram.org/bot{token}/{method}"

_ONE_MSG_ORIG_MAX = 700    # 原文 ≤ 此字符数才走「1 条消息」（解读较长，短推才合一条）
_TRANS_1, _ORIG_1, _EXPL_1 = 1000, 1000, 1800   # 1 条布局各段截断（合计稳在 4096 内）
_TRANS_2, _ORIG_2 = 1900, 1900                  # 2 条布局 msg1（译文+原文）截断
_EXPL_2 = 3800                                   # 2 条布局 msg2（解读，可较详尽）截断


# ---------- 对外接口 ----------

def post_placeholders(cfg: TelegramCfg, tz: ZoneInfo, tweet: Tweet) -> tuple[bool, int, int | None, str]:
    """发原文 + 占位 + 媒体（图片/视频）置底。返回 (two_part, msg1_id, msg2_id|None, media_status)。

    media_status: none(无媒体) / ok(发出) / failed(发送失败，已把媒体链接补回页脚)。第一条文字失败则抛。
    """
    two_part = len(tweet.text) > _ONE_MSG_ORIG_MAX
    if two_part:
        msg1 = _send_text(cfg, _render_msg1_2p(tz, tweet, None, "pending"))
        msg2_id = _send_text(cfg, _render_msg2_2p(None, "pending"))
    else:
        msg1 = _send_text(cfg, _render_single(tz, tweet, None, "pending", None, "pending"))
        msg2_id = None

    media_status = "none"
    photos = tweet.photos if cfg.forward_media else []
    videos = tweet.videos if cfg.forward_media else []
    if photos or videos:
        try:
            _send_media(cfg, photos, videos)   # 置底
            media_status = "ok"
        except ApiError:
            media_status = "failed"            # 没发出去 → 标到 tweet 上、重渲 msg1 页脚补回媒体链接
            tweet.media_status = "failed"
            try:
                if two_part:
                    _edit_text(cfg, msg1, _render_msg1_2p(tz, tweet, None, "pending"))
                else:
                    _edit_text(cfg, msg1, _render_single(tz, tweet, None, "pending", None, "pending"))
            except ApiError:
                pass
    return two_part, msg1, msg2_id, media_status


def fill_translation(cfg: TelegramCfg, tz: ZoneInfo, *, two_part: bool, msg1_id: int, tweet: Tweet,
                     translation: str | None, t_status: str, explanation, e_status: str) -> None:
    """把译文占位替换成译文（或失败提示）。失败抛 ApiError。"""
    if two_part:
        _edit_text(cfg, msg1_id, _render_msg1_2p(tz, tweet, translation, t_status))
    else:
        _edit_text(cfg, msg1_id, _render_single(tz, tweet, translation, t_status, explanation, e_status))


def fill_explanation(cfg: TelegramCfg, tz: ZoneInfo, *, two_part: bool, msg1_id: int, msg2_id: int | None,
                     tweet: Tweet, translation, t_status: str, explanation: str | None, e_status: str) -> None:
    """把解读占位替换成术语解释；无可解释/失败则去掉解读（2 条布局删第二条）。失败抛 ApiError。"""
    if two_part:
        body = _render_msg2_2p(explanation, e_status)
        if body is None:
            if msg2_id is not None:
                _call(cfg, "deleteMessage", {"chat_id": cfg.chat_id, "message_id": msg2_id})
        else:
            _edit_text(cfg, msg2_id, body)
    else:
        _edit_text(cfg, msg1_id, _render_single(tz, tweet, translation, t_status, explanation, e_status))


def send_plain(cfg: TelegramCfg, text: str, chat_id: str | None = None) -> int:
    """发纯文本。默认发 cfg.chat_id（故障提示走这里＝群）；--test 等传入 debug 的 cfg 即发调试会话。"""
    return _send_text(cfg, html.escape(text), chat_id=chat_id or cfg.chat_id)


def get_me(cfg: TelegramCfg) -> dict:
    return _call(cfg, "getMe", {})


def list_chats(cfg: TelegramCfg) -> list[dict]:
    result = _call(cfg, "getUpdates", {"timeout": 0, "allowed_updates": ["message", "channel_post", "my_chat_member"]})
    chats, seen = [], set()
    for upd in result:
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (upd.get(key) or {}).get("chat")
            if chat and chat.get("id") not in seen:
                seen.add(chat["id"])
                chats.append(chat)
    return chats


# ---------- 渲染 ----------

def _render_single(tz, tweet, translation, t_status, explanation, e_status) -> str:
    """1 条布局：作者 →(译文)→ 原文 → 解读（各为可展开折叠块）→ 页脚。原文是中文则省略译文段。"""
    parts = [_header(tweet)]
    if not tweet.skip_translation:
        parts.append(_trans_section(translation, t_status, _TRANS_1))
    parts.append(_orig_section(tweet, _ORIG_1))
    es = _expl_section(explanation, e_status, _EXPL_1)
    if es:
        parts.append(es)
    parts.append(_footer(tz, tweet))
    return "\n\n".join(parts)


def _render_msg1_2p(tz, tweet, translation, t_status) -> str:
    """2 条布局第一条：作者 →(译文)→ 原文 → 页脚。原文是中文则省略译文段。"""
    parts = [_header(tweet)]
    if not tweet.skip_translation:
        parts.append(_trans_section(translation, t_status, _TRANS_2))
    parts.append(_orig_section(tweet, _ORIG_2))
    parts.append(_footer(tz, tweet))
    return "\n\n".join(parts)


def _render_msg2_2p(explanation, e_status) -> str | None:
    """2 条布局第二条：解读。无内容 / 失败 → 返回 None（删掉这条）。"""
    return _expl_section(explanation, e_status, _EXPL_2) or None


# 三段都用「<b>标签</b> + 可展开折叠块」：默认折叠、点按展开，标签也保证各折叠块相互独立。

def _trans_section(translation, status, limit) -> str:
    if status == "done":
        body = html.escape(_clip(translation, limit))
    elif status == "failed":
        body = "<i>（翻译失败，见原文）</i>"
    else:
        body = "<i>翻译中…</i>"
    return f"🌐 <b>译文</b>\n<blockquote expandable>{body}</blockquote>"


def _orig_section(tweet, limit) -> str:
    return f"🐦 <b>原文</b>\n<blockquote expandable>{html.escape(_clip(tweet.text, limit))}</blockquote>"


def _expl_section(explanation, status, limit) -> str:
    """解读折叠块。标签带一行摘要（折叠时可见）。done 且无内容 / failed → 空串（整段省略）。"""
    if status == "done":
        if not explanation:
            return ""
        summary, body = _split_summary(explanation)
        label = f"💡 <b>解读</b> · {html.escape(summary)}" if summary else "💡 <b>解读</b>"
        return f"{label}\n<blockquote expandable>{_fmt_expl_inner(body, limit)}</blockquote>"
    if status == "failed":
        return ""
    return "💡 <b>解读</b>\n<blockquote expandable><i>解读中…</i></blockquote>"


def _split_summary(explanation: str) -> tuple[str, str]:
    """拆出开头的「📌 摘要：…」一行作为折叠预览，其余为正文。无摘要行则摘要为空。"""
    parts = explanation.split("\n", 1)
    first = parts[0].strip()
    if first.startswith("📌"):
        summary = first.lstrip("📌").strip()
        for p in ("摘要：", "摘要:", "摘要"):
            if summary.startswith(p):
                summary = summary[len(p):].strip()
                break
        body = parts[1].strip() if len(parts) > 1 else ""
        return summary, body
    return "", explanation


def _fmt_expl_inner(explanation: str, limit: int) -> str:
    """转义解读正文，把模型给的「📖 名词解释」「💬 解读」小标题加粗。"""
    s = html.escape(_clip(explanation, limit))
    return s.replace("📖 名词解释", "📖 <b>名词解释</b>").replace("💬 解读", "💬 <b>解读</b>")


def _header(tweet: Tweet) -> str:
    profile = f"https://x.com/{tweet.username}"
    label = f"<b>{html.escape(tweet.author_name)}</b> (@{html.escape(tweet.username)})"
    return f'🐦 <a href="{html.escape(profile, quote=True)}">{label}</a>'


def _footer(tz: ZoneInfo, tweet: Tweet) -> str:
    parts = [f'<a href="{html.escape(tweet.url, quote=True)}">🔗 原推</a>']
    if tweet.created_at:
        parts.append(tweet.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z"))  # %Z 注明 PDT/PST
    if tweet.media_status == "failed":
        # 媒体没发出去 → 把媒体链接补回来并注明是媒体（链接交 Telegram 自动可点）
        if tweet.media_links:
            parts.append("📎 媒体：" + " ".join(html.escape(u, quote=True) for u in tweet.media_links))
        else:
            parts.append("📎 媒体见原推")
    return " · ".join(parts)


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------- 内部 HTTP ----------

def _call(cfg: TelegramCfg, method: str, payload: dict):
    data = http_json(_API.format(token=cfg.bot_token, method=method), method="POST", body=payload, timeout=30)
    if not data.get("ok"):
        raise ApiError(f"Telegram {method}: {data.get('description') or data}")
    return data["result"]


def _send_text(cfg: TelegramCfg, body: str, chat_id: str | None = None) -> int:
    return _call(cfg, "sendMessage", {
        "chat_id": chat_id or cfg.chat_id, "text": body, "parse_mode": "HTML",
        "disable_web_page_preview": cfg.disable_web_page_preview,
        "disable_notification": cfg.disable_notification,
    })["message_id"]


def _edit_text(cfg: TelegramCfg, message_id: int, body: str) -> None:
    _call(cfg, "editMessageText", {
        "chat_id": cfg.chat_id, "message_id": message_id, "text": body, "parse_mode": "HTML",
        "disable_web_page_preview": cfg.disable_web_page_preview,
    })


def _send_media(cfg: TelegramCfg, photos: list[str], videos: list[str]) -> None:
    """把图片+视频作为最后的消息发出（≤10 项）。单项 sendPhoto/sendVideo，多项 sendMediaGroup。失败抛。

    视频走 URL 直发，Telegram 拉取有 ~20MB 上限；过大或失败由调用方降级为「页脚补媒体链接」。
    """
    items = [("photo", u) for u in photos] + [("video", u) for u in videos]
    items = items[:10]
    if not items:
        return
    if len(items) == 1:
        kind, url = items[0]
        method, key = ("sendPhoto", "photo") if kind == "photo" else ("sendVideo", "video")
        _call(cfg, method, {"chat_id": cfg.chat_id, key: url, "disable_notification": cfg.disable_notification})
    else:
        media = [{"type": k, "media": u} for k, u in items]
        _call(cfg, "sendMediaGroup", {"chat_id": cfg.chat_id, "media": media,
                                      "disable_notification": cfg.disable_notification})
