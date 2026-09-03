import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    RAZORPAY_KEY_ID: str | None = os.getenv("RAZORPAY_KEY_ID") or None
    RAZORPAY_KEY_SECRET: str | None = os.getenv("RAZORPAY_KEY_SECRET") or None
    RAZORPAY_WEBHOOK_SECRET: str | None = os.getenv("RAZORPAY_WEBHOOK_SECRET") or None
    GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or None
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

    MAX_AUTO_RETRIES: int = int(os.getenv("MAX_AUTO_RETRIES", 3))
    MAX_CONTACT_ATTEMPTS: int = int(os.getenv("MAX_CONTACT_ATTEMPTS", 2))
    QUIET_HOURS_START: int = int(os.getenv("QUIET_HOURS_START", 22))
    QUIET_HOURS_END: int = int(os.getenv("QUIET_HOURS_END", 8))
    RECOVERY_TIMEZONE: str = os.getenv("RECOVERY_TIMEZONE", "Asia/Kolkata")

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./data/vasooli.db")

    @property
    def razorpay_live(self) -> bool:
        return bool(self.RAZORPAY_KEY_ID and self.RAZORPAY_KEY_SECRET)

    @property
    def llm_live(self) -> bool:
        return bool(self.GEMINI_API_KEY)

    @property
    def llm_provider(self) -> str:
        return "gemini" if self.llm_live else "deterministic_fallback"

    @property
    def webhook_ready(self) -> bool:
        return bool(self.RAZORPAY_WEBHOOK_SECRET)


settings = Settings()
