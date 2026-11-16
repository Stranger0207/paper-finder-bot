import re
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    BOT_TOKEN: str
    CONTACT_EMAIL: str = "researcher@example.com"
    TELEGRAM_PROXY: Optional[str] = None
    FALLBACK_MIRRORS: str = "https://sci-hub.ru,https://sci-hub.st,https://sci-hub.wf"
    PORT: int = 7860
    DB_PATH: str = str(DATA_DIR / "bot_cache.db")
    MAX_RESULTS: int = 20
    MAX_PDF_SIZE_MB: int = 45

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def clean_proxy(self) -> Optional[str]:
        if self.TELEGRAM_PROXY and self.TELEGRAM_PROXY.strip():
            return self.TELEGRAM_PROXY.strip()
        return None

    @property
    def mirrors_list(self) -> list[str]:
        mirrors = []
        cleaned = re.sub(r'\[.*?\]\((.*?)\)', r'\1', self.FALLBACK_MIRRORS)
        for m in cleaned.split(","):
            m = m.strip().rstrip("/")
            if m.startswith("http"):
                mirrors.append(m)
        return mirrors or ["https://sci-hub.ru", "https://sci-hub.st", "https://sci-hub.wf"]


settings = Settings()
