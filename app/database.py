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
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                # Seamless migration for existing databases missing arxiv_id column
                try:
                    await db.execute("ALTER TABLE papers_cache ADD COLUMN arxiv_id TEXT")
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
                        doi, pdf_url, landing_url, source, citation_count, arxiv_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    paper.short_id, paper.title, paper.authors, paper.year,
                    paper.venue, paper.abstract, paper.doi, paper.pdf_url,
                    paper.landing_url, paper.source, paper.citation_count,
                    paper.arxiv_id
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
                        p.arxiv_id
                    )
                    for p in papers
                ]
                await db.executemany("""
                    INSERT OR REPLACE INTO papers_cache (
                        short_id, title, authors, year, venue, abstract,
                        doi, pdf_url, landing_url, source, citation_count, arxiv_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    arxiv_id=row["arxiv_id"] if "arxiv_id" in keys else None
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
                await db.commit()

    async def get_user_stats(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT searches_count FROM users WHERE user_id = ?", (user_id,)
            ) as cursor:
                user_row = await cursor.fetchone()
            async with db.execute("SELECT COUNT(*) FROM papers_cache") as cursor:
                cache_row = await cursor.fetchone()
            return {
                "user_searches": user_row[0] if user_row else 0,
                "total_cached_papers": cache_row[0] if cache_row else 0
            }


db = Database()
