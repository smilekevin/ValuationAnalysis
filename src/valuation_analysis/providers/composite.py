from __future__ import annotations

from datetime import date

from valuation_analysis.config import settings
from valuation_analysis.models import (
    CompanyProfile,
    EarningsExecutionMetrics,
    FinancialPeriod,
    ForecastSnapshot,
    MarketSnapshot,
    ValuationHistorySnapshot,
)
from valuation_analysis.providers.base import MarketDataProvider
from valuation_analysis.providers.fmp import FmpProvider
from valuation_analysis.providers.yahoo_finance import YahooFinanceProvider


def _growth_rate(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None or baseline == 0:
        return None
    return (current - baseline) / abs(baseline)


def _filled_metric_count(period: FinancialPeriod) -> int:
    return sum(
        value is not None
        for value in (
            period.revenue,
            period.net_income,
            period.diluted_eps,
            period.operating_income,
            period.free_cash_flow,
        )
    )


class CompositeMarketDataProvider(MarketDataProvider):
    def __init__(self) -> None:
        self.yahoo_provider = YahooFinanceProvider()
        self.fmp_provider = FmpProvider()
        self._last_peer_source_label = "本地股票池候选"
        self._progress_callback = None

    def set_progress_callback(self, progress_callback) -> None:
        self._progress_callback = progress_callback
        self.fmp_provider.set_progress_callback(progress_callback)

    def get_company_profile(self, symbol: str) -> CompanyProfile:
        if not self.fmp_provider.enabled:
            return self.yahoo_provider.get_company_profile(symbol)

        try:
            return self.fmp_provider.get_company_profile(symbol)
        except Exception as exc:
            self._log(
                f"FMP company profile failed for {symbol.upper()}：{exc.__class__.__name__}: {exc}",
                "warning",
            )
            return CompanyProfile(symbol=symbol.upper())

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        if not self.fmp_provider.enabled:
            return self.yahoo_provider.get_market_snapshot(symbol)

        try:
            return self.fmp_provider.get_market_snapshot(symbol)
        except Exception as exc:
            self._log(
                f"FMP market snapshot failed for {symbol.upper()}：{exc.__class__.__name__}: {exc}",
                "warning",
            )
            return MarketSnapshot()

    def get_financial_history(self, symbol: str, limit: int = 8) -> list[FinancialPeriod]:
        source_mode = settings.financial_history_source.strip().lower()

        if source_mode in {"yahoo_sec", "legacy", "fallback"}:
            return self.yahoo_provider.get_financial_history(symbol, limit=max(limit, 8))
        if not self.fmp_provider.enabled:
            return self.yahoo_provider.get_financial_history(symbol, limit=max(limit, 8))

        try:
            primary_periods = self.fmp_provider.get_financial_history(symbol, limit=max(limit, 8))
        except Exception as exc:
            self._log(
                f"FMP financial history failed for {symbol.upper()}：{exc.__class__.__name__}: {exc}",
                "warning",
            )
            primary_periods = []

        return primary_periods[:limit]

    def get_forecast(self, symbol: str) -> ForecastSnapshot:
        if not self.fmp_provider.enabled:
            return self.yahoo_provider.get_forecast(symbol)

        try:
            return self.fmp_provider.get_forecast(symbol)
        except Exception as exc:
            self._log(
                f"FMP forecast failed for {symbol.upper()}：{exc.__class__.__name__}: {exc}",
                "warning",
            )
            return ForecastSnapshot()

    def get_earnings_execution(self, symbol: str, limit: int = 10) -> EarningsExecutionMetrics:
        if self.fmp_provider.enabled:
            try:
                return self.fmp_provider.get_earnings_execution(symbol, limit=limit)
            except Exception as exc:
                self._log(
                    f"FMP earnings execution failed for {symbol.upper()}：{exc.__class__.__name__}: {exc}",
                    "warning",
                )
                return EarningsExecutionMetrics()
        return self.yahoo_provider.get_earnings_execution(symbol, limit=limit)

    def get_valuation_history(self, symbol: str, years: int = 12) -> ValuationHistorySnapshot:
        if self.fmp_provider.enabled:
            try:
                return self.fmp_provider.get_valuation_history(symbol, years=years)
            except Exception as exc:
                self._log(
                    f"FMP valuation history failed for {symbol.upper()}：{exc.__class__.__name__}: {exc}",
                    "warning",
                )
                return ValuationHistorySnapshot()
        return ValuationHistorySnapshot()

    def get_company_profile_for_peer(
        self,
        symbol: str,
        expected_sic_code: str | None = None,
        expected_sic_description: str | None = None,
    ) -> CompanyProfile:
        if self.fmp_provider.enabled:
            return self.fmp_provider.get_company_profile(symbol)
        return self.yahoo_provider.get_company_profile_for_peer(
            symbol,
            expected_sic_code,
            expected_sic_description,
        )

    def get_peer_candidate_symbols(
        self,
        target_symbol: str,
        profile: CompanyProfile,
        limit: int = 250,
    ) -> list[str]:
        if self.fmp_provider.enabled:
            try:
                candidates = self.fmp_provider.get_peer_candidate_symbols(target_symbol, limit=limit)
                if candidates:
                    self._last_peer_source_label = "FMP peers 候选池"
                    return candidates
                self._last_peer_source_label = "FMP peers 候选池"
                return []
            except Exception as exc:
                self._log(
                    f"FMP peers failed for {target_symbol.upper()}：{exc.__class__.__name__}: {exc}",
                    "warning",
                )
                return []

        candidates = self.yahoo_provider.get_peer_candidate_symbols(target_symbol, profile, limit=limit)
        self._last_peer_source_label = self.yahoo_provider.get_peer_candidate_source_label(profile)
        return candidates

    def get_peer_candidate_source_label(self, profile: CompanyProfile) -> str:
        return self._last_peer_source_label

    def allows_local_universe_fallback(self) -> bool:
        return not self.fmp_provider.enabled

    def _log(self, message: str, level: str = "progress") -> None:
        if self._progress_callback is not None:
            self._progress_callback(message, level)

    @staticmethod
    def _merge_financial_histories(
        primary_periods: list[FinancialPeriod],
        fallback_periods: list[FinancialPeriod],
    ) -> list[FinancialPeriod]:
        merged_periods = [period.model_copy(deep=True) for period in primary_periods]

        for fallback_period in fallback_periods:
            if fallback_period.period_end is None:
                continue

            matched_period = next(
                (
                    existing_period
                    for existing_period in merged_periods
                    if existing_period.period_end is not None
                    and abs((existing_period.period_end - fallback_period.period_end).days) <= 20
                ),
                None,
            )

            if matched_period is not None:
                if matched_period.revenue is None:
                    matched_period.revenue = fallback_period.revenue
                if matched_period.operating_income is None:
                    matched_period.operating_income = fallback_period.operating_income
                if matched_period.net_income is None:
                    matched_period.net_income = fallback_period.net_income
                if matched_period.diluted_eps is None:
                    matched_period.diluted_eps = fallback_period.diluted_eps
                if matched_period.free_cash_flow is None:
                    matched_period.free_cash_flow = fallback_period.free_cash_flow
                continue

            if _filled_metric_count(fallback_period) < 2:
                continue
            merged_periods.append(fallback_period.model_copy(deep=True))

        merged_periods.sort(key=lambda period: period.period_end or date.min, reverse=True)
        return merged_periods

    @staticmethod
    def _apply_growth_rates(periods: list[FinancialPeriod]) -> None:
        for index, period in enumerate(periods):
            previous_quarter = periods[index + 1] if index + 1 < len(periods) else None
            previous_year = periods[index + 4] if index + 4 < len(periods) else None

            period.revenue_qoq_growth = _growth_rate(
                period.revenue,
                previous_quarter.revenue if previous_quarter else None,
            )
            period.revenue_yoy_growth = _growth_rate(
                period.revenue,
                previous_year.revenue if previous_year else None,
            )
            period.net_income_qoq_growth = _growth_rate(
                period.net_income,
                previous_quarter.net_income if previous_quarter else None,
            )
            period.net_income_yoy_growth = _growth_rate(
                period.net_income,
                previous_year.net_income if previous_year else None,
            )
            period.diluted_eps_qoq_growth = _growth_rate(
                period.diluted_eps,
                previous_quarter.diluted_eps if previous_quarter else None,
            )
            period.diluted_eps_yoy_growth = _growth_rate(
                period.diluted_eps,
                previous_year.diluted_eps if previous_year else None,
            )
