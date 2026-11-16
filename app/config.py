import logging
import re
from pathlib import Path
from typing import Optional
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("PaperFinderBot.Config")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    BOT_TOKEN: str = ""
    CONTACT_EMAIL: str = "researcher@example.com"
    UNPAYWALL_EMAIL: Optional[str] = None
    TELEGRAM_PROXY: Optional[str] = None
    FALLBACK_MIRRORS: str = "https://sci-hub.ru,https://sci-hub.st,https://sci-hub.wf"
    PORT: int = 7860
    DB_PATH: str = str(DATA_DIR / "bot_cache.db")
    MAX_RESULTS: int = 20
    MAX_PDF_SIZE_MB: int = 45

    model_config = SettingsConfigDict(
        env_file=(str(BASE_DIR / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @model_validator(mode="after")
    def sync_and_validate(self):
        # Sync UNPAYWALL_EMAIL and CONTACT_EMAIL if one is set or default
        if not self.UNPAYWALL_EMAIL:
            self.UNPAYWALL_EMAIL = self.CONTACT_EMAIL
        elif self.CONTACT_EMAIL == "researcher@example.com" and self.UNPAYWALL_EMAIL:
            self.CONTACT_EMAIL = self.UNPAYWALL_EMAIL

        self.validate_credentials()
        return self

    @property
    def is_bot_token_valid(self) -> bool:
        """Check if BOT_TOKEN is provided and not a placeholder."""
        raw_token = (self.BOT_TOKEN or "").strip().strip("\"'")
        token_lower = raw_token.lower()
        token_placeholders = [
            "your_bot_token_here",
            "your_telegram_bot_token_here",
            "your_token_here",
            "bot_token_here",
            "1234567890:aah",
            "dummy_token",
            "placeholder",
        ]
        if not raw_token or any(p in token_lower for p in token_placeholders):
            return False
        return bool(re.match(r"^\d+:[A-Za-z0-9_-]+$", raw_token))

    @property
    def is_unpaywall_email_valid(self) -> bool:
        """Check if UNPAYWALL_EMAIL is provided and not a placeholder like example.com."""
        raw_email = (self.UNPAYWALL_EMAIL or self.CONTACT_EMAIL or "").strip()
        email_lower = raw_email.lower()
        email_placeholders = [
            "example.com",
            "example.org",
            "example.net",
            "test.com",
            "sample.com",
            "your_email",
            "researcher@example.com",
            "your_email@gmail.com",
            "your_email@example.com",
            "user@example.com",
        ]
        if not raw_email or any(p in email_lower for p in email_placeholders):
            return False
        if "@" not in email_lower or "." not in email_lower.split("@")[-1]:
            return False
        return True

    def validate_credentials(self) -> None:
        """Validate BOT_TOKEN and UNPAYWALL_EMAIL/CONTACT_EMAIL against empty or default placeholder values."""
        raw_token = (self.BOT_TOKEN or "").strip()
        token_lower = raw_token.lower()
        token_placeholders = [
            "your_bot_token_here",
            "your_telegram_bot_token_here",
            "your_token_here",
            "bot_token_here",
            "1234567890:aah",
        ]

        if not raw_token:
            logger.error(
                "❌ [CONFIG ERROR] BOT_TOKEN is empty! "
                "Please set a valid Telegram Bot Token from @BotFather in your .env file."
            )
        elif any(p in token_lower for p in token_placeholders):
            logger.error(
                f"❌ [CONFIG ERROR] BOT_TOKEN contains default placeholder text: '{raw_token}'. "
                "Please replace it with your real Telegram Bot Token from @BotFather in your .env file."
            )
        elif not re.match(r"^\d+:[A-Za-z0-9_-]+$", raw_token.strip("\"'")):
            logger.warning(
                f"⚠️ [CONFIG WARNING] BOT_TOKEN format looks unusual ('{raw_token[:10]}...'). "
                "Ensure it matches standard Telegram Bot token structure (<digits>:<token>)."
            )

        # --- Check UNPAYWALL_EMAIL / CONTACT_EMAIL ---
        raw_email = (self.UNPAYWALL_EMAIL or self.CONTACT_EMAIL or "").strip()
        if not self.is_unpaywall_email_valid:
            if not raw_email:
                logger.warning(
                    "⚠️ [CONFIG WARNING] UNPAYWALL_EMAIL / CONTACT_EMAIL is empty! "
                    "OpenAlex & Unpaywall APIs require an email for their polite pool (higher rate limits)."
                )
            else:
                logger.warning(
                    f"⚠️ [CONFIG WARNING] UNPAYWALL_EMAIL / CONTACT_EMAIL is set to a placeholder or invalid value ('{raw_email}'). "
                    "Please update it in your .env file with a real email to access OpenAlex & Unpaywall Polite Pool."
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
