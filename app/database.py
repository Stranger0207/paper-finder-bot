import asyncio
import aiosqlite
from typing import Optional
from app.config import settings
from app.services.models import PaperItem


class Database:
    def __init__(self, db_path: str = settings.DB_PATH):
        self.db_path = db_path
        self._lock: Optional[asyncio.Lock] = None

    @property
    def lock(self) -> asyncio.Lock:
        """Lazy thread-safe asyncio.Lock for SQLite transaction serialization."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def init_db(self):
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        full_name TEXT,
                        searches_count INTEGER DEFAULT 0,
                        is_premium INTEGER DEFAULT 0,
                        premium_expires_at TIMESTAMP,
                        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS papers_cache (
                        short_id TEXT PRIMARY KEY,
                        title TEXT,
                        authors TEXT,
                        year TEXT,
                        venue TEXT,
                        abstract TEXT,
                        doi TEXT,
                        pdf_url TEXT,
                        landing_url TEXT,
                        source TEXT,
                        citation_count INTEGER DEFAULT 0,
                        arxiv_id TEXT,
                        tldr TEXT,
                        publisher TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Seamless migration for existing databases missing columns
                try:
                    await db.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0")
                except Exception:
                    pass
                try:
                    await db.execute("ALTER TABLE users ADD COLUMN premium_expires_at TIMESTAMP")
                except Exception:
                    pass
                try:
                    await db.execute("ALTER TABLE papers_cache ADD COLUMN arxiv_id TEXT")
                except Exception:
                    pass
                try:
                    await db.execute("ALTER TABLE papers_cache ADD COLUMN tldr TEXT")
                except Exception:
                    pass
                try:
                    await db.execute("ALTER TABLE papers_cache ADD COLUMN publisher TEXT")
                except Exception:
                    pass

                await db.execute("""
                    CREATE TABLE IF NOT EXISTS search_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        query TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS search_sessions (
                        session_id TEXT PRIMARY KEY,
                        query TEXT,
                        last_page INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS bookmarks (
                        user_id INTEGER,
                        short_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, short_id)
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS pending_receipts (
                        user_id INTEGER PRIMARY KEY,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.commit()

        await self.cleanup_old_sessions()

    async def register_or_update_user(self, user_id: int, username: Optional[str], full_name: str):
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT INTO users (user_id, username, full_name, searches_count)
                    VALUES (?, ?, ?, 0)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username = excluded.username,
                        full_name = excluded.full_name
                """, (user_id, username or "", full_name))
                await db.commit()

    async def check_rate_limit(self, user_id: int, max_per_minute: int = 25) -> bool:
        """Return True if rate limit is exceeded (e.g. >25 searches per minute), False otherwise."""
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    """SELECT COUNT(*) FROM search_logs
                       WHERE user_id = ? AND created_at >= datetime('now', '-1 minute')""",
                    (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    count = row[0] if row else 0
                    return count >= max_per_minute

    async def log_search(self, user_id: int, query: str):
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO search_logs (user_id, query) VALUES (?, ?)",
                    (user_id, query)
                )
                await db.execute(
                    "UPDATE users SET searches_count = searches_count + 1 WHERE user_id = ?",
                    (user_id,)
                )
                await db.commit()

    async def save_paper(self, paper: PaperItem):
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO papers_cache (
                        short_id, title, authors, year, venue, abstract,
                        doi, pdf_url, landing_url, source, citation_count, arxiv_id,
                        tldr, publisher
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    paper.short_id, paper.title, paper.authors, paper.year,
                    paper.venue, paper.abstract, paper.doi, paper.pdf_url,
                    paper.landing_url, paper.source, paper.citation_count,
                    paper.arxiv_id, paper.tldr, paper.publisher
                ))
                await db.commit()

    async def save_papers_batch(self, papers: list[PaperItem]):
        """Save a batch of PaperItem records in a single database transaction using executemany."""
        if not papers:
            return
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                params = [
                    (
                        p.short_id, p.title, p.authors, p.year,
                        p.venue, p.abstract, p.doi, p.pdf_url,
                        p.landing_url, p.source, p.citation_count,
                        p.arxiv_id, p.tldr, p.publisher
                    )
                    for p in papers
                ]
                await db.executemany("""
                    INSERT OR REPLACE INTO papers_cache (
                        short_id, title, authors, year, venue, abstract,
                        doi, pdf_url, landing_url, source, citation_count, arxiv_id,
                        tldr, publisher
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, params)
                await db.commit()

    async def get_paper(self, short_id: str) -> Optional[PaperItem]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM papers_cache WHERE short_id = ?", (short_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                keys = row.keys()
                paper = PaperItem(
                    title=row["title"],
                    authors=row["authors"],
                    year=row["year"],
                    venue=row["venue"],
                    abstract=row["abstract"],
                    doi=row["doi"],
                    pdf_url=row["pdf_url"],
                    landing_url=row["landing_url"],
                    source=row["source"],
                    citation_count=row["citation_count"],
                    arxiv_id=row["arxiv_id"] if "arxiv_id" in keys else None,
                    tldr=row["tldr"] if "tldr" in keys else None,
                    publisher=row["publisher"] if "publisher" in keys else None
                )
                paper.short_id = row["short_id"]
                return paper

    async def save_search_session(self, session_id: str, query: str, page: int = 1):
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO search_sessions (session_id, query, last_page)
                    VALUES (?, ?, ?)
                """, (session_id, query, page))
                await db.commit()

    async def get_search_session(self, session_id: str) -> Optional[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM search_sessions WHERE session_id = ?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return {
                    "session_id": row["session_id"],
                    "query": row["query"],
                    "last_page": row["last_page"]
                }

    async def cleanup_old_sessions(self, hours: int = 24, search_log_days: int = 7):
        """Cleanup search sessions older than given hours and search logs older than given days."""
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    f"DELETE FROM search_sessions WHERE created_at < datetime('now', '-{hours} hours')"
                )
                await db.execute(
                    f"DELETE FROM search_logs WHERE created_at < datetime('now', '-{search_log_days} days')"
                )
                await db.execute(
                    "DELETE FROM pending_receipts WHERE created_at < datetime('now', '-2 days')"
                )
                await db.commit()

    # ── Premium Subscription & Receipts Methods ──────────────────────

    async def set_user_premium(self, user_id: int, status: int = 1, days: int = 30):
        """Activate premium subscription for specified days (default 30) or deactivate."""
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                if status == 1:
                    await db.execute("""
                        INSERT INTO users (user_id, is_premium, premium_expires_at)
                        VALUES (?, 1, datetime('now', ?))
                        ON CONFLICT(user_id) DO UPDATE SET
                            is_premium = 1,
                            premium_expires_at = datetime('now', ?)
                    """, (user_id, f"+{days} days", f"+{days} days"))
                else:
                    await db.execute("""
                        UPDATE users SET is_premium = 0, premium_expires_at = NULL WHERE user_id = ?
                    """, (user_id,))
                await db.commit()

    async def is_user_premium(self, user_id: int) -> bool:
        """Check if user has an active premium subscription and it hasn't expired."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT 1 FROM users
                WHERE user_id = ? AND is_premium = 1 AND (premium_expires_at IS NULL OR premium_expires_at > datetime('now'))
            """, (user_id,)) as cursor:
                return (await cursor.fetchone()) is not None

    async def get_user_premium_info(self, user_id: int) -> tuple[bool, Optional[str]]:
        """Return tuple (is_active, premium_expires_at)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("""
                SELECT is_premium, premium_expires_at FROM users WHERE user_id = ?
            """, (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return False, None
                is_prem, expires_at = row[0], row[1]
                if not is_prem:
                    return False, None
                async with db.execute("""
                    SELECT 1 FROM users
                    WHERE user_id = ? AND is_premium = 1 AND (premium_expires_at IS NULL OR premium_expires_at > datetime('now'))
                """, (user_id,)) as exp_cursor:
                    is_active = (await exp_cursor.fetchone()) is not None
                    return is_active, expires_at

    async def add_pending_receipt(self, user_id: int):
        """Add user to pending receipts table waiting for receipt upload."""
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO pending_receipts (user_id, created_at)
                    VALUES (?, datetime('now'))
                """, (user_id,))
                await db.commit()

    async def remove_pending_receipt(self, user_id: int):
        """Remove user from pending receipts table."""
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM pending_receipts WHERE user_id = ?", (user_id,))
                await db.commit()

    async def has_pending_receipt(self, user_id: int) -> bool:
        """Check if user is waiting to send payment receipt."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT 1 FROM pending_receipts WHERE user_id = ?", (user_id,)) as cursor:
                return (await cursor.fetchone()) is not None

    async def get_user_stats(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT searches_count, is_premium, premium_expires_at FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                user_row = await cursor.fetchone()
            async with db.execute("SELECT COUNT(*) FROM papers_cache") as cursor:
                cache_row = await cursor.fetchone()

            is_premium = False
            premium_expires = None
            if user_row and user_row[1]:
                async with db.execute(
                    "SELECT 1 FROM users WHERE user_id = ? AND is_premium = 1 AND (premium_expires_at IS NULL OR premium_expires_at > datetime('now'))",
                    (user_id,)
                ) as exp_cursor:
                    if await exp_cursor.fetchone():
                        is_premium = True
                        premium_expires = user_row[2]

            return {
                "user_searches": user_row[0] if user_row else 0,
                "total_cached_papers": cache_row[0] if cache_row else 0,
                "is_premium": is_premium,
                "premium_expires_at": premium_expires
            }

    # ── Bookmark / Personal Library Methods ──────────────────────────

    async def toggle_bookmark(self, user_id: int, short_id: str) -> bool:
        """Toggle bookmark for a paper. Returns True if bookmarked, False if removed."""
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT 1 FROM bookmarks WHERE user_id = ? AND short_id = ?",
                    (user_id, short_id)
                ) as cursor:
                    exists = await cursor.fetchone()
                if exists:
                    await db.execute(
                        "DELETE FROM bookmarks WHERE user_id = ? AND short_id = ?",
                        (user_id, short_id)
                    )
                    await db.commit()
                    return False
                else:
                    await db.execute(
                        "INSERT INTO bookmarks (user_id, short_id) VALUES (?, ?)",
                        (user_id, short_id)
                    )
                    await db.commit()
                    return True

    async def is_bookmarked(self, user_id: int, short_id: str) -> bool:
        """Check if a paper is bookmarked by the user."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM bookmarks WHERE user_id = ? AND short_id = ?",
                (user_id, short_id)
            ) as cursor:
                return (await cursor.fetchone()) is not None

    async def get_user_bookmarks(self, user_id: int) -> list[PaperItem]:
        """Get all bookmarked papers for a user, ordered by most recent first."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT p.* FROM papers_cache p
                INNER JOIN bookmarks b ON p.short_id = b.short_id
                WHERE b.user_id = ?
                ORDER BY b.created_at DESC
            """, (user_id,)) as cursor:
                rows = await cursor.fetchall()
                papers = []
                for row in rows:
                    keys = row.keys()
                    paper = PaperItem(
                        title=row["title"],
                        authors=row["authors"],
                        year=row["year"],
                        venue=row["venue"],
                        abstract=row["abstract"],
                        doi=row["doi"],
                        pdf_url=row["pdf_url"],
                        landing_url=row["landing_url"],
                        source=row["source"],
                        citation_count=row["citation_count"],
                        arxiv_id=row["arxiv_id"] if "arxiv_id" in keys else None,
                        tldr=row["tldr"] if "tldr" in keys else None,
                        publisher=row["publisher"] if "publisher" in keys else None
                    )
                    paper.short_id = row["short_id"]
                    papers.append(paper)
                return papers

    async def clear_user_bookmarks(self, user_id: int) -> None:
        """Remove all bookmarks for a user."""
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "DELETE FROM bookmarks WHERE user_id = ?", (user_id,)
                )
                await db.commit()


db = Database()
