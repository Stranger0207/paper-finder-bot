import aiosqlite
from typing import Optional
from app.config import settings
from app.services.models import PaperItem


class Database:
    def __init__(self, db_path: str = settings.DB_PATH):
        self.db_path = db_path

    async def init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS search_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    query TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    async def register_or_update_user(self, user_id: int, username: Optional[str], full_name: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO users (user_id, username, full_name, searches_count)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name
            """, (user_id, username or "", full_name))
            await db.commit()

    async def log_search(self, user_id: int, query: str):
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
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO papers_cache (
                    short_id, title, authors, year, venue, abstract,
                    doi, pdf_url, landing_url, source, citation_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                paper.short_id, paper.title, paper.authors, paper.year,
                paper.venue, paper.abstract, paper.doi, paper.pdf_url,
                paper.landing_url, paper.source, paper.citation_count
            ))
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
                    citation_count=row["citation_count"]
                )
                paper.short_id = row["short_id"]
                return paper

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
