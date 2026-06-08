from __future__ import annotations

from datetime import date as Date
from typing import Any

from pydantic import BaseModel, Field


class CompanyProfile(BaseModel):
    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    sic_code: str | None = None
    sic_description: str | None = None
    country: str | None = None
    website: str | None = None
    market_cap: float | None = None
    currency: str | None = None


class MarketSnapshot(BaseModel):
    price: float | None = None
    previous_close: float | None = None
    average_price_5d: float | None = None
    average_price_30d: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    trailing_eps: float | None = None
    forward_eps: float | None = None
    peg_ratio: float | None = None
    price_to_sales: float | None = None
    enterprise_to_ebitda: float | None = None
    week_52_low: float | None = Field(default=None, alias="fifty_two_week_low")
    week_52_high: float | None = Field(default=None, alias="fifty_two_week_high")

    model_config = {"populate_by_name": True}


class FinancialPeriod(BaseModel):
    period_end: Date | None = None
    revenue: float | None = None
    revenue_qoq_growth: float | None = None
    revenue_yoy_growth: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    net_income_qoq_growth: float | None = None
    net_income_yoy_growth: float | None = None
    diluted_eps: float | None = None
    diluted_eps_qoq_growth: float | None = None
    diluted_eps_yoy_growth: float | None = None
    free_cash_flow: float | None = None


class ForecastSnapshot(BaseModel):
    current_year_eps: float | None = None
    next_year_eps: float | None = None
    current_year_revenue: float | None = None
    next_year_revenue: float | None = None
    earnings_growth: float | None = None
    revenue_growth: float | None = None


class EarningsExecutionMetrics(BaseModel):
    observations: int = 0
    beat_count: int = 0
    miss_count: int = 0
    meet_count: int = 0
    beat_rate: float | None = None
    average_surprise_pct: float | None = None
    recent_events: list[dict[str, Any]] = Field(default_factory=list)


class ValuationHistoryPoint(BaseModel):
    date: Date | None = None
    price: float | None = None
    diluted_eps: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    price_to_sales: float | None = None
    enterprise_to_ebitda: float | None = None


class ValuationHistorySnapshot(BaseModel):
    points: list[ValuationHistoryPoint] = Field(default_factory=list)
    current_trailing_pe: float | None = None
    min_trailing_pe: float | None = None
    max_trailing_pe: float | None = None
    median_trailing_pe: float | None = None
    current_percentile: float | None = None
    current_forward_pe: float | None = None
    min_forward_pe: float | None = None
    max_forward_pe: float | None = None
    median_forward_pe: float | None = None
    forward_pe_percentile: float | None = None
    current_price_to_sales: float | None = None
    min_price_to_sales: float | None = None
    max_price_to_sales: float | None = None
    median_price_to_sales: float | None = None
    price_to_sales_percentile: float | None = None
    current_enterprise_to_ebitda: float | None = None
    min_enterprise_to_ebitda: float | None = None
    max_enterprise_to_ebitda: float | None = None
    median_enterprise_to_ebitda: float | None = None
    enterprise_to_ebitda_percentile: float | None = None


class PeerValuation(BaseModel):
    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    sic_code: str | None = None
    price: float | None = None
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    peg_ratio: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None


class ValuationAssessment(BaseModel):
    label: str
    valuation_score: int
    execution_score: int
    growth_score: int
    rationale: list[str]


class CompanyAnalysis(BaseModel):
    company: CompanyProfile
    market: MarketSnapshot
    valuation_history: ValuationHistorySnapshot = Field(default_factory=ValuationHistorySnapshot)
    financial_history: list[FinancialPeriod]
    forecast: ForecastSnapshot
    earnings_execution: EarningsExecutionMetrics
    peers: list[PeerValuation]
    assessment: ValuationAssessment
