from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime
from functools import lru_cache
from io import BytesIO
import json
from pathlib import Path
import re
import time
import zipfile

import httpx

from valuation_analysis.config import settings
from valuation_analysis.models import FinancialPeriod

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
BULK_SUBMISSIONS_ZIP_URL = "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
BULK_INDEX_DOWNLOAD_TIMEOUT_SECONDS = 12.0
SEC_FILING_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession_no_dashes}/{accession_number}-index.htm"

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
]
NET_INCOME_TAGS = [
    "NetIncomeLoss",
    "ProfitLoss",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
    "NetIncomeLossAvailableToCommonStockholdersDiluted",
    "NetIncomeLossAttributableToParent",
]
DILUTED_EPS_TAGS = [
    "EarningsPerShareDiluted",
    "EarningsPerShareBasicAndDiluted",
    "IncomeLossFromContinuingOperationsPerDilutedShare",
    "IncomeLossFromContinuingOperationsPerBasicAndDilutedShare",
    "NetIncomeLossAvailableToCommonStockholdersDilutedPerShare",
]


def _parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _quarter_duration_days(start: date | None, end: date | None) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days + 1


def _is_quarter_fact(item: dict) -> bool:
    start = _parse_iso_date(item.get("start"))
    end = _parse_iso_date(item.get("end"))
    duration = _quarter_duration_days(start, end)
    if duration is None:
        return False
    return 75 <= duration <= 105


def _fact_sort_key(item: dict) -> tuple[date, date]:
    end = _parse_iso_date(item.get("end")) or date.min
    filed = _parse_iso_date(item.get("filed")) or date.min
    return (end, filed)


class SecCompanyFactsProvider:
    def __init__(self) -> None:
        self.headers = {
            "User-Agent": settings.sec_api_user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        self.cache_path = (
            Path(__file__).resolve().parents[3] / ".cache" / "sec_sic_index.json"
        )
        self._bulk_sic_index: dict[str, list[str]] | None = None

    def get_financial_history(self, symbol: str, limit: int = 8) -> list[FinancialPeriod]:
        facts = self._get_company_facts(symbol)
        if not facts:
            return []

        revenue = self._extract_best_series(facts, "us-gaap", REVENUE_TAGS, "USD")
        net_income = self._extract_best_series(facts, "us-gaap", NET_INCOME_TAGS, "USD")
        diluted_eps = self._extract_best_series(facts, "us-gaap", DILUTED_EPS_TAGS, "USD/shares")

        dates = sorted(
            set(revenue.keys()) | set(net_income.keys()) | set(diluted_eps.keys()),
            reverse=True,
        )[:limit]

        periods: list[FinancialPeriod] = []
        for period_end in dates:
            periods.append(
                FinancialPeriod(
                    period_end=period_end,
                    revenue=revenue.get(period_end),
                    net_income=net_income.get(period_end),
                    diluted_eps=diluted_eps.get(period_end),
                )
            )
        return periods

    def get_company_metadata(self, symbol: str) -> dict[str, str | None]:
        submissions = self._get_company_submissions(symbol)
        if not submissions:
            return {}

        sic_code = submissions.get("sic")
        sic_description = submissions.get("sicDescription")
        entity_name = submissions.get("name")

        return {
            "name": str(entity_name).strip() if entity_name else None,
            "sic_code": str(sic_code).strip() if sic_code is not None else None,
            "sic_description": str(sic_description).strip() if sic_description else None,
        }

    def get_latest_reported_period_end(self, symbol: str) -> date | None:
        submissions = self._get_company_submissions(symbol)
        recent = submissions.get("filings", {}).get("recent", {}) if submissions else {}
        forms = recent.get("form", [])
        report_dates = recent.get("reportDate", [])
        filing_dates = recent.get("filingDate", [])

        candidates: list[tuple[date, date]] = []
        for form, report_date, filing_date in zip(forms, report_dates, filing_dates, strict=False):
            if form not in {"8-K", "10-Q", "10-K", "6-K"}:
                continue
            parsed_report_date = _parse_iso_date(report_date)
            if parsed_report_date is None:
                continue
            parsed_filing_date = _parse_iso_date(filing_date) or date.min
            candidates.append((parsed_report_date, parsed_filing_date))

        if not candidates:
            return None

        candidates.sort(reverse=True)
        return candidates[0][0]

    def get_latest_earnings_release_html(self, symbol: str) -> tuple[date | None, str | None]:
        releases = self.get_recent_earnings_release_htmls(symbol, limit=1)
        if not releases:
            return None, None
        release = releases[0]
        return release["report_date"], release["html"]

    def get_recent_earnings_release_htmls(
        self,
        symbol: str,
        limit: int = 8,
    ) -> list[dict[str, date | str]]:
        submissions = self._get_company_submissions(symbol)
        recent = submissions.get("filings", {}).get("recent", {}) if submissions else {}
        forms = recent.get("form", [])
        report_dates = recent.get("reportDate", [])
        accession_numbers = recent.get("accessionNumber", [])

        cik = self._ticker_to_cik_map().get(symbol.upper())
        if not cik:
            return []

        releases: list[dict[str, date | str]] = []

        for form, report_date, accession_number in zip(
            forms,
            report_dates,
            accession_numbers,
            strict=False,
        ):
            if form not in {"8-K", "6-K"}:
                continue
            parsed_report_date = _parse_iso_date(report_date)
            if parsed_report_date is None or not accession_number:
                continue

            exhibit_url = self._find_exhibit_99_url(cik, str(accession_number))
            if not exhibit_url:
                continue

            response = httpx.get(exhibit_url, headers=self.headers, timeout=20.0)
            response.raise_for_status()
            releases.append(
                {
                    "report_date": parsed_report_date,
                    "html": response.text,
                }
            )
            if len(releases) >= limit:
                break

        return releases

    def get_symbols_by_sic(
        self,
        sic_code: str,
        exclude_symbol: str | None = None,
        limit: int = 250,
    ) -> list[str]:
        if not sic_code:
            return []

        bulk_index = self._get_bulk_sic_index()
        candidates = bulk_index.get(str(sic_code).strip(), [])
        if exclude_symbol:
            normalized_exclude = exclude_symbol.upper()
            candidates = [symbol for symbol in candidates if symbol != normalized_exclude]
        return candidates[:limit]

    def get_bulk_sic_index_path(self) -> Path:
        return self.cache_path

    def refresh_bulk_sic_index(
        self,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, list[str]]:
        fresh_index = self._download_bulk_sic_index(progress_callback=progress_callback)
        self._bulk_sic_index = fresh_index
        self._persist_bulk_sic_index(fresh_index)
        return fresh_index

    def _extract_best_series(
        self,
        facts: dict,
        taxonomy: str,
        tags: Iterable[str],
        unit: str,
    ) -> dict[date, float]:
        merged: dict[date, float] = {}
        for tag in tags:
            series = self._extract_series_for_tag(facts, taxonomy, tag, unit)
            for period_end, value in series.items():
                # Preserve tag priority order: earlier tags in the list win.
                merged.setdefault(period_end, value)
        return merged

    def _extract_series_for_tag(
        self,
        facts: dict,
        taxonomy: str,
        tag: str,
        unit: str,
    ) -> dict[date, float]:
        units = (
            facts.get("facts", {})
            .get(taxonomy, {})
            .get(tag, {})
            .get("units", {})
        )
        rows = units.get(unit, [])
        if not isinstance(rows, list):
            return {}

        deduped: dict[date, dict] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            if not _is_quarter_fact(item):
                continue
            period_end = _parse_iso_date(item.get("end"))
            value = item.get("val")
            if period_end is None or not isinstance(value, (int, float)):
                continue

            existing = deduped.get(period_end)
            if existing is None or _fact_sort_key(item) > _fact_sort_key(existing):
                deduped[period_end] = item

        return {
            period_end: float(item["val"])
            for period_end, item in deduped.items()
        }

    def _get_bulk_sic_index(self) -> dict[str, list[str]]:
        if self._bulk_sic_index is not None:
            return self._bulk_sic_index

        cached_index = self._load_bulk_sic_index_from_cache()
        if cached_index is not None:
            self._bulk_sic_index = cached_index
            return cached_index

        self._bulk_sic_index = {}
        return self._bulk_sic_index

    def _load_bulk_sic_index_from_cache(self) -> dict[str, list[str]] | None:
        if not self.cache_path.exists():
            return None

        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return {
                str(sic_code): [
                    str(symbol).strip().upper()
                    for symbol in symbols
                    if str(symbol).strip()
                ]
                for sic_code, symbols in payload.items()
                if isinstance(symbols, list)
            }
        except Exception:
            return None

    def _persist_bulk_sic_index(self, index: dict[str, list[str]]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(index, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            return

    def _download_bulk_sic_index(
        self,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, list[str]]:
        if progress_callback:
            progress_callback("开始下载 SEC submissions.zip ...")

        content = bytearray()
        with httpx.stream(
            "GET",
            BULK_SUBMISSIONS_ZIP_URL,
            headers=self.headers,
            timeout=BULK_INDEX_DOWNLOAD_TIMEOUT_SECONDS,
        ) as response:
            response.raise_for_status()
            total_bytes = int(response.headers.get("Content-Length") or 0)
            downloaded_bytes = 0
            started_at = time.monotonic()
            last_reported_at = started_at
            last_reported_bytes = 0

            for chunk in response.iter_bytes():
                content.extend(chunk)
                downloaded_bytes += len(chunk)

                if not progress_callback:
                    continue

                now = time.monotonic()
                elapsed = max(now - started_at, 0.001)
                interval = max(now - last_reported_at, 0.001)
                speed_mbps = (downloaded_bytes - last_reported_bytes) / 1024 / 1024 / interval

                should_report = (
                    downloaded_bytes == len(chunk)
                    or now - last_reported_at >= 1.0
                    or (total_bytes > 0 and downloaded_bytes >= total_bytes)
                )
                if should_report:
                    percent = int(downloaded_bytes * 100 / total_bytes) if total_bytes > 0 else 0
                    progress_callback(
                        "下载进度 "
                        f"{percent}% "
                        f"({downloaded_bytes / 1024 / 1024:.1f} / {total_bytes / 1024 / 1024:.1f} MB), "
                        f"实时速度 {speed_mbps:.2f} MB/s, "
                        f"已用 {elapsed:.1f}s"
                    )
                    last_reported_at = now
                    last_reported_bytes = downloaded_bytes

        index: dict[str, set[str]] = {}
        with zipfile.ZipFile(BytesIO(bytes(content))) as archive:
            member_names = [name for name in archive.namelist() if name.endswith(".json")]
            total_members = len(member_names)
            if progress_callback:
                progress_callback(f"下载完成，开始解析 {total_members} 个 SEC submissions JSON 文件 ...")

            for position, member_name in enumerate(member_names, start=1):
                if not member_name.endswith(".json"):
                    continue

                with archive.open(member_name) as handle:
                    try:
                        payload = json.load(handle)
                    except Exception:
                        continue

                sic_code = payload.get("sic")
                tickers = payload.get("tickers")
                if sic_code is None or not isinstance(tickers, list):
                    continue

                symbols = {
                    str(symbol).strip().upper()
                    for symbol in tickers
                    if str(symbol).strip()
                }
                if not symbols:
                    continue

                index.setdefault(str(sic_code).strip(), set()).update(symbols)

                if progress_callback and (
                    position == total_members
                    or position == 1
                    or position % 1000 == 0
                ):
                    progress_callback(
                        f"解析进度 {position}/{total_members}，当前已累计 {len(index)} 个 SIC 分组。"
                    )

        return {
            sic_code: sorted(symbols)
            for sic_code, symbols in index.items()
            if symbols
        }

    def _find_exhibit_99_url(self, cik: str, accession_number: str) -> str | None:
        accession_no_dashes = accession_number.replace("-", "")
        cik_path = str(int(cik))
        index_url = SEC_FILING_INDEX_URL.format(
            cik_path=cik_path,
            accession_no_dashes=accession_no_dashes,
            accession_number=accession_number,
        )

        response = httpx.get(index_url, headers=self.headers, timeout=20.0)
        response.raise_for_status()
        html = response.text

        match = re.search(
            r'<a href="(?P<href>[^"]+?)">[^<]+</a>\s*</td>\s*<td[^>]*>\s*EX-99\.1\s*</td>',
            html,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None

        href = match.group("href")
        if href.startswith("http"):
            return href
        if href.startswith("/"):
            return f"https://www.sec.gov{href}"
        return (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{cik_path}/{accession_no_dashes}/{href}"
        )

    @lru_cache(maxsize=1)
    def _ticker_to_cik_map(self) -> dict[str, str]:
        response = httpx.get(SEC_TICKERS_URL, headers=self.headers, timeout=20.0)
        response.raise_for_status()
        payload = response.json()

        mapping: dict[str, str] = {}
        for item in payload.values():
            ticker = str(item.get("ticker", "")).strip().upper()
            cik = item.get("cik_str")
            if ticker and cik is not None:
                mapping[ticker] = f"{int(cik):010d}"
        return mapping

    @lru_cache(maxsize=512)
    def _get_company_facts(self, symbol: str) -> dict:
        cik = self._ticker_to_cik_map().get(symbol.upper())
        if not cik:
            return {}

        response = httpx.get(
            SEC_COMPANY_FACTS_URL.format(cik=cik),
            headers=self.headers,
            timeout=25.0,
        )
        response.raise_for_status()
        return response.json()

    @lru_cache(maxsize=512)
    def _get_company_submissions(self, symbol: str) -> dict:
        cik = self._ticker_to_cik_map().get(symbol.upper())
        if not cik:
            return {}

        response = httpx.get(
            SEC_SUBMISSIONS_URL.format(cik=cik),
            headers=self.headers,
            timeout=20.0,
        )
        response.raise_for_status()
        return response.json()
