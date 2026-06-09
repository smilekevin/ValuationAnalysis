from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from valuation_analysis.config import settings
from valuation_analysis.models import (
    CompanyAnalysis,
    CompanyProfile,
    EarningsExecutionMetrics,
    FinancialPeriod,
    ForecastSnapshot,
    MarketSnapshot,
    PeerValuation,
    ValuationAssessment,
    ValuationHistorySnapshot,
)
from valuation_analysis.providers.base import MarketDataProvider
from valuation_analysis.progress import ProgressCallback
from valuation_analysis.services.market_enrichment import (
    enrich_market_with_financial_history,
    enrich_market_with_forecast,
    enrich_market_with_valuation_history,
)
from valuation_analysis.services.peer_analysis import PeerAnalysisService


class ValuationService:
    VALUATION_HISTORY_YEARS = 12
    EARNINGS_EVENT_LIMIT = 10
    PRICE_SIMULATION_PE_MAX = 100

    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider
        self.peer_service = PeerAnalysisService(provider)

    def analyze_company(
        self,
        symbol: str,
        peer_count: int = 5,
        progress_callback: ProgressCallback | None = None,
    ) -> CompanyAnalysis:
        self.provider.set_progress_callback(progress_callback)
        try:
            if progress_callback:
                progress_callback(f"开始分析 {symbol}。", "start")
                progress_callback(
                    f"正在并发抓取核心 FMP 数据，最大并发数 {self._max_data_workers()}。",
                    "progress",
                )

            (
                profile,
                market,
                valuation_history,
                forecast,
                execution,
            ) = self._fetch_core_data(symbol)

            if progress_callback:
                progress_callback("核心数据抓取完成，正在抓取季度财务报表。", "progress")
            financial_history = self.provider.get_financial_history(
                symbol,
                execution=execution,
            )

            enrich_market_with_valuation_history(market, valuation_history)
            enrich_market_with_forecast(market, forecast)
            enrich_market_with_financial_history(market, financial_history, execution)
            self._enrich_valuation_history_snapshot(valuation_history, market)

            if progress_callback:
                progress_callback("正在筛选并比较同行公司。", "progress")
            peers = self.peer_service.build_peer_set(
                symbol,
                profile,
                peer_count=peer_count,
                progress_callback=progress_callback,
            )

            if progress_callback:
                progress_callback("正在生成综合价值评估结论。", "progress")
            assessment = self._build_assessment(
                profile,
                market,
                valuation_history,
                financial_history,
                forecast,
                peers,
                execution,
            )

            if progress_callback:
                progress_callback("分析完成，正在返回结果。", "success")

            return CompanyAnalysis(
                company=profile,
                market=market,
                valuation_history=valuation_history,
                financial_history=financial_history,
                forecast=forecast,
                earnings_execution=execution,
                peers=peers,
                assessment=assessment,
            )
        finally:
            self.provider.set_progress_callback(None)

    @staticmethod
    def _max_data_workers() -> int:
        return max(1, settings.fmp_max_workers)

    def _fetch_core_data(
        self,
        symbol: str,
    ) -> tuple[
        CompanyProfile,
        MarketSnapshot,
        ValuationHistorySnapshot,
        ForecastSnapshot,
        EarningsExecutionMetrics,
    ]:
        with ThreadPoolExecutor(max_workers=self._max_data_workers()) as executor:
            profile_future = executor.submit(self.provider.get_company_profile, symbol)
            market_future = executor.submit(self.provider.get_market_snapshot, symbol)
            valuation_history_future = executor.submit(
                self.provider.get_valuation_history,
                symbol,
                self.VALUATION_HISTORY_YEARS,
            )
            forecast_future = executor.submit(self.provider.get_forecast, symbol)
            execution_future = executor.submit(
                self.provider.get_earnings_execution,
                symbol,
                self.EARNINGS_EVENT_LIMIT,
            )

            return (
                profile_future.result(),
                market_future.result(),
                valuation_history_future.result(),
                forecast_future.result(),
                execution_future.result(),
            )

    @staticmethod
    def _enrich_valuation_history_snapshot(
        valuation_history,
        market: MarketSnapshot,
    ) -> None:
        if market.trailing_pe is not None and market.trailing_pe > 0:
            valuation_history.current_trailing_pe = market.trailing_pe

        ValuationService._fill_valuation_history_stats(
            valuation_history,
            point_attr="trailing_pe",
            current_attr="current_trailing_pe",
            min_attr="min_trailing_pe",
            max_attr="max_trailing_pe",
            median_attr="median_trailing_pe",
            percentile_attr="current_percentile",
        )

        if market.forward_pe is not None and market.forward_pe > 0:
            valuation_history.current_forward_pe = market.forward_pe
        ValuationService._fill_valuation_history_stats(
            valuation_history,
            point_attr="forward_pe",
            current_attr="current_forward_pe",
            min_attr="min_forward_pe",
            max_attr="max_forward_pe",
            median_attr="median_forward_pe",
            percentile_attr="forward_pe_percentile",
        )

        if (
            valuation_history.current_price_to_sales is None
            and market.price_to_sales is not None
        ):
            valuation_history.current_price_to_sales = market.price_to_sales
        ValuationService._fill_valuation_history_stats(
            valuation_history,
            point_attr="price_to_sales",
            current_attr="current_price_to_sales",
            min_attr="min_price_to_sales",
            max_attr="max_price_to_sales",
            median_attr="median_price_to_sales",
            percentile_attr="price_to_sales_percentile",
        )

        if (
            valuation_history.current_enterprise_to_ebitda is None
            and market.enterprise_to_ebitda is not None
        ):
            valuation_history.current_enterprise_to_ebitda = market.enterprise_to_ebitda
        ValuationService._fill_valuation_history_stats(
            valuation_history,
            point_attr="enterprise_to_ebitda",
            current_attr="current_enterprise_to_ebitda",
            min_attr="min_enterprise_to_ebitda",
            max_attr="max_enterprise_to_ebitda",
            median_attr="median_enterprise_to_ebitda",
            percentile_attr="enterprise_to_ebitda_percentile",
        )

    @staticmethod
    def _fill_valuation_history_stats(
        valuation_history,
        *,
        point_attr: str,
        current_attr: str,
        min_attr: str,
        max_attr: str,
        median_attr: str,
        percentile_attr: str,
    ) -> None:
        current_value = getattr(valuation_history, current_attr, None)
        if current_value is None or current_value <= 0:
            return

        historical_values = [
            value
            for point in valuation_history.points
            if (value := getattr(point, point_attr, None)) is not None and value > 0
        ]
        full_values = historical_values + [current_value]
        if not full_values:
            return

        sorted_values = sorted(full_values)
        middle = len(sorted_values) // 2
        if len(sorted_values) % 2 == 1:
            median_value = sorted_values[middle]
        else:
            median_value = (sorted_values[middle - 1] + sorted_values[middle]) / 2
        less_or_equal_count = sum(1 for value in full_values if value <= current_value)

        setattr(valuation_history, min_attr, min(full_values))
        setattr(valuation_history, max_attr, max(full_values))
        setattr(valuation_history, median_attr, median_value)
        setattr(valuation_history, percentile_attr, less_or_equal_count / len(full_values))

    def _build_assessment(
        self,
        profile: CompanyProfile,
        market: MarketSnapshot,
        valuation_history: ValuationHistorySnapshot,
        financial_history: list[FinancialPeriod],
        forecast: ForecastSnapshot,
        peers: list[PeerValuation],
        execution: EarningsExecutionMetrics,
    ) -> ValuationAssessment:
        rationale: list[str] = []
        history_score, history_metric, history_percentile = self._score_history_valuation(
            valuation_history,
        )
        price_simulation_score, price_simulation_rationale = self._score_price_simulation(
            profile,
            market,
            valuation_history,
            financial_history,
            forecast,
        )
        primary_valuation_score = (
            price_simulation_score
            if price_simulation_score is not None
            else history_score
        )
        growth_score = self._score_growth_quality(forecast, financial_history)
        execution_score = self._score_execution_quality(execution)
        peer_score, peer_weight, comparable_peers, peer_relative_premium = self._score_peer_context(
            profile,
            market,
            peers,
        )

        valuation_score = round(
            self._weighted_average(
                [
                    (primary_valuation_score, 0.45),
                    (peer_score, peer_weight),
                ]
            )
        )

        if price_simulation_rationale:
            rationale.append(price_simulation_rationale)
        elif history_percentile is None:
            rationale.append(
                "价格模拟与历史估值分位数据不足，估值主评分按中性处理；此时不会因为同行 PE 看起来便宜而直接判断低估。"
            )
        else:
            rationale.append(
                f"当前 {history_metric} 处在自身历史约 {history_percentile:.0%} 分位，"
                f"历史估值分是 {history_score}；由于价格模拟不足，本次暂以历史分位作为估值主依据。"
            )

        if peer_weight == 0:
            rationale.append(
                "同行可比性不足或缺少有效正向 Forward P/E，同行估值本次只展示，不进入主评分。"
            )
        elif peer_weight <= 0.05:
            rationale.append(
                "同行匹配更像同板块/相关公司而非严格可比公司，同行估值只以低权重作为 sanity check。"
            )
        else:
            rationale.append("找到足够的同细分行业可比公司，同行估值以 10% 低权重参与综合判断。")

        if peer_relative_premium is not None and comparable_peers:
            if peer_relative_premium <= -0.20:
                rationale.append(
                    f"{profile.symbol} 的 Forward P/E 低于可比样本中位数约 {abs(peer_relative_premium):.0%}，"
                    "但这只增强历史估值信号，不单独决定低估结论。"
                )
            elif peer_relative_premium >= 0.20:
                rationale.append(
                    f"{profile.symbol} 的 Forward P/E 高于可比样本中位数约 {peer_relative_premium:.0%}，"
                    "相对估值对结论形成一定压力。"
                )
            else:
                rationale.append("Forward P/E 与可比样本中位数接近，同行维度整体中性。")

        rationale.extend(self._growth_rationale(growth_score, forecast, financial_history))
        rationale.extend(self._execution_rationale(execution_score, execution))

        composite = self._weighted_average(
            [
                (primary_valuation_score, 0.45),
                (growth_score, 0.30),
                (execution_score, 0.15),
                (peer_score, peer_weight),
            ]
        )

        if price_simulation_score is not None and price_simulation_score <= 35 and growth_score < 50:
            composite = min(composite, 44)
            rationale.append(
                "保护规则触发：价格模拟多数情景不支持当前价格，且增长质量偏弱，因此不能给出低估结论。"
            )
        elif history_percentile is not None and history_percentile >= 0.80 and growth_score < 50:
            composite = min(composite, 44)
            rationale.append(
                "保护规则触发：历史估值处于高位且增长质量偏弱，即使同行不贵，也不能给出低估结论。"
            )

        if composite >= 67:
            label = "Undervalued"
        elif composite >= 45:
            label = "Fairly Valued"
        else:
            label = "Richly Valued"

        return ValuationAssessment(
            label=label,
            valuation_score=valuation_score,
            execution_score=execution_score,
            growth_score=growth_score,
            rationale=rationale,
        )

    @staticmethod
    def _score_price_simulation(
        profile: CompanyProfile,
        market: MarketSnapshot,
        valuation_history: ValuationHistorySnapshot,
        financial_history: list[FinancialPeriod],
        forecast: ForecastSnapshot,
    ) -> tuple[int | None, str | None]:
        current_price = market.price
        if current_price is None or current_price <= 0:
            return None, None

        pe_target_prices: list[float] = []
        ps_target_prices: list[float] = []
        history_points = valuation_history.points
        historical_pe_values = [
            point.trailing_pe
            for point in history_points
            if point.trailing_pe is not None and point.trailing_pe > 0
        ]
        historical_ps_values = [
            point.price_to_sales
            for point in history_points
            if point.price_to_sales is not None and point.price_to_sales > 0
        ]
        has_negative_quarter_eps = any(
            period.diluted_eps is not None and period.diluted_eps < 0
            for period in financial_history
        )
        has_extreme_pe = any(
            value > ValuationService.PRICE_SIMULATION_PE_MAX
            for value in historical_pe_values
        )

        eps = forecast.next_year_eps if forecast.next_year_eps is not None else market.forward_eps
        if (
            eps is not None
            and eps > 0
            and historical_pe_values
            and not has_negative_quarter_eps
            and not has_extreme_pe
        ):
            pe_multiples = ValuationService._simulation_multiples(
                historical_pe_values,
                valuation_history.median_trailing_pe,
            )
            pe_target_prices = [eps * multiple for multiple in pe_multiples]

        next_revenue = forecast.next_year_revenue
        market_cap = profile.market_cap
        implied_shares = (
            market_cap / current_price
            if market_cap is not None and market_cap > 0
            else None
        )
        if (
            next_revenue is not None
            and next_revenue > 0
            and implied_shares is not None
            and implied_shares > 0
            and historical_ps_values
        ):
            ps_multiples = ValuationService._simulation_multiples(
                historical_ps_values,
                valuation_history.median_price_to_sales,
            )
            ps_target_prices = [
                (next_revenue * multiple) / implied_shares
                for multiple in ps_multiples
            ]

        pe_target_prices = [
            price
            for price in pe_target_prices
            if price is not None and price > 0
        ]
        ps_target_prices = [
            price
            for price in ps_target_prices
            if price is not None and price > 0
        ]
        if len(pe_target_prices) >= 3:
            target_prices = pe_target_prices
            scenario_group = "P/E"
        elif len(ps_target_prices) >= 3:
            target_prices = ps_target_prices
            scenario_group = "P/S"
        else:
            target_prices = []

        if len(target_prices) < 3:
            return None, None

        upside_20_count = sum(
            1
            for target_price in target_prices
            if target_price >= current_price * 1.20
        )
        upside_count = sum(
            1
            for target_price in target_prices
            if target_price > current_price
        )
        downside_count = sum(
            1
            for target_price in target_prices
            if target_price < current_price
        )

        if upside_20_count >= 4:
            score = 95
            conclusion = "4 个或以上情景价高出当前价格 20% 以上，价格模拟判断为极度低估。"
        elif upside_20_count == 3:
            score = 82
            conclusion = "3 个情景价高出当前价格 20% 以上，价格模拟判断为低估。"
        elif upside_20_count <= 1:
            score = 25
            conclusion = "只有 1 个或没有情景价高出当前价格 20% 以上，价格模拟判断为高估。"
        else:
            score = 50
            conclusion = "2 个情景价高出当前价格 20% 以上，价格模拟判断为中性。"

        rationale = (
            f"估值主评分改用价格模拟：基于 {scenario_group} 共 {len(target_prices)} 个情景"
            "（已排除历史低位锚），"
            f"{upside_20_count} 个高出当前价 20% 以上，{upside_count} 个高于当前价，"
            f"{downside_count} 个低于当前价；{conclusion}"
        )
        if has_negative_quarter_eps or has_extreme_pe or eps is None or eps <= 0:
            skip_reasons = [
                "历史季度 EPS 出现负数" if has_negative_quarter_eps else None,
                (
                    f"历史 P/E 超过 {ValuationService.PRICE_SIMULATION_PE_MAX}x"
                    if has_extreme_pe
                    else None
                ),
                "明年 EPS 为负或缺失" if eps is None or eps <= 0 else None,
            ]
            skip_reasons = [reason for reason in skip_reasons if reason]
            if skip_reasons and scenario_group == "P/S":
                rationale += f" P/E 因 {'、'.join(skip_reasons)} 被跳过，改用 P/S 模拟。"

        return score, rationale

    @staticmethod
    def _simulation_multiples(
        historical_values: list[float],
        historical_median: float | None,
    ) -> list[float]:
        candidates = [
            historical_values[-1] if historical_values else None,
            ValuationService._average(historical_values[-4:]),
            ValuationService._average(historical_values[-8:]),
            ValuationService._average(historical_values[-20:]),
            historical_median if historical_median is not None else ValuationService._median(historical_values),
        ]
        return [
            value
            for value in candidates
            if value is not None and value > 0
        ]

    @staticmethod
    def _score_history_valuation(
        valuation_history: ValuationHistorySnapshot,
    ) -> tuple[int, str, float | None]:
        metric_candidates = [
            ("Trailing P/E", valuation_history.current_percentile),
            ("EV/EBITDA", valuation_history.enterprise_to_ebitda_percentile),
            ("P/S", valuation_history.price_to_sales_percentile),
        ]
        for metric_name, percentile in metric_candidates:
            if percentile is not None:
                return ValuationService._score_percentile(percentile), metric_name, percentile
        return 50, "历史估值", None

    @staticmethod
    def _score_percentile(percentile: float) -> int:
        if percentile <= 0.20:
            return 90
        if percentile <= 0.40:
            return 75
        if percentile <= 0.60:
            return 55
        if percentile <= 0.80:
            return 35
        return 20

    @staticmethod
    def _average(values: list[float]) -> float | None:
        valid_values = [
            value
            for value in values
            if value is not None
        ]
        if not valid_values:
            return None
        return sum(valid_values) / len(valid_values)

    @staticmethod
    def _score_growth_quality(
        forecast: ForecastSnapshot,
        financial_history: list[FinancialPeriod],
    ) -> int:
        latest_period = financial_history[0] if financial_history else None
        growth_inputs = [
            value
            for value in [
                forecast.earnings_growth,
                forecast.revenue_growth,
                latest_period.revenue_yoy_growth if latest_period else None,
                latest_period.net_income_yoy_growth if latest_period else None,
                latest_period.diluted_eps_yoy_growth if latest_period else None,
            ]
            if value is not None
        ]
        if not growth_inputs:
            return 50

        avg_growth = sum(growth_inputs) / len(growth_inputs)
        if avg_growth >= 0.20:
            score = 85
        elif avg_growth >= 0.10:
            score = 75
        elif avg_growth >= 0.03:
            score = 60
        elif avg_growth >= -0.05:
            score = 45
        else:
            score = 30

        if (
            latest_period
            and latest_period.revenue_yoy_growth is not None
            and latest_period.revenue_yoy_growth > 0
            and latest_period.net_income_yoy_growth is not None
            and latest_period.net_income_yoy_growth < 0
        ):
            score = min(score, 50)

        return score

    @staticmethod
    def _score_execution_quality(execution: EarningsExecutionMetrics) -> int:
        if execution.beat_rate is None:
            return 50

        if execution.beat_rate >= 0.75:
            score = 85
        elif execution.beat_rate >= 0.55:
            score = 65
        elif execution.beat_rate >= 0.40:
            score = 45
        else:
            score = 30

        if execution.average_surprise_pct is not None:
            if execution.average_surprise_pct >= 5:
                score = min(score + 8, 95)
            elif execution.average_surprise_pct < 0:
                score = max(score - 8, 20)
        return score

    def _score_peer_context(
        self,
        profile: CompanyProfile,
        market: MarketSnapshot,
        peers: list[PeerValuation],
    ) -> tuple[int, float, list[PeerValuation], float | None]:
        valid_forward_pe = market.forward_pe if market.forward_pe and market.forward_pe > 0 else None
        if valid_forward_pe is None:
            return 50, 0.0, [], None

        high_confidence_peers = self._select_comparable_peers_for_valuation(profile, peers)
        if len(high_confidence_peers) >= 2:
            comparable_peers = high_confidence_peers
            peer_weight = 0.10
        else:
            same_sector_peers = [
                peer
                for peer in peers
                if peer.forward_pe is not None
                and peer.forward_pe > 0
                and profile.sector
                and peer.sector == profile.sector
            ]
            if len(same_sector_peers) < 2:
                return 50, 0.0, [], None
            comparable_peers = same_sector_peers
            peer_weight = 0.05

        peer_median_forward_pe = PeerAnalysisService.median_forward_pe(comparable_peers)
        if not peer_median_forward_pe:
            return 50, 0.0, [], None

        relative_premium = (valid_forward_pe - peer_median_forward_pe) / peer_median_forward_pe
        if relative_premium <= -0.30:
            peer_score = 85
        elif relative_premium <= -0.10:
            peer_score = 70
        elif relative_premium < 0.10:
            peer_score = 55
        elif relative_premium < 0.30:
            peer_score = 40
        else:
            peer_score = 25

        return peer_score, peer_weight, comparable_peers, relative_premium

    @staticmethod
    def _growth_rationale(
        growth_score: int,
        forecast: ForecastSnapshot,
        financial_history: list[FinancialPeriod],
    ) -> list[str]:
        latest_period = financial_history[0] if financial_history else None
        details: list[str] = []
        if growth_score >= 75:
            details.append("增长质量较强，未来预期与最近财务趋势可以支撑较高估值倍数。")
        elif growth_score >= 55:
            details.append("增长质量处于中性偏稳区间，估值能否扩张主要取决于后续兑现。")
        elif growth_score >= 40:
            details.append("增长质量偏弱，若历史估值分位不低，需要对估值保持谨慎。")
        else:
            details.append("增长质量明显承压，估值结论应优先考虑下修风险。")

        if latest_period and latest_period.revenue_yoy_growth is not None:
            details.append(f"最近季度收入同比为 {latest_period.revenue_yoy_growth:.1%}。")
        if forecast.earnings_growth is not None:
            details.append(f"分析师预期盈利增长约 {forecast.earnings_growth:.1%}。")
        return details

    @staticmethod
    def _execution_rationale(
        execution_score: int,
        execution: EarningsExecutionMetrics,
    ) -> list[str]:
        if execution.beat_rate is None:
            return ["缺少足够的历史财报 surprise 数据，执行力判断按中性处理。"]
        if execution_score >= 75:
            return ["历史财报 beat rate 较高且 surprise 质量较好，说明公司兑现预期的能力较强。"]
        if execution_score >= 55:
            return ["历史财报达成率中性偏稳，执行力没有明显短板。"]
        return ["历史财报 miss 或 surprise 偏弱，执行稳定性需要折价考虑。"]

    @staticmethod
    def _weighted_average(score_weights: list[tuple[int, float]]) -> float:
        total_weight = sum(weight for _, weight in score_weights if weight > 0)
        if total_weight <= 0:
            return 50
        return sum(score * weight for score, weight in score_weights if weight > 0) / total_weight

    @staticmethod
    def _select_comparable_peers_for_valuation(
        profile: CompanyProfile,
        peers: list[PeerValuation],
    ) -> list[PeerValuation]:
        peers_with_positive_forward_pe = [
            peer
            for peer in peers
            if peer.forward_pe is not None and peer.forward_pe > 0
        ]
        if not peers_with_positive_forward_pe:
            return []

        if profile.sic_code:
            exact_sic_peers = [
                peer
                for peer in peers_with_positive_forward_pe
                if peer.sic_code and peer.sic_code == profile.sic_code
            ]
            if exact_sic_peers:
                return exact_sic_peers

            three_digit_sic_peers = [
                peer
                for peer in peers_with_positive_forward_pe
                if peer.sic_code and peer.sic_code[:3] == profile.sic_code[:3]
            ]
            if len(three_digit_sic_peers) >= 2:
                return three_digit_sic_peers

            two_digit_sic_peers = [
                peer
                for peer in peers_with_positive_forward_pe
                if peer.sic_code and peer.sic_code[:2] == profile.sic_code[:2]
            ]
            if len(two_digit_sic_peers) >= 2:
                return two_digit_sic_peers

        if profile.industry:
            exact_industry_peers = [
                peer
                for peer in peers_with_positive_forward_pe
                if peer.industry and peer.industry == profile.industry
            ]
            if len(exact_industry_peers) >= 2:
                return exact_industry_peers

        return []
