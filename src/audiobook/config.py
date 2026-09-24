from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AB_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    llm_concurrency: int = 8
    synth_concurrency: int = 4
    tts_endpoints: list[str] = []
    worker_poll_seconds: float = 1.0
    lease_seconds: int = 30

    @property
    def db_path(self) -> Path:
        return self.data_dir / "service.db"

    @property
    def books_dir(self) -> Path:
        return self.data_dir / "books"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"


def get_settings(**overrides) -> Settings:
    return Settings(**overrides)
