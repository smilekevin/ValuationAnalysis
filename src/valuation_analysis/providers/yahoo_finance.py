from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from html import unescape
from io import StringIO
import re
from statistics import mean

import pandas as pd
import yfinance as yf

from valuation_analysis.models import (
    CompanyProfile,
    EarningsExecutionMetrics,
    FinancialPeriod,
    ForecastSnapshot,
    MarketSnapshot,
)
from valuation_analysis.providers.base import MarketDataProvider
from valuation_analysis.providers.sec_companyfacts import SecCompanyFactsProvider


def _safe_number(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_date(value: object) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _safe_table(getter: Callable[[], object]) -> pd.DataFrame:
    try:
        table = getter()
    except Exception:
        return pd.DataFrame()
    if table is None or table.empty:
        return pd.DataFrame()
    return table.copy()


def _extract_table_value(
    table: pd.DataFrame,
    periods: list[str],
    columns: list[str],
) -> float | None:
    if table is None or table.empty:
        return None

    normalized_index = {str(index).strip().lower(): index for index in table.index}
    normalized_columns = {str(column).strip().lower(): column for column in table.columns}

    for period in periods:
        index_key = normalized_index.get(period.strip().lower())
        if index_key is None:
            continue
        row = table.loc[index_key]
        for column in columns:
            column_key = normalized_columns.get(column.strip().lower())
            if column_key is None:
                continue
            value = _safe_number(row.get(column_key))
            if value is not None:
                return value
    return None


def _rolling_average(series: pd.Series, window: int) -> float | None:
    if series is None or series.empty:
        return None
    cleaned = series.dropna()
    if cleaned.empty:
        return None
    sample = cleaned.tail(window)
    if sample.empty:
        return None
    return _safe_number(sample.mean())


def _growth_rate(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline is None or baseline == 0:
        return None
    return (current - baseline) / abs(baseline)


def _normalize_label(value: str) -> str:
    return (
        value.lower()
        .replace("&", "and")
        .replace(" ", "")
        .replace("-", "")
        .replace("/", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
    )


def _extract_statement_value(
    statement: pd.DataFrame,
    column: object,
    labels: list[str],
) -> float | None:
    if statement is None or statement.empty:
        return None

    normalized_index = {
        _normalize_label(str(index)): index
        for index in statement.index
    }
    for label in labels:
        index_key = normalized_index.get(_normalize_label(label))
        if index_key is None:
            continue
        value = _safe_number(statement.at[index_key, column])
        if value is not None:
            return value
    return None


def _parse_report_value(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "-"}:
        return None

    negative = text.startswith("(") and text.endswith(")")
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None

    try:
        numeric = float(cleaned)
    except ValueError:
        return None
    return -numeric if negative else numeric


def _parse_human_date(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def _unit_multiplier(unit: str | None) -> float:
    normalized = (unit or "").strip().lower()
    if normalized in {"billion", "billions", "b"}:
        return 1_000_000_000
    if normalized in {"million", "millions", "m"}:
        return 1_000_000
    if normalized in {"thousand", "thousands", "k"}:
        return 1_000
    return 1.0


def _flatten_columns(table: pd.DataFrame) -> pd.DataFrame:
    normalized = table.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = [
            " ".join(str(part).strip() for part in column if str(part).strip() and str(part).strip().lower() != "nan")
            for column in normalized.columns
        ]
    else:
        normalized.columns = [str(column).strip() for column in normalized.columns]
    return normalized


def _extract_metric_from_text(text: str, patterns: list[str]) -> tuple[float | None, str | None]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        value = _parse_report_value(match.group("value"))
        if value is None:
            continue
        return value, match.groupdict().get("unit")
    return None, None


def _core_metric_count(period: FinancialPeriod) -> int:
    return sum(
        value is not None
        for value in (period.revenue, period.net_income, period.diluted_eps)
    )


def _metric_magnitude_score(period: FinancialPeriod) -> float:
    score = 0.0
    for value in (period.revenue, period.net_income, period.diluted_eps):
        if value is None:
            continue
        score += abs(value)
    return score


class YahooFinanceProvider(MarketDataProvider):
    PERIOD_END_PATTERNS = [
        r"(?:quarter|three months)\s+ended\s+(?P<period_end>[A-Z][a-z]+ \d{1,2}, \d{4})",
        r"ended\s+(?P<period_end>[A-Z][a-z]+ \d{1,2}, \d{4})",
    ]
    REVENUE_PATTERNS = [
        r"(?:net|total)?\s*revenue\s+of\s+\$?(?P<value>[\d,]+(?:\.\d+)?)\s*(?P<unit>billion|million|thousand|b|m|k)?",
        r"revenue\s+was\s+\$?(?P<value>[\d,]+(?:\.\d+)?)\s*(?P<unit>billion|million|thousand|b|m|k)?",
    ]
    NET_INCOME_PATTERNS = [
        r"GAAP\s+net\s+income\s+of\s+\$?(?P<value>[\d,]+(?:\.\d+)?)\s*(?P<unit>billion|million|thousand|b|m|k)?",
        r"net\s+income\s+of\s+\$?(?P<value>[\d,]+(?:\.\d+)?)\s*(?P<unit>billion|million|thousand|b|m|k)?",
        r"GAAP\s+profit\s+of\s+\$?(?P<value>[\d,]+(?:\.\d+)?)\s*(?P<unit>billion|million|thousand|b|m|k)?",
    ]
    DILUTED_EPS_PATTERNS = [
        r"GAAP\s+(?:diluted\s+EPS|diluted\s+earnings\s+per\s+share)\s+of\s+\$?(?P<value>[\d,]+(?:\.\d+)?)",
        r"diluted\s+(?:EPS|earnings\s+per\s+share)\s+of\s+\$?(?P<value>[\d,]+(?:\.\d+)?)",
    ]

    def __init__(self) -> None:
        self.sec_provider = SecCompanyFactsProvider()

    def get_company_profile(self, symbol: str) -> CompanyProfile:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        try:
            sec_metadata = self.sec_provider.get_company_metadata(symbol)
        except Exception:
            sec_metadata = {}
        return CompanyProfile(
            symbol=symbol.upper(),
            name=info.get("longName") or info.get("shortName") or sec_metadata.get("name"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            sic_code=sec_metadata.get("sic_code"),
            sic_description=sec_metadata.get("sic_description"),
            country=info.get("country"),
            website=info.get("website"),
            market_cap=_safe_number(info.get("marketCap")),
            currency=info.get("currency"),
        )

    def get_company_profile_for_peer(
        self,
        symbol: str,
        expected_sic_code: str | None = None,
        expected_sic_description: str | None = None,
    ) -> CompanyProfile:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        try:
            sec_metadata = self.sec_provider.get_company_metadata(symbol)
        except Exception:
            sec_metadata = {}
        return CompanyProfile(
            symbol=symbol.upper(),
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            sic_code=sec_metadata.get("sic_code") or expected_sic_code,
            sic_description=sec_metadata.get("sic_description") or expected_sic_description,
            country=info.get("country"),
            website=info.get("website"),
            market_cap=_safe_number(info.get("marketCap")),
            currency=info.get("currency"),
        )

    def get_peer_candidate_symbols(
        self,
        target_symbol: str,
        profile: CompanyProfile,
        limit: int = 250,
    ) -> list[str]:
        if not profile.sic_code:
            return []

        try:
            return self.sec_provider.get_symbols_by_sic(
                profile.sic_code,
                exclude_symbol=target_symbol.upper(),
                limit=limit,
            )
        except Exception:
            return []

    def get_peer_candidate_source_label(self, profile: CompanyProfile) -> str:
        if profile.sic_code:
            return "SEC 同 SIC 候选池"
        return "本地股票池候选"

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        fast_info = getattr(ticker, "fast_info", {}) or {}
        history = _safe_table(lambda: ticker.history(period="3mo", interval="1d", auto_adjust=False))
        close_prices = history["Close"] if "Close" in history.columns else pd.Series(dtype=float)
        return MarketSnapshot(
            price=_safe_number(fast_info.get("lastPrice") or info.get("currentPrice")),
            previous_close=_safe_number(info.get("previousClose")),
            average_price_5d=_rolling_average(close_prices, 5),
            average_price_30d=_rolling_average(close_prices, 30),
            trailing_pe=_safe_number(info.get("trailingPE")),
            forward_pe=_safe_number(info.get("forwardPE")),
            trailing_eps=_safe_number(info.get("trailingEps")),
            forward_eps=_safe_number(info.get("forwardEps")),
            peg_ratio=_safe_number(info.get("pegRatio")),
            price_to_sales=_safe_number(info.get("priceToSalesTrailing12Months")),
            enterprise_to_ebitda=_safe_number(info.get("enterpriseToEbitda")),
            fifty_two_week_low=_safe_number(
                fast_info.get("yearLow") or info.get("fiftyTwoWeekLow")
            ),
            fifty_two_week_high=_safe_number(
                fast_info.get("yearHigh") or info.get("fiftyTwoWeekHigh")
            ),
        )

    def get_financial_history(self, symbol: str, limit: int = 8) -> list[FinancialPeriod]:
        history_limit = max(limit + 6, 12)
        try:
            periods = self.sec_provider.get_financial_history(symbol, limit=history_limit)
        except Exception:
            periods = []

        release_periods = self._get_reported_quarters_from_releases(symbol, limit=history_limit)
        if release_periods:
            periods = self._merge_statement_periods(periods, release_periods)

        latest_reported_period = self._get_latest_reported_quarter_from_statement(symbol)
        if latest_reported_period is not None:
            periods = self._merge_latest_reported_period(periods, latest_reported_period)

        statement_periods = self._get_reported_quarters_from_statement(symbol)
        if statement_periods:
            periods = self._merge_statement_periods(periods, statement_periods)

        periods = self._coalesce_nearby_periods(periods)
        self._backfill_eps_from_earnings_dates(symbol, periods, limit=history_limit)
        periods = periods[:limit]
        self._apply_growth_rates(periods)
        return periods

    def _get_reported_quarters_from_releases(self, symbol: str, limit: int = 8) -> list[FinancialPeriod]:
        releases = self.sec_provider.get_recent_earnings_release_htmls(symbol, limit=limit)
        periods: list[FinancialPeriod] = []

        for release in releases:
            report_date = release.get("report_date")
            html = release.get("html")
            if not isinstance(report_date, date) or not isinstance(html, str):
                continue

            period_from_text = self._parse_release_text(symbol, html)
            if period_from_text is not None:
                periods.append(period_from_text)
                continue

            try:
                tables = pd.read_html(StringIO(html), displayed_only=False)
            except Exception:
                continue

            for table in tables:
                period = self._parse_release_table(table, report_date)
                if period is not None:
                    periods.append(period)
                    break

        return self._coalesce_nearby_periods(periods)

    def _parse_release_text(self, symbol: str, html: str) -> FinancialPeriod | None:
        text = unescape(re.sub(r"<[^>]+>", " ", html))
        text = re.sub(r"\s+", " ", text)

        period_end = None
        for pattern in self.PERIOD_END_PATTERNS:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            period_end = _parse_human_date(match.group("period_end"))
            if period_end is not None:
                break

        revenue, revenue_unit = _extract_metric_from_text(text, self.REVENUE_PATTERNS)
        net_income, net_income_unit = _extract_metric_from_text(text, self.NET_INCOME_PATTERNS)
        diluted_eps, _ = _extract_metric_from_text(text, self.DILUTED_EPS_PATTERNS)

        if revenue is not None:
            revenue *= _unit_multiplier(revenue_unit)
        if net_income is not None:
            net_income *= _unit_multiplier(net_income_unit)

        if period_end is None:
            return None
        if revenue is None and net_income is None and diluted_eps is None:
            return None

        return FinancialPeriod(
            period_end=period_end,
            revenue=revenue,
            net_income=net_income,
            diluted_eps=diluted_eps,
        )

    def _get_latest_reported_quarter_from_statement(self, symbol: str) -> FinancialPeriod | None:
        periods = self._get_reported_quarters_from_statement(symbol)
        return periods[0] if periods else None

    def _get_reported_quarters_from_statement(self, symbol: str) -> list[FinancialPeriod]:
        statement = _safe_table(lambda: yf.Ticker(symbol).quarterly_income_stmt)
        if statement.empty:
            statement = _safe_table(lambda: yf.Ticker(symbol).quarterly_financials)
        if statement.empty:
            return []

        dated_columns = sorted(
            [
                column
                for column in statement.columns
                if _safe_date(column) is not None
            ],
            key=lambda column: _safe_date(column) or date.min,
            reverse=True,
        )
        periods: list[FinancialPeriod] = []
        for statement_period_end in dated_columns:
            period_end = _safe_date(statement_period_end)
            if period_end is None:
                continue

            period = FinancialPeriod(
                period_end=period_end,
                revenue=_extract_statement_value(
                    statement,
                    statement_period_end,
                    [
                        "Total Revenue",
                        "Revenue",
                        "Net Revenue",
                        "Operating Revenue",
                    ],
                ),
                net_income=_extract_statement_value(
                    statement,
                    statement_period_end,
                    [
                        "Net Income",
                        "Net Income Common Stockholders",
                        "Net Income Including Noncontrolling Interests",
                        "Net Income Available To Common Stockholders",
                        "Net Income Available To Common Shares",
                        "Profit Loss",
                    ],
                ),
                diluted_eps=_extract_statement_value(
                    statement,
                    statement_period_end,
                    [
                        "Diluted EPS",
                        "Basic And Diluted EPS",
                        "Diluted Earnings Per Share",
                        "Basic And Diluted Earnings Per Share",
                        "Earnings Per Share Diluted",
                        "Earnings Per Share Basic And Diluted",
                    ],
                ),
            )
            if period.revenue is None and period.net_income is None and period.diluted_eps is None:
                continue
            periods.append(period)
        return periods

    def _parse_release_table(self, table: pd.DataFrame, report_date: date) -> FinancialPeriod | None:
        normalized = _flatten_columns(table)
        if normalized.empty or normalized.shape[1] < 2:
            return None

        period_end = self._extract_period_end_from_release_table(normalized, report_date)
        if period_end is None:
            return None

        row_label_column = normalized.columns[0]
        row_labels = normalized[row_label_column].astype(str).map(_normalize_label)

        def find_row(candidates: list[str]) -> int | None:
            normalized_candidates = {_normalize_label(label) for label in candidates}
            for index, row_label in enumerate(row_labels):
                if row_label in normalized_candidates:
                    return index
            return None

        revenue_row = find_row(["Net revenue", "Revenue", "Total net revenue"])
        net_income_row = find_row(["Net income"])
        if net_income_row is None:
            net_income_row = find_row(
                [
                    "Net income attributable to common stockholders",
                    "Net income available to common stockholders",
                    "Profit",
                ]
            )
        diluted_eps_row = find_row(
            [
                "Earnings per common share - diluted",
                "Earnings per share - diluted",
                "Diluted earnings per share",
                "Diluted",
                "Diluted EPS",
                "Basic and diluted",
            ]
        )

        if revenue_row is None and net_income_row is None and diluted_eps_row is None:
            return None

        value_column_index = self._find_primary_value_column(normalized)
        if value_column_index is None:
            return None

        multiplier = self._table_value_multiplier(normalized)

        def value_at(row_index: int | None) -> float | None:
            if row_index is None:
                return None
            value = _parse_report_value(normalized.iat[row_index, value_column_index])
            if value is None:
                return None
            return value * multiplier

        period = FinancialPeriod(
            period_end=period_end,
            revenue=value_at(revenue_row),
            net_income=value_at(net_income_row),
            diluted_eps=value_at(diluted_eps_row),
        )
        if period.revenue is None and period.net_income is None and period.diluted_eps is None:
            return None
        return period

    def _extract_period_end_from_release_table(
        self,
        table: pd.DataFrame,
        report_date: date,
    ) -> date | None:
        flattened_text = " ".join(str(value) for value in table.columns)
        flattened_text += " " + " ".join(str(value) for value in table.head(8).to_numpy().flatten())
        normalized_text = re.sub(r"\s+", " ", flattened_text)
        candidates: list[date] = []

        for pattern in self.PERIOD_END_PATTERNS:
            for match in re.finditer(pattern, normalized_text, re.IGNORECASE):
                parsed = _parse_human_date(match.group("period_end"))
                if parsed is not None:
                    candidates.append(parsed)

        for match in re.finditer(r"[A-Z][a-z]+ \d{1,2}, \d{4}", normalized_text):
            parsed = _parse_human_date(match.group(0))
            if parsed is not None:
                candidates.append(parsed)

        if not candidates:
            return None

        preferred_candidates = [
            candidate
            for candidate in candidates
            if candidate < report_date and 5 <= (report_date - candidate).days <= 120
        ]
        if preferred_candidates:
            return max(preferred_candidates)

        return max(candidates)

    @staticmethod
    def _find_primary_value_column(table: pd.DataFrame) -> int | None:
        for column_index in range(1, table.shape[1]):
            series = table.iloc[:, column_index]
            numeric_hits = sum(_parse_report_value(value) is not None for value in series.head(12))
            if numeric_hits >= 2:
                return column_index
        return None

    @staticmethod
    def _table_value_multiplier(table: pd.DataFrame) -> float:
        flattened_text = " ".join(
            str(value)
            for value in table.columns
        ) + " " + " ".join(
            str(value)
            for value in table.head(5).to_numpy().flatten()
        )
        lowered = flattened_text.lower()
        if "in billions" in lowered:
            return 1_000_000_000
        if "in millions" in lowered:
            return 1_000_000
        if "in thousands" in lowered:
            return 1_000
        return 1.0

    @staticmethod
    def _merge_latest_reported_period(
        periods: list[FinancialPeriod],
        latest_period: FinancialPeriod,
    ) -> list[FinancialPeriod]:
        if latest_period.period_end is None:
            return periods

        for existing_period in periods:
            if existing_period.period_end is None:
                continue
            if abs((existing_period.period_end - latest_period.period_end).days) <= 10:
                return periods

        merged_periods = [latest_period, *periods]
        merged_periods.sort(
            key=lambda period: period.period_end or date.min,
            reverse=True,
        )
        return merged_periods

    @staticmethod
    def _merge_statement_periods(
        periods: list[FinancialPeriod],
        statement_periods: list[FinancialPeriod],
    ) -> list[FinancialPeriod]:
        merged_periods = list(periods)

        for statement_period in statement_periods:
            if statement_period.period_end is None:
                continue

            matched_period = next(
                (
                    existing_period
                    for existing_period in merged_periods
                    if existing_period.period_end is not None
                    and abs((existing_period.period_end - statement_period.period_end).days) <= 10
                ),
                None,
            )

            if matched_period is None:
                merged_periods.append(statement_period)
                continue

            if matched_period.revenue is None:
                matched_period.revenue = statement_period.revenue
            if matched_period.net_income is None:
                matched_period.net_income = statement_period.net_income
            if matched_period.diluted_eps is None:
                matched_period.diluted_eps = statement_period.diluted_eps

        return YahooFinanceProvider._coalesce_nearby_periods(merged_periods)

    @staticmethod
    def _coalesce_nearby_periods(
        periods: list[FinancialPeriod],
        tolerance_days: int = 25,
    ) -> list[FinancialPeriod]:
        merged_periods: list[FinancialPeriod] = []

        for period in sorted(
            periods,
            key=lambda item: item.period_end or date.min,
            reverse=True,
        ):
            if period.period_end is None:
                continue

            matched_index = next(
                (
                    index
                    for index, existing_period in enumerate(merged_periods)
                    if existing_period.period_end is not None
                    and abs((existing_period.period_end - period.period_end).days) <= tolerance_days
                ),
                None,
            )

            if matched_index is None:
                merged_periods.append(period.model_copy(deep=True))
                continue

            existing_period = merged_periods[matched_index]
            preferred_period, fallback_period = YahooFinanceProvider._pick_preferred_period(
                existing_period,
                period,
            )
            merged_period = preferred_period.model_copy(deep=True)

            if merged_period.revenue is None:
                merged_period.revenue = fallback_period.revenue
            if merged_period.net_income is None:
                merged_period.net_income = fallback_period.net_income
            if merged_period.diluted_eps is None:
                merged_period.diluted_eps = fallback_period.diluted_eps

            merged_periods[matched_index] = merged_period

        merged_periods.sort(
            key=lambda item: item.period_end or date.min,
            reverse=True,
        )
        return merged_periods

    @staticmethod
    def _pick_preferred_period(
        left: FinancialPeriod,
        right: FinancialPeriod,
    ) -> tuple[FinancialPeriod, FinancialPeriod]:
        left_score = (
            _core_metric_count(left),
            _metric_magnitude_score(left),
            left.period_end or date.min,
        )
        right_score = (
            _core_metric_count(right),
            _metric_magnitude_score(right),
            right.period_end or date.min,
        )
        if right_score > left_score:
            return right, left
        return left, right

    def _backfill_eps_from_earnings_dates(
        self,
        symbol: str,
        periods: list[FinancialPeriod],
        limit: int = 12,
    ) -> None:
        if not periods:
            return

        try:
            earnings_dates = getattr(yf.Ticker(symbol), "earnings_dates", None)
        except Exception:
            return

        if earnings_dates is None or earnings_dates.empty:
            return

        recent_events = earnings_dates.sort_index(ascending=False).head(limit)
        available_events: list[tuple[date, float]] = []
        for event_date, row in recent_events.iterrows():
            parsed_event_date = _safe_date(event_date)
            reported_eps = _safe_number(row.get("Reported EPS"))
            if parsed_event_date is None or reported_eps is None:
                continue
            available_events.append((parsed_event_date, reported_eps))

        for period in periods:
            if period.period_end is None or period.diluted_eps is not None:
                continue

            matched_eps = next(
                (
                    reported_eps
                    for event_date, reported_eps in available_events
                    if 0 <= (event_date - period.period_end).days <= 60
                ),
                None,
            )
            if matched_eps is not None:
                period.diluted_eps = matched_eps

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

    def get_forecast(self, symbol: str) -> ForecastSnapshot:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}

        earnings_estimate = _safe_table(lambda: ticker.earnings_estimate)
        revenue_estimate = _safe_table(lambda: ticker.revenue_estimate)
        growth_estimates = _safe_table(lambda: ticker.growth_estimates)

        current_year_eps = None
        next_year_eps = None
        current_year_revenue = None
        next_year_revenue = None
        earnings_growth = None
        revenue_growth = None

        current_year_eps = _extract_table_value(
            earnings_estimate,
            periods=["0y", "current year", "current"],
            columns=["avg", "current"],
        )
        next_year_eps = _extract_table_value(
            earnings_estimate,
            periods=["+1y", "next year"],
            columns=["avg", "current"],
        )

        current_year_revenue = _extract_table_value(
            revenue_estimate,
            periods=["0y", "current year", "current"],
            columns=["avg", "current"],
        )
        next_year_revenue = _extract_table_value(
            revenue_estimate,
            periods=["+1y", "next year"],
            columns=["avg", "current"],
        )

        earnings_growth = _safe_number(info.get("earningsGrowth"))
        revenue_growth = _safe_number(info.get("revenueGrowth"))

        if growth_estimates is not None and not growth_estimates.empty:
            growth_from_table = _extract_table_value(
                growth_estimates,
                periods=["+1y", "0y", "+5y"],
                columns=["stockTrend", "stock", "growth"],
            )
            earnings_growth = earnings_growth if earnings_growth is not None else growth_from_table

        if (
            earnings_growth is None
            and current_year_eps is not None
            and next_year_eps is not None
            and current_year_eps != 0
        ):
            earnings_growth = (next_year_eps - current_year_eps) / abs(current_year_eps)

        if (
            revenue_growth is None
            and current_year_revenue is not None
            and next_year_revenue is not None
            and current_year_revenue != 0
        ):
            revenue_growth = (next_year_revenue - current_year_revenue) / abs(current_year_revenue)

        if current_year_eps is None:
            current_year_eps = _safe_number(info.get("trailingEps"))
        if next_year_eps is None:
            next_year_eps = _safe_number(info.get("forwardEps"))

        return ForecastSnapshot(
            current_year_eps=current_year_eps,
            next_year_eps=next_year_eps,
            current_year_revenue=current_year_revenue,
            next_year_revenue=next_year_revenue,
            earnings_growth=earnings_growth,
            revenue_growth=revenue_growth,
        )

    def get_earnings_execution(self, symbol: str, limit: int = 10) -> EarningsExecutionMetrics:
        ticker = yf.Ticker(symbol)
        events = getattr(ticker, "earnings_dates", None)

        if events is None or events.empty:
            return EarningsExecutionMetrics()

        rows = []
        surprises: list[float] = []
        beat_count = 0
        miss_count = 0
        meet_count = 0

        recent = events.sort_index(ascending=False).head(limit)
        for event_date, row in recent.iterrows():
            estimate = _safe_number(row.get("EPS Estimate"))
            reported = _safe_number(row.get("Reported EPS"))
            surprise_pct = _safe_number(row.get("Surprise(%)"))
            if estimate is None or reported is None:
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
                    "date": _safe_date(event_date),
                    "eps_estimate": estimate,
                    "reported_eps": reported,
                    "reported_eps_label": "实际 EPS（Surprise口径）",
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
