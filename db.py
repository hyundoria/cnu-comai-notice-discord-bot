import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_channels (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    updated_at TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_articles (
    category    TEXT    NOT NULL,
    article_no  TEXT    NOT NULL,
    title       TEXT,
    seen_at     TEXT    NOT NULL,
    PRIMARY KEY (category, article_no)
);
CREATE INDEX IF NOT EXISTS idx_seen_seen_at ON seen_articles(seen_at);
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT    NOT NULL,
    article_no  TEXT    NOT NULL,
    guild_id    INTEGER NOT NULL,
    sent_at     TEXT    NOT NULL,
    success     INTEGER NOT NULL
);
"""

def _now():
    return datetime.now(timezone.utc).isoformat()

def init():
    with connect() as c:
        c.executescript(SCHEMA)
        c.execute("PRAGMA journal_mode=WAL")   # 동시 읽기/쓰기 성능 ↑

@contextmanager
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

# ---------- guild_channels ----------
def upsert_guild_channel(guild_id: int, channel_id: int):
    with connect() as c:
        c.execute("""
            INSERT INTO guild_channels(guild_id, channel_id, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE
              SET channel_id=excluded.channel_id, updated_at=excluded.updated_at
        """, (guild_id, channel_id, _now()))

def get_guild_channel(guild_id: int) -> int | None:
    with connect() as c:
        row = c.execute(
            "SELECT channel_id FROM guild_channels WHERE guild_id=?",
            (guild_id,)
        ).fetchone()
        return row["channel_id"] if row else None

def all_guild_channels() -> list[tuple[int, int]]:
    with connect() as c:
        rows = c.execute(
            "SELECT guild_id, channel_id FROM guild_channels"
        ).fetchall()
        return [(r["guild_id"], r["channel_id"]) for r in rows]

# ---------- seen_articles ----------
def is_baseline_empty(category: str) -> bool:
    with connect() as c:
        row = c.execute(
            "SELECT 1 FROM seen_articles WHERE category=? LIMIT 1",
            (category,)
        ).fetchone()
        return row is None

def filter_new(category: str, article_nos: list[str]) -> set[str]:
    """이 카테고리에서 처음 보는 article_no만 골라 set으로 반환."""
    if not article_nos:
        return set()
    with connect() as c:
        placeholders = ",".join("?" * len(article_nos))
        rows = c.execute(
            f"SELECT article_no FROM seen_articles "
            f"WHERE category=? AND article_no IN ({placeholders})",
            (category, *article_nos)
        ).fetchall()
        existing = {r["article_no"] for r in rows}
        return set(article_nos) - existing

def mark_seen(category: str, items: list[dict]):
    """items: [{'id':..., 'title':...}, ...]"""
    if not items:
        return
    now = _now()
    with connect() as c:
        c.executemany("""
            INSERT OR IGNORE INTO seen_articles(category, article_no, title, seen_at)
            VALUES(?, ?, ?, ?)
        """, [(category, n['id'], n.get('title',''), now) for n in items])

def trim_old(category: str, keep: int = 500):
    """카테고리당 최신 keep개만 유지."""
    with connect() as c:
        c.execute("""
            DELETE FROM seen_articles
            WHERE category=? AND article_no NOT IN (
                SELECT article_no FROM seen_articles
                WHERE category=?
                ORDER BY seen_at DESC LIMIT ?
            )
        """, (category, category, keep))

# ---------- notifications (선택) ----------
def log_notification(category: str, article_no: str, guild_id: int, success: bool):
    with connect() as c:
        c.execute("""
            INSERT INTO notifications(category, article_no, guild_id, sent_at, success)
            VALUES(?, ?, ?, ?, ?)
        """, (category, article_no, guild_id, _now(), 1 if success else 0))
