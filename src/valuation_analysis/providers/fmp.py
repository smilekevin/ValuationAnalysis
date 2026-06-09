from __future__ import annotations

from datetime import date, datetime, timedelta
from bisect import bisect_right
from statistics import mean

import httpx

from valuation_analysis.config import settings
from valuation_analysis.models import (
    CompanyProfile,
    EarningsExecutionMetrics,
    FinancialPeriod,
    ForecastSnapshot,
    MarketSnapshot,
    ValuationHistoryPoint,
    ValuationHistorySnapshot,
)
from valuation_analysis.providers.base import MarketDataProvider
from valuation_analysis.progress import ProgressCallback


def _safe_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"none", "null", "nan", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _safe_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None

    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _growth_rate(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None or baseline == 0:
        return None
    return (current - baseline) / abs(baseline)


class FmpProvider(MarketDataProvider):
    def __init__(self) -> None:
        self.api_key = settings.fmp_api_key.strip()
        self.base_url = settings.fmp_base_url.rstrip("/")
        self.progress_callback: ProgressCallback | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def set_progress_callback(self, progress_callback: ProgressCallback | None) -> None:
        self.progress_callback = progress_callback

    def get_company_profile(self, symbol: str) -> CompanyProfile:
        if not self.enabled:
            return CompanyProfile(symbol=symbol.upper())

        record = self._get_first_record(
            "profile",
            {
                "symbol": symbol.upper(),
            },
        )
        return CompanyProfile(
            symbol=symbol.upper(),
            name=self._pick_text(record, ["companyName", "name"]),
            sector=self._pick_text(record, ["sector"]),
            industry=self._pick_text(record, ["industry"]),
            country=self._pick_text(record, ["country"]),
            website=self._pick_text(record, ["website"]),
            market_cap=self._pick_value(record, ["mktCap", "marketCap"]),
            currency=self._pick_text(record, ["currency"]),
        )

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        if not self.enabled:
            return MarketSnapshot()

        symbol = symbol.upper()
        quote = self._get_first_record(
            "quote",
            {
                "symbol": symbol,
            },
        )
        ratios_ttm = self._try_get_first_record("ratios-ttm", {"symbol": symbol})
        key_metrics_ttm = self._try_get_first_record("key-metrics-ttm", {"symbol": symbol})
        recent_prices = self._get_recent_close_prices(symbol)
        price = self._pick_value(quote, ["price"])
        trailing_pe = self._pick_first_value(
            [
                (quote, ["pe", "priceEarningsRatio"]),
                (ratios_ttm, ["priceEarningsRatioTTM", "peRatioTTM"]),
                (key_metrics_ttm, ["peRatioTTM", "priceEarningsRatioTTM"]),
            ]
        )
        trailing_eps = self._pick_first_value(
            [
                (quote, ["eps", "epsdiluted", "epsDiluted"]),
                (key_metrics_ttm, ["netIncomePerShareTTM", "epsDilutedTTM", "epsTTM"]),
                (ratios_ttm, ["epsDilutedTTM", "epsTTM"]),
            ]
        )
        if trailing_eps is None and price is not None and trailing_pe is not None and trailing_pe > 0:
            trailing_eps = price / trailing_pe

        return MarketSnapshot(
            price=price,
            previous_close=self._pick_value(quote, ["previousClose"]),
            average_price_5d=self._rolling_average(recent_prices, 5),
            average_price_30d=self._rolling_average(recent_prices, 30),
            trailing_pe=trailing_pe,
            trailing_eps=trailing_eps,
            peg_ratio=self._pick_first_value(
                [
                    (quote, ["pegRatio", "pegratio"]),
                    (ratios_ttm, ["priceEarningsToGrowthRatioTTM", "pegRatioTTM"]),
                    (key_metrics_ttm, ["pegRatioTTM", "priceEarningsToGrowthRatioTTM"]),
                ]
            ),
            price_to_sales=self._pick_first_value(
                [
                    (quote, ["priceToSalesRatio", "priceToSales"]),
                    (ratios_ttm, ["priceToSalesRatioTTM"]),
                    (key_metrics_ttm, ["priceToSalesRatioTTM"]),
                ]
            ),
            enterprise_to_ebitda=self._pick_first_value(
                [
                    (quote, ["enterpriseValueOverEBITDA", "evToEbitda"]),
                    (key_metrics_ttm, ["enterpriseValueOverEBITDATTM", "evToEBITDATTM"]),
                    (ratios_ttm, ["enterpriseValueMultipleTTM", "evToEBITDATTM"]),
                ]
            ),
            week_52_low=self._pick_value(quote, ["yearLow"]),
            week_52_high=self._pick_value(quote, ["yearHigh"]),
        )

    def _get_recent_close_prices(self, symbol: str, lookback_days: int = 75) -> list[float]:
        try:
            records = self._get_records(
                "historical-price-eod/full",
                {
                    "symbol": symbol.upper(),
                    "from": (date.today() - timedelta(days=lookback_days)).isoformat(),
                    "to": date.today().isoformat(),
                },
            )
        except Exception:
            return []
        dated_prices = [
            (price_date, close)
            for record in records
            if (price_date := _safe_date(record.get("date"))) is not None
            and (close := self._pick_value(record, ["close"])) is not None
        ]
        dated_prices.sort(key=lambda item: item[0])
        return [close for _, close in dated_prices]

    def get_valuation_history(self, symbol: str, years: int = 12) -> ValuationHistorySnapshot:
        if not self.enabled:
            return ValuationHistorySnapshot()

        quarter_limit = max(years * 4 + 8, 56)
        quarter_records = self._get_quarter_income_statement_records(symbol, quarter_limit)
        if len(quarter_records) < 4:
            return ValuationHistorySnapshot()
        earnings_eps_by_date = dict(self._get_quarter_eps_points_from_earnings(symbol, quarter_limit))
        earnings_eps_points = sorted(earnings_eps_by_date.items())
        requires_earnings_eps = self._has_eps_scale_mismatch(
            quarter_records,
            earnings_eps_by_date,
        )
        if earnings_eps_by_date:
            adjusted_quarter_records = []
            for period_end, diluted_eps, revenue, diluted_shares in quarter_records:
                matched_eps = self._find_nearest_eps(period_end, earnings_eps_by_date)
                if matched_eps is not None:
                    effective_eps = matched_eps
                elif requires_earnings_eps:
                    effective_eps = None
                else:
                    effective_eps = diluted_eps
                adjusted_quarter_records.append(
                    (period_end, effective_eps, revenue, diluted_shares)
                )
            quarter_records = adjusted_quarter_records

        try:
            ratio_records = self._get_records(
                "ratios",
                {
                    "symbol": symbol.upper(),
                    "period": "quarter",
                    "limit": quarter_limit,
                },
            )
        except Exception:
            ratio_records = []
        if not ratio_records:
            ratio_records = self._get_records(
                "ratios",
                {
                    "symbol": symbol.upper(),
                    "limit": max(years + 5, 15),
                },
            )
        quarterly_ratios_by_date = {
            ratio_date: record
            for record in ratio_records
            if (ratio_date := _safe_date(record.get("date"))) is not None
            and str(record.get("period") or "").upper() != "FY"
        }
        annual_ratios_by_date = {
            ratio_date: record
            for record in ratio_records
            if (ratio_date := _safe_date(record.get("date"))) is not None
            and str(record.get("period") or "").upper() == "FY"
        }

        pe_source_points = (
            earnings_eps_points
            if len(earnings_eps_points) >= 4
            else [
                (period_end, diluted_eps)
                for period_end, diluted_eps, _, _ in quarter_records
                if diluted_eps is not None
            ]
        )
        oldest_candidates = [period_end for period_end, *_ in quarter_records] + [
            period_end for period_end, _ in pe_source_points
        ]
        oldest_date = min(oldest_candidates)
        price_records = self._get_records(
            "historical-price-eod/full",
            {
                "symbol": symbol.upper(),
                "from": (oldest_date - timedelta(days=10)).isoformat(),
                "to": date.today().isoformat(),
            },
        )
        price_by_date = {
            price_date: close
            for record in price_records
            if (price_date := _safe_date(record.get("date"))) is not None
            and (close := self._pick_value(record, ["close"])) is not None
        }
        sorted_price_dates = sorted(price_by_date.keys())
        calculated_ps_ratio_checks: list[float] = []
        for index in range(3, len(quarter_records)):
            period_end, _, _, diluted_shares = quarter_records[index]
            revenue_window = [
                revenue_value
                for _, _, revenue_value, _ in quarter_records[index - 3 : index + 1]
                if revenue_value is not None and revenue_value > 0
            ]
            revenue_ttm = sum(revenue_window) if len(revenue_window) == 4 else None
            matched_date = self._find_price_date_on_or_before(period_end, sorted_price_dates)
            price = price_by_date.get(matched_date) if matched_date is not None else None
            ratio_record = quarterly_ratios_by_date.get(period_end)
            if ratio_record is None:
                ratio_record = self._find_nearest_ratio_record(period_end, annual_ratios_by_date)
            ratio_price_to_sales = self._pick_value(ratio_record, ["priceToSalesRatio"])
            calculated_price_to_sales = (
                (price * diluted_shares) / revenue_ttm
                if price is not None
                and diluted_shares is not None
                and diluted_shares > 0
                and revenue_ttm is not None
                and revenue_ttm > 0
                else None
            )
            if (
                ratio_price_to_sales is not None
                and ratio_price_to_sales > 0
                and calculated_price_to_sales is not None
                and calculated_price_to_sales > 0
            ):
                calculated_ps_ratio_checks.append(
                    calculated_price_to_sales / ratio_price_to_sales
                )
        calculated_ps_mismatched = False
        if len(calculated_ps_ratio_checks) >= 3:
            median_ps_ratio = self._median(calculated_ps_ratio_checks)
            calculated_ps_mismatched = (
                median_ps_ratio is not None
                and (median_ps_ratio < 0.5 or median_ps_ratio > 2.0)
            )

        points: list[ValuationHistoryPoint] = []
        for index in range(3, len(quarter_records)):
            period_end, _, revenue, diluted_shares = quarter_records[index]
            revenue_window = [
                revenue_value
                for _, _, revenue_value, _ in quarter_records[index - 3 : index + 1]
                if revenue_value is not None and revenue_value > 0
            ]
            revenue_ttm = sum(revenue_window) if len(revenue_window) == 4 else None

            matched_date = self._find_price_date_on_or_before(period_end, sorted_price_dates)
            if matched_date is None:
                continue
            price = price_by_date.get(matched_date)
            if price is None:
                continue

            ratio_record = quarterly_ratios_by_date.get(period_end)
            if ratio_record is None:
                ratio_record = self._find_nearest_ratio_record(period_end, annual_ratios_by_date)
            ratio_price_to_sales = self._pick_value(ratio_record, ["priceToSalesRatio"])
            calculated_price_to_sales = (
                (price * diluted_shares) / revenue_ttm
                if diluted_shares is not None
                and diluted_shares > 0
                and revenue_ttm is not None
                and revenue_ttm > 0
                else None
            )
            price_to_sales = (
                ratio_price_to_sales
                if calculated_ps_mismatched
                else calculated_price_to_sales or ratio_price_to_sales
            )
            enterprise_to_ebitda = self._pick_value(
                ratio_record,
                ["enterpriseValueMultiple", "evToEBITDA", "enterpriseToEbitda"],
            )
            if price_to_sales is None and enterprise_to_ebitda is None:
                continue

            points.append(
                ValuationHistoryPoint(
                    date=period_end,
                    price=price,
                    price_to_sales=price_to_sales,
                    enterprise_to_ebitda=enterprise_to_ebitda,
                )
            )

        for index in range(3, len(pe_source_points)):
            period_end, _ = pe_source_points[index]
            eps_window = [eps for _, eps in pe_source_points[index - 3 : index + 1]]
            if len(eps_window) < 4:
                continue
            diluted_eps_ttm = sum(eps_window)
            if diluted_eps_ttm <= 0:
                continue

            matched_date = self._find_price_date_on_or_before(period_end, sorted_price_dates)
            if matched_date is None:
                continue
            price = price_by_date.get(matched_date)
            if price is None:
                continue

            forward_eps_window = [eps for _, eps in pe_source_points[index + 1 : index + 5]]
            forward_eps_ntm = sum(forward_eps_window) if len(forward_eps_window) == 4 else None
            points.append(
                ValuationHistoryPoint(
                    date=period_end,
                    price=price,
                    diluted_eps=diluted_eps_ttm,
                    trailing_pe=price / diluted_eps_ttm,
                    forward_pe=(
                        price / forward_eps_ntm
                        if forward_eps_ntm is not None and forward_eps_ntm > 0
                        else None
                    ),
                )
            )

        if points:
            return self._build_valuation_history_snapshot(points, years=years)

        annual_records = self._get_records(
            "income-statement",
            {
                "symbol": symbol.upper(),
                "limit": max(years + 5, 15),
            },
        )
        annual_records = [
            record
            for record in annual_records
            if str(record.get("period") or "").upper() == "FY"
        ]
        fiscal_points = []
        for record in annual_records:
            period_end = _safe_date(record.get("date"))
            diluted_eps = self._pick_value(record, ["epsdiluted", "epsDiluted", "eps"])
            if period_end is not None and diluted_eps is not None and diluted_eps > 0:
                fiscal_points.append((period_end, diluted_eps))

        if not fiscal_points:
            return ValuationHistorySnapshot()

        ratio_records = self._get_records(
            "ratios",
            {
                "symbol": symbol.upper(),
                "limit": max(years + 5, 15),
            },
        )
        ratios_by_date = {
            ratio_date: record
            for record in ratio_records
            if (ratio_date := _safe_date(record.get("date"))) is not None
        }

        oldest_date = min(period_end for period_end, _ in fiscal_points)
        price_records = self._get_records(
            "historical-price-eod/full",
            {
                "symbol": symbol.upper(),
                "from": (oldest_date - timedelta(days=10)).isoformat(),
                "to": date.today().isoformat(),
            },
        )
        price_by_date = {
            price_date: close
            for record in price_records
            if (price_date := _safe_date(record.get("date"))) is not None
            and (close := self._pick_value(record, ["close"])) is not None
        }
        sorted_price_dates = sorted(price_by_date.keys())

        points = []
        for period_end, diluted_eps in sorted(fiscal_points):
            matched_date = self._find_price_date_on_or_before(period_end, sorted_price_dates)
            if matched_date is None:
                continue
            price = price_by_date.get(matched_date)
            if price is None:
                continue
            trailing_pe = price / diluted_eps if diluted_eps > 0 else None
            ratio_record = ratios_by_date.get(period_end, {})
            price_to_sales = self._pick_value(ratio_record, ["priceToSalesRatio"])
            enterprise_to_ebitda = self._pick_value(
                ratio_record,
                ["enterpriseValueMultiple", "evToEBITDA", "enterpriseToEbitda"],
            )
            if trailing_pe is None and price_to_sales is None and enterprise_to_ebitda is None:
                continue
            points.append(
                ValuationHistoryPoint(
                    date=period_end,
                    price=price,
                    diluted_eps=diluted_eps,
                    trailing_pe=trailing_pe,
                    price_to_sales=price_to_sales,
                    enterprise_to_ebitda=enterprise_to_ebitda,
                )
            )

        return self._build_valuation_history_snapshot(points, years=years)

    def _build_valuation_history_snapshot(
        self,
        points: list[ValuationHistoryPoint],
        years: int,
    ) -> ValuationHistorySnapshot:
        if not points:
            return ValuationHistorySnapshot()

        points = sorted(
            points,
            key=lambda point: point.date or date.min,
        )
        latest_point_date = next(
            (point.date for point in reversed(points) if point.date is not None),
            None,
        )
        if latest_point_date is not None:
            cutoff_date = latest_point_date - timedelta(days=365 * years + 45)
            points = [
                point
                for point in points
                if point.date is None or point.date >= cutoff_date
            ]

        pe_values = [
            point.trailing_pe
            for point in points
            if point.trailing_pe is not None and point.trailing_pe > 0
        ]

        latest_pe = next(
            (
                point.trailing_pe
                for point in reversed(points)
                if point.trailing_pe is not None and point.trailing_pe > 0
            ),
            None,
        )
        forward_pe_values = [
            point.forward_pe
            for point in points
            if point.forward_pe is not None and point.forward_pe > 0
        ]
        ps_values = [
            point.price_to_sales
            for point in points
            if point.price_to_sales is not None and point.price_to_sales > 0
        ]
        ev_ebitda_values = [
            point.enterprise_to_ebitda
            for point in points
            if point.enterprise_to_ebitda is not None and point.enterprise_to_ebitda > 0
        ]
        latest_ps = next(
            (
                point.price_to_sales
                for point in reversed(points)
                if point.price_to_sales is not None and point.price_to_sales > 0
            ),
            None,
        )
        latest_ev_ebitda = next(
            (
                point.enterprise_to_ebitda
                for point in reversed(points)
                if point.enterprise_to_ebitda is not None and point.enterprise_to_ebitda > 0
            ),
            None,
        )
        latest_forward_pe = next(
            (
                point.forward_pe
                for point in reversed(points)
                if point.forward_pe is not None and point.forward_pe > 0
            ),
            None,
        )

        return ValuationHistorySnapshot(
            points=points,
            current_trailing_pe=latest_pe,
            min_trailing_pe=min(pe_values) if pe_values else None,
            max_trailing_pe=max(pe_values) if pe_values else None,
            median_trailing_pe=self._median(pe_values),
            current_percentile=self._percentile(pe_values, latest_pe),
            current_forward_pe=latest_forward_pe,
            min_forward_pe=min(forward_pe_values) if forward_pe_values else None,
            max_forward_pe=max(forward_pe_values) if forward_pe_values else None,
            median_forward_pe=self._median(forward_pe_values),
            forward_pe_percentile=self._percentile(forward_pe_values, latest_forward_pe),
            current_price_to_sales=latest_ps,
            min_price_to_sales=min(ps_values) if ps_values else None,
            max_price_to_sales=max(ps_values) if ps_values else None,
            median_price_to_sales=self._median(ps_values),
            price_to_sales_percentile=self._percentile(ps_values, latest_ps),
            current_enterprise_to_ebitda=latest_ev_ebitda,
            min_enterprise_to_ebitda=min(ev_ebitda_values) if ev_ebitda_values else None,
            max_enterprise_to_ebitda=max(ev_ebitda_values) if ev_ebitda_values else None,
            median_enterprise_to_ebitda=self._median(ev_ebitda_values),
            enterprise_to_ebitda_percentile=self._percentile(
                ev_ebitda_values,
                latest_ev_ebitda,
            ),
        )

    def _get_quarter_eps_points_from_earnings(
        self,
        symbol: str,
        limit: int,
    ) -> list[tuple[date, float]]:
        try:
            records = self._get_records(
                "earnings",
                {
                    "symbol": symbol.upper(),
                    "limit": limit,
                },
            )
        except Exception:
            return []

        points: list[tuple[date, float]] = []
        for record in records:
            event_date = _safe_date(record.get("date"))
            reported_eps = self._pick_value(
                record,
                [
                    "eps",
                    "epsActual",
                    "actualEps",
                    "reportedEPS",
                    "reportedEps",
                ],
            )
            if event_date is None or reported_eps is None:
                continue
            points.append((event_date, reported_eps))

        points.sort(key=lambda item: item[0])
        return points

    def _get_quarter_eps_points_from_income_statement(
        self,
        symbol: str,
        limit: int,
    ) -> list[tuple[date, float]]:
        records = self._get_quarter_income_statement_records(symbol, limit)
        return [
            (period_end, diluted_eps)
            for period_end, diluted_eps, _, _ in records
        ]

    def _get_quarter_income_statement_records(
        self,
        symbol: str,
        limit: int,
    ) -> list[tuple[date, float, float | None, float | None]]:
        quarter_records = self._get_records(
            "income-statement",
            {
                "symbol": symbol.upper(),
                "period": "quarter",
                "limit": limit,
            },
        )
        quarter_records = [
            record
            for record in quarter_records
            if str(record.get("period") or "").upper() != "FY"
        ]

        points: list[tuple[date, float, float | None, float | None]] = []
        for record in quarter_records:
            period_end = _safe_date(record.get("date"))
            diluted_eps = self._pick_value(record, ["epsdiluted", "epsDiluted", "eps"])
            revenue = self._pick_value(record, ["revenue", "totalRevenue"])
            diluted_shares = self._pick_value(
                record,
                [
                    "weightedAverageShsOutDil",
                    "weightedAverageSharesDiluted",
                    "weightedAverageDilutedSharesOutstanding",
                    "dilutedAverageShares",
                ],
            )
            if period_end is None or diluted_eps is None:
                continue
            points.append((period_end, diluted_eps, revenue, diluted_shares))

        points.sort(key=lambda item: item[0])
        return points

    def get_financial_history(self, symbol: str, limit: int = 12) -> list[FinancialPeriod]:
        if not self.enabled:
            return []

        records = self._get_records(
            "income-statement",
            {
                "symbol": symbol.upper(),
                "period": "quarter",
                "limit": max(limit, 12),
            },
        )

        periods: list[FinancialPeriod] = []
        for record in records:
            period_end = _safe_date(record.get("date"))
            if period_end is None:
                continue

            period = FinancialPeriod(
                period_end=period_end,
                revenue=self._pick_value(
                    record,
                    [
                        "revenue",
                        "totalRevenue",
                    ],
                ),
                operating_income=self._pick_value(
                    record,
                    [
                        "operatingIncome",
                        "operatingIncomeLoss",
                    ],
                ),
                net_income=self._pick_value(
                    record,
                    [
                        "netIncome",
                        "netIncomeApplicableToCommonShares",
                        "netIncomeAvailableToCommonStockholders",
                    ],
                ),
                diluted_eps=self._pick_value(
                    record,
                    [
                        "epsdiluted",
                        "epsDiluted",
                        "eps",
                    ],
                ),
            )

            if (
                period.revenue is None
                and period.net_income is None
                and period.diluted_eps is None
            ):
                continue

            periods.append(period)

        periods.sort(key=lambda period: period.period_end or date.min, reverse=True)
        self._backfill_eps_from_earnings(symbol, periods, limit=max(limit, 12))
        self._apply_growth_rates(periods)
        return periods[:limit]

    def get_earnings_execution(self, symbol: str, limit: int = 10) -> EarningsExecutionMetrics:
        if not self.enabled:
            return EarningsExecutionMetrics()

        records = self._get_records(
            "earnings",
            {
                "symbol": symbol.upper(),
                "limit": max(limit, 10),
            },
        )

        rows = []
        surprises: list[float] = []
        beat_count = 0
        miss_count = 0
        meet_count = 0

        records.sort(
            key=lambda record: _safe_date(record.get("date")) or date.min,
            reverse=True,
        )
        for record in records[:limit]:
            event_date = _safe_date(record.get("date"))
            estimate = self._pick_value(
                record,
                [
                    "epsEstimated",
                    "estimatedEps",
                    "epsEstimate",
                ],
            )
            reported = self._pick_value(
                record,
                [
                    "eps",
                    "epsActual",
                    "actualEps",
                ],
            )
            surprise_pct = self._pick_value(
                record,
                [
                    "surprisePercentage",
                    "surprisePercent",
                    "epsSurprisePercent",
                ],
            )
            if event_date is None or estimate is None or reported is None:
                continue

            if surprise_pct is None and estimate != 0:
                surprise_pct = ((reported - estimate) / abs(estimate)) * 100

            if reported > estimate:
                beat_count += 1
                status = "beat"
            elif reported < estimate:
                miss_count += 1
                status = "miss"
            else:
                meet_count += 1
                status = "meet"

            if surprise_pct is not None:
                surprises.append(surprise_pct)

            rows.append(
                {
                    "date": event_date,
                    "eps_estimate": estimate,
                    "reported_eps": reported,
                    "reported_eps_label": "实际 EPS（数据源口径）",
                    "surprise_pct": surprise_pct,
                    "status": status,
                }
            )

        observations = len(rows)
        beat_rate = beat_count / observations if observations else None
        avg_surprise = mean(surprises) if surprises else None

        return EarningsExecutionMetrics(
            observations=observations,
            beat_count=beat_count,
            miss_count=miss_count,
            meet_count=meet_count,
            beat_rate=beat_rate,
            average_surprise_pct=avg_surprise,
            recent_events=rows,
        )

    def get_forecast(self, symbol: str) -> ForecastSnapshot:
        if not self.enabled:
            return ForecastSnapshot()

        records = self._get_records(
            "analyst-estimates",
            {
                "symbol": symbol.upper(),
                "period": "annual",
                "page": 0,
                "limit": 10,
            },
        )
        if not records:
            return ForecastSnapshot()

        future_records = [
            record
            for record in records
            if (_safe_date(record.get("date")) or date.max).year >= date.today().year
        ]
        future_records.sort(key=lambda record: _safe_date(record.get("date")) or date.max)
        current_record = future_records[0] if future_records else None
        next_record = future_records[1] if len(future_records) > 1 else None

        current_year_eps = self._pick_value(
            current_record,
            ["estimatedEpsAvg", "epsAvg", "estimatedEps", "eps"],
        )
        next_year_eps = self._pick_value(
            next_record,
            ["estimatedEpsAvg", "epsAvg", "estimatedEps", "eps"],
        )
        current_year_revenue = self._pick_value(
            current_record,
            ["estimatedRevenueAvg", "revenueAvg", "estimatedRevenue"],
        )
        next_year_revenue = self._pick_value(
            next_record,
            ["estimatedRevenueAvg", "revenueAvg", "estimatedRevenue"],
        )

        return ForecastSnapshot(
            current_year_eps=current_year_eps,
            next_year_eps=next_year_eps,
            current_year_revenue=current_year_revenue,
            next_year_revenue=next_year_revenue,
            earnings_growth=_growth_rate(next_year_eps, current_year_eps),
            revenue_growth=_growth_rate(next_year_revenue, current_year_revenue),
        )

    def get_peer_candidate_symbols(
        self,
        target_symbol: str,
        profile: CompanyProfile | None = None,
        limit: int = 50,
    ) -> list[str]:
        if not self.enabled:
            return []

        symbol = target_symbol.upper()
        params = {"symbol": symbol}
        self._log(f"FMP 实时请求: stock-peers {symbol}。", "progress")
        response = httpx.get(
            f"{self.base_url}/stock-peers",
            params={**params, "apikey": self.api_key},
            timeout=20.0,
        )
        response.raise_for_status()
        payload = response.json()

        raw_symbols: list[object] = []
        if isinstance(payload, list):
            raw_symbols = payload
        elif isinstance(payload, dict):
            peers_list = payload.get("peersList") or payload.get("symbols") or payload.get("peers")
            if isinstance(peers_list, list):
                raw_symbols = peers_list

        symbols: list[str] = []
        for value in raw_symbols:
            if isinstance(value, dict):
                candidate = str(value.get("symbol") or "").strip().upper()
            else:
                candidate = str(value).strip().upper()
            if not candidate or candidate == symbol:
                continue
            if candidate not in symbols:
                symbols.append(candidate)
        return symbols[:limit]

    def get_peer_candidate_source_label(self, profile: CompanyProfile) -> str:
        return "FMP peers 候选池"

    def _get_first_record(self, endpoint: str, params: dict[str, object]) -> dict:
        records = self._get_records(endpoint, params)
        return records[0] if records else {}

    def _try_get_first_record(self, endpoint: str, params: dict[str, object]) -> dict:
        try:
            return self._get_first_record(endpoint, params)
        except Exception:
            return {}

    def _backfill_eps_from_earnings(
        self,
        symbol: str,
        periods: list[FinancialPeriod],
        limit: int = 12,
    ) -> None:
        if not periods:
            return

        execution = self.get_earnings_execution(symbol, limit=limit)
        events = [
            (
                _safe_date(event.get("date")),
                _safe_number(event.get("reported_eps")),
            )
            for event in execution.recent_events
        ]

        for period in periods:
            if period.period_end is None or period.diluted_eps is not None:
                continue

            matched_eps = next(
                (
                    reported_eps
                    for event_date, reported_eps in events
                    if event_date is not None
                    and reported_eps is not None
                    and 0 <= (event_date - period.period_end).days <= 60
                ),
                None,
            )
            if matched_eps is not None:
                period.diluted_eps = matched_eps

    def _get_records(self, endpoint: str, params: dict[str, object]) -> list[dict]:
        self._log(
            f"FMP 实时请求: {endpoint} {self._describe_params(params)}。",
            "progress",
        )

        try:
            response = httpx.get(
                f"{self.base_url}/{endpoint}",
                params={**params, "apikey": self.api_key},
                timeout=20.0,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            self._log(
                f"FMP realtime fetch failed: {endpoint} {self._describe_params(params)} -> {exc.__class__.__name__}: {exc}",
                "warning",
            )
            raise
        except Exception as exc:
            self._log(
                f"FMP realtime fetch failed: {endpoint} {self._describe_params(params)} -> {exc.__class__.__name__}: {exc}",
                "warning",
            )
            raise
        records = self._normalize_records_payload(payload)
        if not records:
            self._log(
                f"FMP realtime response: {endpoint} {self._describe_params(params)} 未解析出结构化记录，将回退到其他数据源。",
                "warning",
            )
        return records

    def _describe_params(self, params: dict[str, object]) -> str:
        symbol = params.get("symbol")
        period = params.get("period")
        limit = params.get("limit")
        parts = [str(symbol).upper()] if symbol else []
        if period:
            parts.append(f"period={period}")
        if limit:
            parts.append(f"limit={limit}")
        return " ".join(parts).strip()

    def _log(self, message: str, level: str = "progress") -> None:
        if self.progress_callback is not None:
            self.progress_callback(message, level)

    @staticmethod
    def _find_price_date_on_or_before(target_date: date, sorted_price_dates: list[date]) -> date | None:
        if not sorted_price_dates:
            return None
        insertion_index = bisect_right(sorted_price_dates, target_date) - 1
        if insertion_index < 0:
            return None
        candidate_date = sorted_price_dates[insertion_index]
        if (target_date - candidate_date).days > 7:
            return None
        return candidate_date

    @staticmethod
    def _find_nearest_ratio_record(
        target_date: date,
        ratios_by_date: dict[date, dict],
    ) -> dict:
        if not ratios_by_date:
            return {}

        nearest_date = min(
            ratios_by_date.keys(),
            key=lambda ratio_date: abs((target_date - ratio_date).days),
        )
        if abs((target_date - nearest_date).days) > 120:
            return {}
        return ratios_by_date.get(nearest_date, {})

    @staticmethod
    def _find_nearest_eps(
        target_date: date,
        eps_by_date: dict[date, float],
    ) -> float | None:
        if not eps_by_date:
            return None

        nearest_date = min(
            eps_by_date.keys(),
            key=lambda eps_date: abs((target_date - eps_date).days),
        )
        if abs((target_date - nearest_date).days) > 45:
            return None
        return eps_by_date.get(nearest_date)

    def _has_eps_scale_mismatch(
        self,
        quarter_records: list[tuple[date, float, float | None, float | None]],
        eps_by_date: dict[date, float],
    ) -> bool:
        ratios: list[float] = []
        for period_end, income_statement_eps, _, _ in quarter_records:
            earnings_eps = self._find_nearest_eps(period_end, eps_by_date)
            if (
                earnings_eps is None
                or income_statement_eps is None
                or earnings_eps == 0
                or income_statement_eps == 0
            ):
                continue
            ratios.append(abs(earnings_eps / income_statement_eps))

        if len(ratios) < 3:
            return False

        median_ratio = self._median(ratios)
        return median_ratio is not None and (median_ratio < 0.5 or median_ratio > 2.0)

    @staticmethod
    def _median(values: list[float]) -> float | None:
        if not values:
            return None
        sorted_values = sorted(values)
        middle = len(sorted_values) // 2
        if len(sorted_values) % 2 == 1:
            return sorted_values[middle]
        return (sorted_values[middle - 1] + sorted_values[middle]) / 2

    @staticmethod
    def _percentile(values: list[float], current_value: float | None) -> float | None:
        if current_value is None or not values:
            return None
        less_or_equal_count = sum(1 for value in values if value <= current_value)
        return less_or_equal_count / len(values)

    @staticmethod
    def _rolling_average(values: list[float], window: int) -> float | None:
        if len(values) < window:
            return None
        recent_values = values[-window:]
        return sum(recent_values) / len(recent_values)

    @staticmethod
    def _normalize_records_payload(payload: object) -> list[dict]:
        if isinstance(payload, list):
            return [record for record in payload if isinstance(record, dict)]

        if isinstance(payload, dict):
            for key in (
                "data",
                "results",
                "financials",
                "estimates",
                "earnings",
                "analystEstimates",
            ):
                value = payload.get(key)
                if isinstance(value, list):
                    return [record for record in value if isinstance(record, dict)]

            if any(
                field in payload
                for field in (
                    "date",
                    "symbol",
                    "revenue",
                    "totalRevenue",
                    "netIncome",
                    "eps",
                    "epsDiluted",
                    "epsdiluted",
                    "peRatioTTM",
                    "priceEarningsRatioTTM",
                    "netIncomePerShareTTM",
                    "enterpriseValueOverEBITDATTM",
                    "enterpriseValueMultipleTTM",
                )
            ):
                return [payload]

        return []

    @staticmethod
    def _pick_value(record: dict | None, keys: list[str]) -> float | None:
        if not isinstance(record, dict):
            return None

        lowered = {str(key).lower(): value for key, value in record.items()}
        for key in keys:
            if key in record:
                value = _safe_number(record.get(key))
                if value is not None:
                    return value

            value = _safe_number(lowered.get(key.lower()))
            if value is not None:
                return value
        return None

    @classmethod
    def _pick_first_value(cls, candidates: list[tuple[dict | None, list[str]]]) -> float | None:
        for record, keys in candidates:
            value = cls._pick_value(record, keys)
            if value is not None:
                return value
        return None

    @staticmethod
    def _pick_text(record: dict | None, keys: list[str]) -> str | None:
        if not isinstance(record, dict):
            return None

        lowered = {str(key).lower(): value for key, value in record.items()}
        for key in keys:
            value = record.get(key)
            if value is None:
                value = lowered.get(key.lower())
            if value is None:
                continue
            text = str(value).strip()
            if text and text.lower() not in {"none", "null", "nan", "-"}:
                return text
        return None

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
