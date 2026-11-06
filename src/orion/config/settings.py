"""Application settings and environment configuration."""

from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from environment variables."""

    app_name: str = "ORION"
    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False

    # Telegram configuration
    telegram_bot_token: str | None = None
    telegram_owner_id: int | None = None

    # Ollama configuration
    ollama_model: str = "qwen3.5:4b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_timeout: float = 60.0
    ollama_plan_num_predict: int = 1024

    # Memory & storage configuration
    database_path: str = "data/orion.db"
    max_history_messages: int = 10
    max_goal_length: int = 500

    # Tool & security configuration
    allowed_directories: str = ""

    # Voice notes & speech-to-text configuration (Step 12)
    voice_enabled: bool = True
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_download_root: str | None = None
    max_voice_duration_seconds: int = 60
    max_voice_file_size_bytes: int = 5 * 1024 * 1024  # 5 MB

    # Reminders & timezone configuration (Step 17)
    timezone: str = "Asia/Kolkata"
    reminder_check_interval_seconds: float = 10.0

    def get_allowed_directories(self) -> list[Path]:
        """Return list of canonical resolved allowed directory paths."""
        if not self.allowed_directories or not self.allowed_directories.strip():
            return []
        dirs: list[Path] = []
        for part in self.allowed_directories.split(","):
            clean = part.strip()
            if clean:
                dirs.append(Path(clean).resolve())
        return dirs

    model_config = SettingsConfigDict(
        env_prefix="ORION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def validate_telegram_configuration(self) -> None:
        """Validate required Telegram credentials before bot startup.

        Raises ValueError with clear local setup instructions if missing or invalid.
        Never logs or prints secret values.
        """
        missing: list[str] = []
        if not self.telegram_bot_token or not self.telegram_bot_token.strip():
            missing.append("ORION_TELEGRAM_BOT_TOKEN")
        if self.telegram_owner_id is None or self.telegram_owner_id <= 0:
            missing.append("ORION_TELEGRAM_OWNER_ID (must be a positive integer)")

        if missing:
            raise ValueError(
                "Missing or invalid Telegram configuration:\n"
                + "\n".join(f"- {item}" for item in missing)
                + "\n\nSetup instructions:\n"
                "1. Copy .env.example to .env (if not done yet).\n"
                "2. Obtain your bot token from @BotFather on Telegram.\n"
                "3. Obtain your numeric user ID from @userinfobot on Telegram.\n"
                "4. Add ORION_TELEGRAM_BOT_TOKEN and ORION_TELEGRAM_OWNER_ID to your .env file."
            )


@lru_cache
def get_settings() -> Settings:
    """Return a cached instance of the application settings."""
    return Settings()
