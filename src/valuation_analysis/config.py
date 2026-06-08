from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    app_name: str = "US Equity Valuation Analyzer"
    app_env: str = "local"
    host: str = "127.0.0.1"
    port: int = 8000
    default_peer_universe_path: str = "data/seed/us_equity_universe.csv"
    sec_api_user_agent: str = "ValuationAnalysis/0.1 contact@example.com"
    fmp_enabled: bool = True
    fmp_api_key: str = ""
    fmp_base_url: str = "https://financialmodelingprep.com/stable"
    financial_history_source: str = "auto"
    cache_dir: str = ".cache"
    fmp_cache_enabled: bool = False
    fmp_cache_ttl_seconds: int = 43200
    fmp_peers_cache_ttl_seconds: int = 86400

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cache_root_path(self) -> Path:
        return Path(self.cache_dir)


settings = Settings()
