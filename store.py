"""SQLite 状态库：推文去重 + 「翻译」「解读」两槽异步填充状态机 + 每博主游标/故障旗标。

发送顺序：先发原文（译文位「翻译中…」、解读位「解读中…」占位）→ 填译文 → 填解读。
布局：内容短→1 条消息(msg2_id 为空)；长→2 条(msg1=译文+原文+页脚, msg2=解读)。
每槽 status：pending→done/failed，各带 attempts 跨轮重试。
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from source import Tweet

_DDL = """
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id      TEXT PRIMARY KEY,
    username      TEXT,
    author_name   TEXT,
    created_at    TEXT,
    url           TEXT,
    has_video     INTEGER NOT NULL DEFAULT 0,
    original_text TEXT,
    photos        TEXT,                          -- JSON：图片直链（发送/重试用）
    videos        TEXT,                          -- JSON：视频 mp4 直链
    media_links   TEXT,                          -- JSON：文中媒体 t.co 短链（失败时显示）
    tg_chat_id    TEXT,
    post_status   TEXT NOT NULL DEFAULT 'pending',  -- pending/done/failed：原文是否已发出（含本地重试）
    post_attempts INTEGER NOT NULL DEFAULT 0,
    msg1_id       INTEGER,
    msg2_id       INTEGER,                 -- 2 条布局时的第二条；1 条布局为 NULL
    two_part      INTEGER NOT NULL DEFAULT 0,
    media_status  TEXT NOT NULL DEFAULT 'none',  -- none / ok / failed（失败则页脚补媒体链接）
    translation   TEXT,
    trans_status  TEXT NOT NULL DEFAULT 'pending',   -- pending/done/failed
    trans_attempts INTEGER NOT NULL DEFAULT 0,
    explanation   TEXT,
    expl_status   TEXT NOT NULL DEFAULT 'pending',   -- pending/done/failed
    expl_attempts INTEGER NOT NULL DEFAULT 0,
    inserted_at   REAL
);
CREATE TABLE IF NOT EXISTS users (
    username     TEXT PRIMARY KEY,
    last_checked REAL,
    alerted      INTEGER NOT NULL DEFAULT 0,
    updated_at   REAL
);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_DDL)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- 去重 / 入库 ---

    def seen(self, tweet_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone() is not None

    def add_new(self, tweet: Tweet, chat_id: str) -> None:
        """抓到推文即存库（内容落盘、去重标记）；post_status=pending，等发送 pass 来发。

        原文明显是中文时 trans_status='skipped'：不翻译、渲染时也不显示译文段。
        """
        trans_status = "skipped" if tweet.skip_translation else "pending"
        self.db.execute(
            "INSERT OR IGNORE INTO tweets(tweet_id, username, author_name, created_at, url, has_video, "
            "original_text, photos, videos, media_links, tg_chat_id, post_status, trans_status, inserted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                tweet.id, tweet.username, tweet.author_name,
                tweet.created_at.isoformat() if tweet.created_at else None,
                tweet.url, int(tweet.has_video), tweet.text,
                json.dumps(tweet.photos), json.dumps(tweet.videos), json.dumps(tweet.media_links),
                str(chat_id), trans_status, time.time(),
            ),
        )
        self.db.commit()

    # --- 发送（原文+占位）：可本地重试，不再调 twitter ---

    def pending_posts(self, max_attempts: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM tweets WHERE post_status='pending' AND post_attempts < ? ORDER BY inserted_at",
            (max_attempts,),
        ).fetchall()

    def mark_posted(self, tweet_id: str, msg1_id: int, msg2_id: int | None,
                    two_part: bool, media_status: str) -> None:
        self.db.execute(
            "UPDATE tweets SET post_status='done', msg1_id=?, msg2_id=?, two_part=?, media_status=? "
            "WHERE tweet_id=?",
            (msg1_id, msg2_id, int(two_part), media_status, tweet_id),
        )
        self.db.commit()

    def bump_post(self, tweet_id: str) -> int:
        self.db.execute("UPDATE tweets SET post_attempts=post_attempts+1 WHERE tweet_id=?", (tweet_id,))
        self.db.commit()
        return self.db.execute("SELECT post_attempts FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone()[0]

    def fail_post(self, tweet_id: str) -> None:
        self.db.execute("UPDATE tweets SET post_status='failed' WHERE tweet_id=?", (tweet_id,))
        self.db.commit()

    def pending_fills(self) -> list[sqlite3.Row]:
        """已发出、但翻译或解读还没填好的推文。"""
        return self.db.execute(
            "SELECT * FROM tweets WHERE post_status='done' "
            "AND (trans_status='pending' OR expl_status='pending') ORDER BY inserted_at"
        ).fetchall()

    # --- 翻译槽 ---

    def set_translation(self, tweet_id: str, text: str) -> None:
        self.db.execute("UPDATE tweets SET translation=?, trans_status='done' WHERE tweet_id=?", (text, tweet_id))
        self.db.commit()

    def bump_trans(self, tweet_id: str) -> int:
        self.db.execute("UPDATE tweets SET trans_attempts=trans_attempts+1 WHERE tweet_id=?", (tweet_id,))
        self.db.commit()
        return self.db.execute("SELECT trans_attempts FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone()[0]

    def fail_trans(self, tweet_id: str) -> None:
        self.db.execute("UPDATE tweets SET trans_status='failed' WHERE tweet_id=?", (tweet_id,))
        self.db.commit()

    # --- 解读槽 ---

    def set_explanation(self, tweet_id: str, text: str) -> None:
        self.db.execute("UPDATE tweets SET explanation=?, expl_status='done' WHERE tweet_id=?", (text, tweet_id))
        self.db.commit()

    def bump_expl(self, tweet_id: str) -> int:
        self.db.execute("UPDATE tweets SET expl_attempts=expl_attempts+1 WHERE tweet_id=?", (tweet_id,))
        self.db.commit()
        return self.db.execute("SELECT expl_attempts FROM tweets WHERE tweet_id=?", (tweet_id,)).fetchone()[0]

    def fail_expl(self, tweet_id: str) -> None:
        self.db.execute("UPDATE tweets SET expl_status='failed' WHERE tweet_id=?", (tweet_id,))
        self.db.commit()

    # --- 每博主：轮询游标 + 故障提示 ---

    def get_last_checked(self, username: str) -> float | None:
        row = self.db.execute("SELECT last_checked FROM users WHERE username=?", (username,)).fetchone()
        return row[0] if row else None

    def set_last_checked(self, username: str, ts: float) -> None:
        self._upsert_user(username, last_checked=ts)

    def get_alerted(self, username: str) -> bool:
        row = self.db.execute("SELECT alerted FROM users WHERE username=?", (username,)).fetchone()
        return bool(row[0]) if row else False

    def set_alerted(self, username: str, alerted: bool) -> None:
        self._upsert_user(username, alerted=1 if alerted else 0)

    def _upsert_user(self, username: str, **fields) -> None:
        cols = ", ".join(fields)
        ph = ", ".join("?" for _ in fields)
        upd = ", ".join(f"{k}=excluded.{k}" for k in fields)
        self.db.execute(
            f"INSERT INTO users(username, {cols}, updated_at) VALUES (?, {ph}, ?) "
            f"ON CONFLICT(username) DO UPDATE SET {upd}, updated_at=excluded.updated_at",
            (username, *fields.values(), time.time()),
        )
        self.db.commit()


def tweet_from_row(row: sqlite3.Row) -> Tweet:
    """从状态库行还原 Tweet（发送/重试要用 photos/videos；编辑重渲染要用文本/媒体状态）。"""
    created = None
    if row["created_at"]:
        try:
            created = datetime.fromisoformat(row["created_at"])
        except ValueError:
            created = None

    def _jload(key):
        try:
            return json.loads(row[key]) if row[key] else []
        except (ValueError, TypeError):
            return []

    return Tweet(
        id=row["tweet_id"],
        username=row["username"] or "",
        author_name=row["author_name"] or row["username"] or "",
        text=row["original_text"] or "",
        url=row["url"] or "",
        created_at=created,
        photos=_jload("photos"),
        videos=_jload("videos"),
        has_video=bool(row["has_video"]),
        media_links=_jload("media_links"),
        media_status=row["media_status"] or "none",
        skip_translation=(row["trans_status"] == "skipped"),
    )
