import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "US Equity Valuation Analyzer"
    app_env: str = "local"
    host: str = "127.0.0.1"
    port: int = 8000
    fmp_api_key: str = ""
    fmp_base_url: str = "https://financialmodelingprep.com/stable"
    fmp_max_workers: int = 4
    app_access_token: str = ""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("VALUATION_ANALYSIS_ENV_FILE"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

settings = Settings()
