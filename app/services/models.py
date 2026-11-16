import hashlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PaperItem:
    title: str
    authors: str = "نامشخص"
    year: str = "نامشخص"
    venue: str = "نامشخص"
    abstract: str = "چکیده‌ای برای این مقاله ثبت نشده است."
    doi: Optional[str] = None
    pdf_url: Optional[str] = None
    landing_url: Optional[str] = None
    source: str = "OpenAlex"
    citation_count: int = 0
    arxiv_id: Optional[str] = None
    short_id: str = field(init=False)

    def __post_init__(self):
        unique_key = (self.doi or self.arxiv_id or self.landing_url or self.title or "").strip().lower()
        self.short_id = hashlib.sha256(unique_key.encode("utf-8")).hexdigest()[:10]
