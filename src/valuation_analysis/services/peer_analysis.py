from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
import math
from statistics import median

from valuation_analysis.models import CompanyProfile, ForecastSnapshot, MarketSnapshot, PeerValuation
from valuation_analysis.providers.base import MarketDataProvider
from valuation_analysis.progress import ProgressCallback
from valuation_analysis.services.market_enrichment import (
    enrich_market_with_financial_history,
    enrich_market_with_forecast,
)


class PeerAnalysisService:
    MAX_CANDIDATES_TO_SCORE = 24
    MAX_FINALISTS_TO_COMPARE = 10
    PROFILE_FETCH_WORKERS = 8
    DETAIL_FETCH_WORKERS = 3
    PROFILE_STAGE_TIMEOUT_SECONDS = 12
    DETAIL_STAGE_TIMEOUT_SECONDS = 18

    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    def find_peer_candidates(self, target_symbol: str, profile: CompanyProfile) -> tuple[list[str], str]:
        peer_candidates = self.provider.get_peer_candidate_symbols(target_symbol, profile)
        candidate_source = self.provider.get_peer_candidate_source_label(profile)
        return peer_candidates, candidate_source

    def build_peer_set(
        self,
        target_symbol: str,
        profile: CompanyProfile,
        peer_count: int = 5,
        progress_callback: ProgressCallback | None = None,
    ) -> list[PeerValuation]:
        candidates, candidate_source = self.find_peer_candidates(target_symbol, profile)
        matching_basis = self._matching_basis(profile)
        candidates = self._limit_candidates(candidates, candidate_source)
        scored_candidates: list[tuple[float, CompanyProfile]] = []

        if progress_callback:
            progress_callback(
                f"已从{candidate_source}加载 {len(candidates)} 个候选公司，开始按 {self._matching_label(profile)} 筛选高置信度同行。",
                "progress",
            )

        if not candidates:
            if progress_callback:
                progress_callback(
                f"{candidate_source} 当前未返回可用候选公司，本次不再依赖额外本地候选池。",
                "warning",
            )
            return []

        if progress_callback and candidate_source == "FMP peers 候选池":
            progress_callback(
                "正在使用 FMP peers 候选代码，并逐个查询这些公司的 FMP 行业信息以完成同行匹配。",
                "progress",
            )

        with ThreadPoolExecutor(max_workers=min(self.PROFILE_FETCH_WORKERS, max(len(candidates), 1))) as executor:
            future_to_symbol = {
                executor.submit(
                    self.provider.get_company_profile_for_peer,
                    candidate,
                    profile.sic_code,
                    profile.sic_description,
                ): candidate
                for candidate in candidates
            }
            try:
                completed_futures = as_completed(
                    future_to_symbol,
                    timeout=self.PROFILE_STAGE_TIMEOUT_SECONDS,
                )
                for future in completed_futures:
                    candidate = future_to_symbol[future]
                    try:
                        candidate_profile = future.result()
                    except Exception:
                        continue

                    if progress_callback:
                        progress_callback(
                            f"已完成 {candidate} 的同行适配检查。",
                            "progress",
                        )
                    score = self._candidate_score(profile, candidate_profile)
                    if score > 0:
                        scored_candidates.append((score, candidate_profile))
            except TimeoutError:
                if progress_callback:
                    progress_callback(
                        "同行候选检查已达到时间预算，先使用当前已完成的候选结果继续分析。",
                        "warning",
                    )

        scored_candidates.sort(
            key=lambda item: (
                item[0],
                item[1].market_cap or 0,
                item[1].symbol,
            ),
            reverse=True,
        )

        finalists = scored_candidates[: self.MAX_FINALISTS_TO_COMPARE]
        if progress_callback and finalists:
            progress_callback(
                f"已按行业匹配度收敛到前 {len(finalists)} 家候选公司，最终将优先展示市值最大的同行。",
                "progress",
            )

        threshold_qualified_profiles = [
            candidate_profile
            for score, candidate_profile in finalists
            if score >= self._minimum_accept_score(profile)
        ]

        if not threshold_qualified_profiles:
            threshold_qualified_profiles = [candidate_profile for _, candidate_profile in finalists]

        qualified_profiles = threshold_qualified_profiles

        if candidate_source == "FMP peers 候选池":
            industry_priority_profiles, industry_match_message, industry_match_level = (
                self._filter_fmp_profiles_by_industry(
                    profile,
                    [candidate_profile for _, candidate_profile in finalists],
                )
            )
            qualified_profiles = self._fill_profiles_by_market_cap(
                primary_profiles=industry_priority_profiles,
                fallback_profiles=[candidate_profile for _, candidate_profile in finalists],
                desired_count=peer_count,
            )
            if progress_callback:
                progress_callback(industry_match_message, industry_match_level)
        else:
            qualified_profiles = threshold_qualified_profiles

        qualified_profiles.sort(
            key=lambda candidate_profile: (
                candidate_profile.market_cap or 0,
                candidate_profile.symbol,
            ),
            reverse=True,
        )
        selected_profiles = qualified_profiles[:peer_count]

        peers: list[PeerValuation] = []
        with ThreadPoolExecutor(max_workers=min(self.DETAIL_FETCH_WORKERS, max(len(selected_profiles), 1))) as executor:
            if progress_callback:
                for candidate_profile in selected_profiles:
                    progress_callback(
                        f"已提交高相关同行 {candidate_profile.symbol} 的估值与预期抓取任务。",
                        "progress",
                    )
            future_to_profile = {
                executor.submit(self._fetch_peer_details, candidate_profile): candidate_profile
                for candidate_profile in selected_profiles
            }
            try:
                completed_futures = as_completed(
                    future_to_profile,
                    timeout=self.DETAIL_STAGE_TIMEOUT_SECONDS,
                )
                for future in completed_futures:
                    candidate_profile = future_to_profile[future]
                    try:
                        peer = future.result()
                    except Exception:
                        continue
                    if progress_callback:
                        progress_callback(
                            f"已完成同行 {candidate_profile.symbol} 的估值与预期抓取。",
                            "progress",
                        )
                    peers.append(peer)
            except TimeoutError:
                if progress_callback:
                    progress_callback(
                        "同行估值抓取已达到时间预算，先返回当前已完成的同行结果。",
                        "warning",
                    )

        selected_order = {profile.symbol: index for index, profile in enumerate(selected_profiles)}
        peers.sort(key=lambda peer: selected_order.get(peer.symbol, 999))

        if progress_callback:
            progress_callback(
                f"同行筛选完成，共找到 {len(peers)} 家高相关同{self._basis_display_name(matching_basis)}可比公司。",
                "success",
            )
        return peers

    def _limit_candidates(self, candidates: list[str], candidate_source: str) -> list[str]:
        return candidates[: self.MAX_CANDIDATES_TO_SCORE]

    @staticmethod
    def _matching_basis(profile: CompanyProfile) -> str:
        if profile.industry:
            return "industry"
        return "sector"

    @staticmethod
    def _basis_display_name(basis: str) -> str:
        if basis == "industry":
            return "行业"
        return "板块"

    def _matching_label(self, profile: CompanyProfile) -> str:
        return profile.industry or profile.sector or "相关业务"

    @staticmethod
    def _minimum_accept_score(profile: CompanyProfile) -> float:
        return 70 if profile.industry else 50

    @staticmethod
    def _filter_fmp_profiles_by_industry(
        target_profile: CompanyProfile,
        candidate_profiles: list[CompanyProfile],
    ) -> tuple[list[CompanyProfile], str, str]:
        if not target_profile.industry:
            return (
                candidate_profiles,
                "目标公司缺少 FMP 行业字段，FMP peers 将直接按市值优先挑选行业相关龙头。",
                "warning",
            )

        profiles_with_industry = [
            candidate_profile
            for candidate_profile in candidate_profiles
            if candidate_profile.industry
        ]
        if not profiles_with_industry:
            return (
                candidate_profiles,
                "FMP peers 候选公司暂未拿到可用的 FMP 行业字段，改为按市值优先挑选行业相关龙头。",
                "warning",
            )

        exact_industry_matches = [
            candidate_profile
            for candidate_profile in profiles_with_industry
            if candidate_profile.industry == target_profile.industry
        ]
        if exact_industry_matches:
            return (
                exact_industry_matches,
                f"FMP peers 中找到 {len(exact_industry_matches)} 家 FMP 行业完全匹配公司，优先按市值排序。",
                "progress",
            )

        exact_sector_matches = [
            candidate_profile
            for candidate_profile in profiles_with_industry
            if candidate_profile.sector and candidate_profile.sector == target_profile.sector
        ]
        if exact_sector_matches:
            return (
                exact_sector_matches,
                f"FMP peers 中未找到 FMP 行业完全匹配公司，已退到 {len(exact_sector_matches)} 家同板块公司，并按市值排序。",
                "warning",
            )

        return (
            candidate_profiles,
            "FMP peers 中未找到可用的 FMP 行业匹配公司，改为按市值优先挑选行业相关龙头。",
            "warning",
        )

    @staticmethod
    def _fill_profiles_by_market_cap(
        primary_profiles: list[CompanyProfile],
        fallback_profiles: list[CompanyProfile],
        desired_count: int,
    ) -> list[CompanyProfile]:
        selected_by_symbol: dict[str, CompanyProfile] = {}

        for candidate_profile in primary_profiles:
            selected_by_symbol[candidate_profile.symbol] = candidate_profile

        if len(selected_by_symbol) < desired_count:
            for candidate_profile in sorted(
                fallback_profiles,
                key=lambda profile: (profile.market_cap or 0, profile.symbol),
                reverse=True,
            ):
                selected_by_symbol.setdefault(candidate_profile.symbol, candidate_profile)
                if len(selected_by_symbol) >= desired_count:
                    break

        selected_profiles = list(selected_by_symbol.values())
        selected_profiles.sort(
            key=lambda profile: (profile.market_cap or 0, profile.symbol),
            reverse=True,
        )
        return selected_profiles

    @staticmethod
    def _normalize_text(value: str | None) -> set[str]:
        if not value:
            return set()
        normalized = (
            value.lower()
            .replace("&", " ")
            .replace("/", " ")
            .replace(",", " ")
            .replace("-", " ")
        )
        return {token for token in normalized.split() if token}

    def _candidate_score(
        self,
        target_profile: CompanyProfile,
        candidate_profile: CompanyProfile,
    ) -> float:
        target_sic = target_profile.sic_code
        candidate_sic = candidate_profile.sic_code
        target_industry = target_profile.industry
        candidate_industry = candidate_profile.industry
        target_sector = target_profile.sector
        candidate_sector = candidate_profile.sector

        score = 0.0
        if target_sic and candidate_sic:
            if candidate_sic == target_sic:
                score += 130
            elif candidate_sic[:3] == target_sic[:3]:
                score += 75
            elif candidate_sic[:2] == target_sic[:2]:
                score += 35

        if target_industry and candidate_industry:
            if candidate_industry == target_industry:
                score += 100
            else:
                target_tokens = self._normalize_text(target_industry)
                candidate_tokens = self._normalize_text(candidate_industry)
                overlap = len(target_tokens & candidate_tokens)
                union = len(target_tokens | candidate_tokens)
                similarity = overlap / union if union else 0
                if similarity >= 0.6:
                    score += 75
                elif similarity >= 0.35 and target_sector and candidate_sector == target_sector:
                    score += 55
        elif target_sector and candidate_sector == target_sector:
            score += 55

        if target_sector and candidate_sector == target_sector:
            score += 10

        if (
            target_profile.country
            and candidate_profile.country
            and target_profile.country == candidate_profile.country
        ):
            score += 5

        score += self._market_cap_similarity_score(
            target_profile.market_cap,
            candidate_profile.market_cap,
        )
        return score

    @staticmethod
    def _market_cap_similarity_score(
        target_market_cap: float | None,
        candidate_market_cap: float | None,
    ) -> float:
        if (
            target_market_cap is None
            or candidate_market_cap is None
            or target_market_cap <= 0
            or candidate_market_cap <= 0
        ):
            return 0.0

        ratio = max(target_market_cap, candidate_market_cap) / min(
            target_market_cap,
            candidate_market_cap,
        )
        log_distance = abs(math.log10(ratio))
        if log_distance <= 0.15:
            return 20
        if log_distance <= 0.35:
            return 14
        if log_distance <= 0.6:
            return 8
        if log_distance <= 1.0:
            return 3
        return 0

    @staticmethod
    def median_forward_pe(peers: list[PeerValuation]) -> float | None:
        values = [peer.forward_pe for peer in peers if peer.forward_pe and peer.forward_pe > 0]
        return median(values) if values else None

    @staticmethod
    def _to_peer(
        profile: CompanyProfile, market: MarketSnapshot, forecast: ForecastSnapshot
    ) -> PeerValuation:
        return PeerValuation(
            symbol=profile.symbol,
            name=profile.name,
            sector=profile.sector,
            industry=profile.industry,
            sic_code=profile.sic_code,
            price=market.price,
            market_cap=profile.market_cap,
            trailing_pe=market.trailing_pe,
            forward_pe=market.forward_pe,
            peg_ratio=market.peg_ratio,
            revenue_growth=forecast.revenue_growth,
            earnings_growth=forecast.earnings_growth,
        )

    def _fetch_peer_details(self, profile: CompanyProfile) -> PeerValuation:
        market = self.provider.get_market_snapshot(profile.symbol)
        forecast = self.provider.get_forecast(profile.symbol)
        enrich_market_with_forecast(market, forecast)
        if market.trailing_pe is None or market.trailing_pe < 3 or market.peg_ratio is None:
            execution = self.provider.get_earnings_execution(profile.symbol, limit=4)
            financial_history = self.provider.get_financial_history(profile.symbol, limit=4)
            enrich_market_with_financial_history(market, financial_history, execution)
        return self._to_peer(profile, market, forecast)
