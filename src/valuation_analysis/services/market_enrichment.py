from __future__ import annotations

from valuation_analysis.models import (
    EarningsExecutionMetrics,
    FinancialPeriod,
    ForecastSnapshot,
    MarketSnapshot,
    ValuationHistorySnapshot,
)


def enrich_market_with_forecast(
    market: MarketSnapshot,
    forecast: ForecastSnapshot,
) -> None:
    if market.forward_eps is None:
        market.forward_eps = forecast.next_year_eps or forecast.current_year_eps

    if (
        market.forward_pe is None
        and market.price is not None
        and market.forward_eps is not None
        and market.forward_eps > 0
    ):
        market.forward_pe = market.price / market.forward_eps

    if (
        market.peg_ratio is None
        and market.forward_pe is not None
        and market.forward_pe > 0
        and forecast.earnings_growth is not None
        and forecast.earnings_growth > 0
    ):
        market.peg_ratio = market.forward_pe / (forecast.earnings_growth * 100)


def enrich_market_with_financial_history(
    market: MarketSnapshot,
    financial_history: list[FinancialPeriod],
    execution: EarningsExecutionMetrics | None = None,
) -> None:
    ttm_eps = _ttm_eps_from_earnings(execution)
    if ttm_eps is None and not _has_suspicious_low_trailing_pe(market):
        ttm_eps = _ttm_eps_from_financial_history(financial_history)

    if ttm_eps is None:
        if _has_suspicious_low_trailing_pe(market):
            market.trailing_eps = None
            market.trailing_pe = None
        return

    if market.trailing_eps is None or _has_suspicious_low_trailing_pe(market):
        market.trailing_eps = ttm_eps

    if (
        market.price is not None
        and market.trailing_eps is not None
        and market.trailing_eps > 0
    ):
        recalculated_pe = market.price / market.trailing_eps
        if market.trailing_pe is None or _has_suspicious_low_trailing_pe(market):
            market.trailing_pe = recalculated_pe


def enrich_market_with_valuation_history(
    market: MarketSnapshot,
    valuation_history: ValuationHistorySnapshot,
) -> None:
    if (
        market.enterprise_to_ebitda is None
        and valuation_history.current_enterprise_to_ebitda is not None
    ):
        market.enterprise_to_ebitda = valuation_history.current_enterprise_to_ebitda

    if market.price_to_sales is None and valuation_history.current_price_to_sales is not None:
        market.price_to_sales = valuation_history.current_price_to_sales


def _ttm_eps_from_earnings(execution: EarningsExecutionMetrics | None) -> float | None:
    if execution is None:
        return None

    recent_eps = [
        event.get("reported_eps")
        for event in execution.recent_events[:4]
        if isinstance(event.get("reported_eps"), (int, float))
    ]
    if len(recent_eps) != 4:
        return None

    ttm_eps = sum(float(value) for value in recent_eps)
    return ttm_eps if ttm_eps > 0 else None


def _ttm_eps_from_financial_history(financial_history: list[FinancialPeriod]) -> float | None:
    recent_eps = [
        period.diluted_eps
        for period in financial_history[:4]
        if period.diluted_eps is not None
    ]
    if len(recent_eps) != 4:
        return None

    ttm_eps = sum(recent_eps)
    return ttm_eps if ttm_eps > 0 else None


def _has_suspicious_low_trailing_pe(market: MarketSnapshot) -> bool:
    if market.trailing_pe is None or market.trailing_pe >= 3:
        return False
    if market.price is not None and market.price <= 10:
        return False
    if market.forward_pe is not None and market.forward_pe >= 8:
        return True
    if (
        market.trailing_eps is not None
        and market.price is not None
        and market.trailing_eps > market.price / 3
    ):
        return True
    return False
