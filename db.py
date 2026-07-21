import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 파이썬 3.8 호환을 위해 typing 모듈에서 필요한 타입들을 모두 불러옵니다.
from typing import Optional, List, Tuple, Set, Dict, Any

DB_DIR = Path(__file__).parent / "data"
DB_DIR.mkdir(exist_ok=True) # 폴더가 없으면 자동으로 생성
DB_PATH = DB_DIR / "bot.db"

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
CREATE TABLE IF NOT EXISTS deadlines (
    category      TEXT    NOT NULL,
    article_no    TEXT    NOT NULL,
    title         TEXT,
    link          TEXT,
    deadline_date TEXT    NOT NULL,   -- 'YYYY-MM-DD'
    reminded      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    PRIMARY KEY (category, article_no)
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

# 수정 1: int | None -> Optional[int]
def get_guild_channel(guild_id: int) -> Optional[int]:
    with connect() as c:
        row = c.execute(
            "SELECT channel_id FROM guild_channels WHERE guild_id=?",
            (guild_id,)
        ).fetchone()
        return row["channel_id"] if row else None

# 수정 2: list[tuple[int, int]] -> List[Tuple[int, int]]
def all_guild_channels() -> List[Tuple[int, int]]:
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

# 수정 3: list[str] -> List[str], set[str] -> Set[str]
def filter_new(category: str, article_nos: List[str]) -> Set[str]:
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

# 수정 4: list[dict] -> List[Dict[str, Any]]
def mark_seen(category: str, items: List[Dict[str, Any]]):
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

# ---------- 통계 (/통계 명령용) ----------
def notification_stats(days: int = 7) -> Dict[str, Any]:
    """최근 days일 카테고리별 전송 성공/실패 + 전체 누계·마지막 전송 시각."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect() as c:
        per_cat = c.execute("""
            SELECT category,
                   SUM(success)     AS ok,
                   SUM(1 - success) AS fail
            FROM notifications
            WHERE sent_at >= ?
            GROUP BY category
            ORDER BY category
        """, (cutoff,)).fetchall()
        totals = c.execute("""
            SELECT SUM(success)     AS ok,
                   SUM(1 - success) AS fail,
                   MAX(sent_at)     AS last_at
            FROM notifications
        """).fetchone()
        seen = c.execute("""
            SELECT category, COUNT(*) AS n
            FROM seen_articles
            GROUP BY category
            ORDER BY category
        """).fetchall()
    return {
        "days": days,
        "per_category": [(r["category"], r["ok"] or 0, r["fail"] or 0) for r in per_cat],
        "total_ok": (totals["ok"] or 0) if totals else 0,
        "total_fail": (totals["fail"] or 0) if totals else 0,
        "last_at": totals["last_at"] if totals else None,
        "seen_counts": [(r["category"], r["n"]) for r in seen],
    }

# ---------- deadlines (마감 리마인더용) ----------
def add_deadline(category: str, article_no: str, title: str, link: str, deadline_date: str):
    """이미 있으면 무시(reminded 플래그 보존)."""
    with connect() as c:
        c.execute("""
            INSERT OR IGNORE INTO deadlines(category, article_no, title, link, deadline_date, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
        """, (category, article_no, title, link, deadline_date, _now()))

def due_deadlines(from_date: str, to_date: str) -> List[Dict[str, Any]]:
    """아직 리마인드하지 않은, from_date~to_date(포함) 사이 마감 항목."""
    with connect() as c:
        rows = c.execute("""
            SELECT category, article_no, title, link, deadline_date
            FROM deadlines
            WHERE reminded = 0 AND deadline_date >= ? AND deadline_date <= ?
            ORDER BY deadline_date ASC
        """, (from_date, to_date)).fetchall()
        return [dict(r) for r in rows]

def mark_deadline_reminded(category: str, article_no: str):
    with connect() as c:
        c.execute(
            "UPDATE deadlines SET reminded = 1 WHERE category = ? AND article_no = ?",
            (category, article_no),
        )

def upcoming_deadlines(from_date: str, limit: int = 10) -> List[Dict[str, Any]]:
    """from_date 이후 다가오는 마감 목록(/마감 명령용)."""
    with connect() as c:
        rows = c.execute("""
            SELECT category, article_no, title, link, deadline_date
            FROM deadlines
            WHERE deadline_date >= ?
            ORDER BY deadline_date ASC
            LIMIT ?
        """, (from_date, limit)).fetchall()
        return [dict(r) for r in rows]

def trim_old_deadlines(before_date: str):
    """마감이 지난 지 오래된 항목 정리."""
    with connect() as c:
        c.execute("DELETE FROM deadlines WHERE deadline_date < ?", (before_date,))