import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "US Equity Valuation Analyzer"
    app_env: str = "local"
    host: str = "127.0.0.1"
    port: int = 8000
    fmp_api_key: str = ""
    fmp_base_url: str = "https://financialmodelingprep.com/stable"
    cache_dir: str = ".cache"
    fmp_cache_enabled: bool = False
    fmp_cache_ttl_seconds: int = 43200
    fmp_peers_cache_ttl_seconds: int = 86400
    app_access_token: str = ""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("VALUATION_ANALYSIS_ENV_FILE"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cache_root_path(self) -> Path:
        return Path(self.cache_dir)


settings = Settings()
