from __future__ import annotations

from abc import ABC, abstractmethod

from valuation_analysis.models import (
    CompanyProfile,
    EarningsExecutionMetrics,
    FinancialPeriod,
    ForecastSnapshot,
    MarketSnapshot,
    ValuationHistorySnapshot,
)
from valuation_analysis.progress import ProgressCallback


class MarketDataProvider(ABC):
    @abstractmethod
    def get_company_profile(self, symbol: str) -> CompanyProfile:
        raise NotImplementedError

    @abstractmethod
    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_financial_history(self, symbol: str, limit: int = 8) -> list[FinancialPeriod]:
        raise NotImplementedError

    @abstractmethod
    def get_forecast(self, symbol: str) -> ForecastSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_earnings_execution(self, symbol: str, limit: int = 10) -> EarningsExecutionMetrics:
        raise NotImplementedError

    def get_valuation_history(self, symbol: str, years: int = 12) -> ValuationHistorySnapshot:
        return ValuationHistorySnapshot()

    def get_company_profile_for_peer(
        self,
        symbol: str,
        expected_sic_code: str | None = None,
        expected_sic_description: str | None = None,
    ) -> CompanyProfile:
        return self.get_company_profile(symbol)

    def get_peer_candidate_symbols(
        self,
        target_symbol: str,
        profile: CompanyProfile,
        limit: int = 250,
    ) -> list[str]:
        return []

    def get_peer_candidate_source_label(self, profile: CompanyProfile) -> str:
        return "自定义候选池"

    def set_progress_callback(self, progress_callback: ProgressCallback | None) -> None:
        return None
