const form = document.getElementById("search-form");
const symbolInput = document.getElementById("symbol-input");
const peerCountInput = document.getElementById("peer-count-input");
const statusLogCard = document.getElementById("status-log-card");
const statusBanner = document.getElementById("status-banner");
const statusLogToggle = document.getElementById("status-log-toggle");
const submitButton = form.querySelector("button");

const companyName = document.getElementById("company-name");
const companySymbol = document.getElementById("company-symbol");
const companyIndustry = document.getElementById("company-industry");
const companySector = document.getElementById("company-sector");
const companyMarketCap = document.getElementById("company-market-cap");
const assessmentLabel = document.getElementById("assessment-label");
const assessmentSummary = document.getElementById("assessment-summary");
const valuationScore = document.getElementById("valuation-score");
const executionScore = document.getElementById("execution-score");
const growthScore = document.getElementById("growth-score");
const assessmentRationale = document.getElementById("assessment-rationale");
const marketMetrics = document.getElementById("market-metrics");
const priceSimulation = document.getElementById("price-simulation");
const forecastMetrics = document.getElementById("forecast-metrics");
const earningsMetrics = document.getElementById("earnings-metrics");
const valuationHistorySummary = document.getElementById("valuation-history-summary");
const valuationHistoryChart = document.getElementById("valuation-history-chart");
const peersTableBody = document.getElementById("peers-table-body");
const financialTableBody = document.getElementById("financial-table-body");
const financialTrendSummary = document.getElementById("financial-trend-summary");
const earningsEvents = document.getElementById("earnings-events");
const logOutput = document.getElementById("log-output");
let activeEventSource = null;
let activeValuationMetric = "trailing_pe";
let latestValuationHistorySnapshot = null;

const VALUATION_HISTORY_METRICS = {
  trailing_pe: {
    label: "P/E",
    chartLabel: "Trailing P/E",
    pointKey: "trailing_pe",
    currentKey: "current_trailing_pe",
    minKey: "min_trailing_pe",
    maxKey: "max_trailing_pe",
    medianKey: "median_trailing_pe",
    percentileKey: "current_percentile",
  },
  forward_pe: {
    label: "Forward P/E",
    chartLabel: "Forward P/E (NTM)",
    pointKey: "forward_pe",
    currentKey: "current_forward_pe",
    minKey: "min_forward_pe",
    maxKey: "max_forward_pe",
    medianKey: "median_forward_pe",
    percentileKey: "forward_pe_percentile",
  },
  price_to_sales: {
    label: "P/S",
    chartLabel: "Price / Sales",
    pointKey: "price_to_sales",
    currentKey: "current_price_to_sales",
    minKey: "min_price_to_sales",
    maxKey: "max_price_to_sales",
    medianKey: "median_price_to_sales",
    percentileKey: "price_to_sales_percentile",
  },
  enterprise_to_ebitda: {
    label: "EV/EBITDA",
    chartLabel: "EV / EBITDA",
    pointKey: "enterprise_to_ebitda",
    currentKey: "current_enterprise_to_ebitda",
    minKey: "min_enterprise_to_ebitda",
    maxKey: "max_enterprise_to_ebitda",
    medianKey: "median_enterprise_to_ebitda",
    percentileKey: "enterprise_to_ebitda_percentile",
  },
};
const PRICE_SIMULATION_PE_MAX = 100;

function syncStatusToggleLabel() {
  statusLogToggle.textContent = statusLogCard.open ? "收起日志" : "展开日志";
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  }).format(Number(value));
}

function formatCurrency(value, currency = "USD", digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function formatPercent(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${(Number(value) * 100).toFixed(digits)}%`;
}

function formatSurprisePercent(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${Number(value).toFixed(digits)}%`;
}

function formatCompactCurrency(value, currency = "USD") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    notation: "compact",
    maximumFractionDigits: 2,
  }).format(Number(value));
}

function formatMultiple(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${formatNumber(value, digits)}x`;
}

function average(values) {
  const validValues = values.filter((value) => Number.isFinite(Number(value)));
  if (!validValues.length) {
    return null;
  }
  return validValues.reduce((total, value) => total + Number(value), 0) / validValues.length;
}

function percentileValue(values, percentile) {
  const sortedValues = values
    .filter((value) => Number.isFinite(Number(value)))
    .map(Number)
    .sort((a, b) => a - b);
  if (!sortedValues.length) {
    return null;
  }
  if (sortedValues.length === 1) {
    return sortedValues[0];
  }
  const index = (sortedValues.length - 1) * percentile;
  const lowerIndex = Math.floor(index);
  const upperIndex = Math.ceil(index);
  if (lowerIndex === upperIndex) {
    return sortedValues[lowerIndex];
  }
  const weight = index - lowerIndex;
  return sortedValues[lowerIndex] * (1 - weight) + sortedValues[upperIndex] * weight;
}

function trendClass(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "trend-neutral";
  }
  if (Number(value) > 0) {
    return "trend-positive";
  }
  if (Number(value) < 0) {
    return "trend-negative";
  }
  return "trend-neutral";
}

function describeTrend(value, label) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return `${label}数据不足`;
  }
  if (Number(value) >= 0.15) {
    return `${label}明显加速`;
  }
  if (Number(value) > 0) {
    return `${label}保持增长`;
  }
  if (Number(value) <= -0.15) {
    return `${label}明显承压`;
  }
  return `${label}略有回落`;
}

function renderMetricList(container, items) {
  container.innerHTML = items
    .map(
      (item) => `
        <div class="metric">
          <span class="metric-label">${item.label}</span>
          <strong class="metric-value">${item.value}</strong>
        </div>
      `
    )
    .join("");
}

function renderPriceSimulation(data, currency) {
  const eps = data.forecast?.next_year_eps ?? data.market?.forward_eps;
  const nextRevenue = data.forecast?.next_year_revenue;
  const currentPrice = data.market?.price;
  const marketCap = data.company?.market_cap;
  const impliedShares =
    marketCap && marketCap > 0 && currentPrice && currentPrice > 0
      ? marketCap / currentPrice
      : null;
  const historyPoints = data.valuation_history?.points ?? [];
  const historicalPeValues = historyPoints
    .map((point) => point.trailing_pe)
    .filter((value) => Number.isFinite(Number(value)) && Number(value) > 0)
    .map(Number);
  const historicalPsValues = historyPoints
    .map((point) => point.price_to_sales)
    .filter((value) => Number.isFinite(Number(value)) && Number(value) > 0)
    .map(Number);
  const hasNegativeQuarterEps = (data.financial_history ?? []).some(
    (period) => Number.isFinite(Number(period.diluted_eps)) && Number(period.diluted_eps) < 0
  );
  const hasExtremePe = historicalPeValues.some((value) => value > PRICE_SIMULATION_PE_MAX);
  const peSkipReasons = [
    hasNegativeQuarterEps ? "历史季度 EPS 出现负数" : null,
    hasExtremePe ? `历史 P/E 出现超过 ${PRICE_SIMULATION_PE_MAX}x 的极端值` : null,
    !eps || eps <= 0 ? "明年 EPS 为负或缺失" : null,
  ].filter(Boolean);
  const sections = [];

  if (eps && eps > 0 && historicalPeValues.length && !hasNegativeQuarterEps && !hasExtremePe) {
    const latestPe = historicalPeValues[historicalPeValues.length - 1] ?? null;
    const recentFourPe = average(historicalPeValues.slice(-4));
    const recentTwoYearPe = average(historicalPeValues.slice(-8));
    const recentFiveYearPe = average(historicalPeValues.slice(-20));
    const medianPe = data.valuation_history?.median_trailing_pe ?? percentileValue(historicalPeValues, 0.5);
    const peScenarios = [
      {
        label: "上次财报 P/E",
        multiple: latestPe,
        targetPrice: Number(eps) * latestPe,
        note: "最近一个历史季度的 TTM P/E",
      },
      {
        label: "近 1 年平均 P/E",
        multiple: recentFourPe,
        targetPrice: Number(eps) * recentFourPe,
        note: "最近四个历史季度估值均值",
      },
      {
        label: "近 2 年平均 P/E",
        multiple: recentTwoYearPe,
        targetPrice: Number(eps) * recentTwoYearPe,
        note: "最近约 8 个季度估值均值",
      },
      {
        label: "近 5 年平均 P/E",
        multiple: recentFiveYearPe,
        targetPrice: Number(eps) * recentFiveYearPe,
        note: "最近约 20 个季度估值均值",
      },
      {
        label: "历史中位 P/E",
        multiple: medianPe,
        targetPrice: Number(eps) * medianPe,
        note: "历史估值的中性锚",
      },
    ].filter((scenario) => scenario.multiple && scenario.multiple > 0);

    if (peScenarios.length) {
      sections.push({
        title: "P/E 模拟",
        description: `使用明年 EPS ${formatNumber(eps)}。`,
        scenarios: peScenarios,
      });
    }
  }

  if (nextRevenue && nextRevenue > 0 && impliedShares && historicalPsValues.length) {
    const latestPs = historicalPsValues[historicalPsValues.length - 1] ?? null;
    const recentFourPs = average(historicalPsValues.slice(-4));
    const recentTwoYearPs = average(historicalPsValues.slice(-8));
    const recentFiveYearPs = average(historicalPsValues.slice(-20));
    const medianPs =
      data.valuation_history?.median_price_to_sales ?? percentileValue(historicalPsValues, 0.5);
    const psTargetPrice = (multiple) =>
      multiple && multiple > 0 ? (Number(nextRevenue) * multiple) / impliedShares : null;
    const psScenarios = [
      {
        label: "上次财报 P/S",
        multiple: latestPs,
        targetPrice: psTargetPrice(latestPs),
        note: "最近一个历史季度的 P/S",
      },
      {
        label: "近 1 年平均 P/S",
        multiple: recentFourPs,
        targetPrice: psTargetPrice(recentFourPs),
        note: "最近四个历史季度估值均值",
      },
      {
        label: "近 2 年平均 P/S",
        multiple: recentTwoYearPs,
        targetPrice: psTargetPrice(recentTwoYearPs),
        note: "最近约 8 个季度估值均值",
      },
      {
        label: "近 5 年平均 P/S",
        multiple: recentFiveYearPs,
        targetPrice: psTargetPrice(recentFiveYearPs),
        note: "最近约 20 个季度估值均值",
      },
      {
        label: "历史中位 P/S",
        multiple: medianPs,
        targetPrice: psTargetPrice(medianPs),
        note: "历史收入倍数的中性锚",
      },
    ].filter((scenario) => scenario.multiple && scenario.multiple > 0 && scenario.targetPrice);

    if (psScenarios.length) {
      sections.push({
        title: "P/S 模拟",
        description: `使用明年收入 ${formatCompactCurrency(nextRevenue, currency)}，按当前市值/股价反推股本。`,
        scenarios: psScenarios,
      });
    }
  }

  if (!sections.length) {
    priceSimulation.className = "price-simulation empty-state";
    priceSimulation.textContent = "缺少明年 EPS / 收入、历史 P/E / P/S 或当前市值数据，暂时无法做情景价格模拟。";
    return;
  }

  priceSimulation.className = "price-simulation";
  const simulationNotice = peSkipReasons.length
    ? `P/E 已跳过：${peSkipReasons.join("；")}，优先使用 P/S。`
    : "EPS 和历史 P/E 通过质量检查时显示 P/E；收入和历史 P/S 可用时显示 P/S。";
  priceSimulation.innerHTML = `
    <div class="price-simulation-header">
      <div>
        <span>价格模拟</span>
        <strong>明年 EPS / Sales × 历史估值锚</strong>
      </div>
      <p>${simulationNotice} 结果仅作敏感性分析，不是目标价。</p>
    </div>
    ${sections
      .map(
        (section) => `
          <section class="price-simulation-section">
            <div class="price-simulation-section-title">
              <strong>${section.title}</strong>
              <span>${section.description}</span>
            </div>
            <div class="price-scenario-grid">
              ${section.scenarios
        .map(
          (scenario) => `
            <article class="price-scenario">
              <span>${scenario.label}</span>
              <strong>${formatCurrency(scenario.targetPrice, currency)}</strong>
              <p>${formatMultiple(scenario.multiple)} · ${scenario.note}</p>
            </article>
          `
        )
        .join("")}
            </div>
          </section>
        `
      )
      .join("")}
  `;
}

function formatPercentile(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return `${(Number(value) * 100).toFixed(0)}%`;
}

function setStatus(message, type = "") {
  statusBanner.textContent = message;
  statusBanner.className = `status-banner ${type}`.trim();
  statusLogCard.className = `card status-log-card ${type}`.trim();
  if (type === "error") {
    statusLogCard.open = true;
  }
  syncStatusToggleLabel();
}

function clearLogs() {
  logOutput.innerHTML = '<p class="log-entry empty-log">日志初始化完成，正在等待服务返回进度。</p>';
  statusLogCard.open = false;
  syncStatusToggleLabel();
}

function appendLog(message, level = "progress", timestamp = "") {
  const empty = logOutput.querySelector(".empty-log");
  if (empty) {
    empty.remove();
  }

  const entry = document.createElement("p");
  entry.className = `log-entry ${level}`.trim();
  entry.innerHTML = `<strong>${timestamp || "--:--:--"}</strong>${message}`;
  logOutput.appendChild(entry);
  logOutput.scrollTop = logOutput.scrollHeight;
}

function setAssessmentBadge(label) {
  assessmentLabel.textContent = label || "No Analysis";
  const normalized = (label || "").toLowerCase().replace(/\s+/g, "-");
  assessmentLabel.className = `badge ${normalized}`.trim();
}

function renderRationale(items) {
  if (!items || !items.length) {
    assessmentRationale.innerHTML = "<li>暂无解释。</li>";
    return;
  }
  assessmentRationale.innerHTML = items.map((item) => `<li>${item}</li>`).join("");
}

function renderPeers(peers) {
  if (!peers || !peers.length) {
    peersTableBody.innerHTML = '<tr><td colspan="8" class="empty-cell">没有找到可比公司</td></tr>';
    return;
  }

  peersTableBody.innerHTML = peers
    .map(
      (peer) => `
        <tr>
          <td>${peer.symbol ?? "-"}</td>
          <td>${peer.name ?? "-"}</td>
          <td>${peer.industry ?? peer.sector ?? "-"}</td>
          <td>${formatCompactCurrency(peer.market_cap)}</td>
          <td>${formatNumber(peer.forward_pe)}</td>
          <td>${formatNumber(peer.trailing_pe)}</td>
          <td>${formatNumber(peer.peg_ratio)}</td>
          <td>${formatPercent(peer.earnings_growth)}</td>
        </tr>
      `
    )
    .join("");
}

function renderValuationHistory(snapshot, metricKey = activeValuationMetric) {
  latestValuationHistorySnapshot = snapshot;
  const points = snapshot?.points ?? [];
  if (!points.length) {
    valuationHistorySummary.innerHTML =
      '<p class="empty-state">暂无足够的历史估值数据，暂时无法判断当前估值处在历史高位还是低位。</p>';
    valuationHistoryChart.className = "valuation-history-chart empty-state";
    valuationHistoryChart.textContent = "暂无历史估值曲线";
    return;
  }
  const hasMetricSeries = (key) => {
    const pointKey = VALUATION_HISTORY_METRICS[key]?.pointKey;
    return points.some((point) => {
      const value = pointKey ? point[pointKey] : null;
      return value !== null && value !== undefined && Number.isFinite(Number(value));
    });
  };
  let selectedMetricKey = metricKey in VALUATION_HISTORY_METRICS ? metricKey : "trailing_pe";
  if (!hasMetricSeries(selectedMetricKey)) {
    selectedMetricKey =
      Object.keys(VALUATION_HISTORY_METRICS).find((key) => hasMetricSeries(key)) ??
      selectedMetricKey;
  }
  const metric = VALUATION_HISTORY_METRICS[selectedMetricKey] ?? VALUATION_HISTORY_METRICS.trailing_pe;
  activeValuationMetric = selectedMetricKey;

  const metricButtons = Object.entries(VALUATION_HISTORY_METRICS)
    .map(
      ([key, item]) => `
        <button
          type="button"
          class="valuation-metric-button ${key === activeValuationMetric ? "active" : ""}"
          data-valuation-metric="${key}"
          title="切换到 ${item.chartLabel} 历史曲线"
        >${item.label}</button>
      `
    )
    .join("");

  valuationHistorySummary.innerHTML = `
    <div class="valuation-history-toolbar">
      <div class="valuation-metric-switch" aria-label="历史估值指标切换">${metricButtons}</div>
    </div>
    <div class="metric-list valuation-history-metrics">
      <div class="metric">
        <span class="metric-label">当前 ${metric.chartLabel}</span>
        <strong class="metric-value">${formatNumber(snapshot[metric.currentKey])}</strong>
      </div>
      <div class="metric">
        <span class="metric-label">历史中位数</span>
        <strong class="metric-value">${formatNumber(snapshot[metric.medianKey])}</strong>
      </div>
      <div class="metric">
        <span class="metric-label">历史低点</span>
        <strong class="metric-value">${formatNumber(snapshot[metric.minKey])}</strong>
      </div>
      <div class="metric">
        <span class="metric-label">历史高点</span>
        <strong class="metric-value">${formatNumber(snapshot[metric.maxKey])}</strong>
      </div>
      <div class="metric">
        <span class="metric-label">当前历史分位</span>
        <strong class="metric-value">${formatPercentile(snapshot[metric.percentileKey])}</strong>
      </div>
    </div>
  `;

  valuationHistorySummary.querySelectorAll("[data-valuation-metric]").forEach((button) => {
    button.addEventListener("click", () => {
      renderValuationHistory(latestValuationHistorySnapshot, button.dataset.valuationMetric);
    });
  });

  const series = points
    .filter((point) => point[metric.pointKey] !== null && point[metric.pointKey] !== undefined)
    .map((point) => ({
      date: point.date,
      value: Number(point[metric.pointKey]),
    }));

  if (!series.length) {
    valuationHistoryChart.className = "valuation-history-chart empty-state";
    valuationHistoryChart.textContent = `暂无可绘制的 ${metric.chartLabel} 历史曲线`;
    return;
  }

  const width = 960;
  const height = 300;
  const padding = { top: 38, right: 58, bottom: 44, left: 58 };
  const currentValue = snapshot[metric.currentKey] ?? series[series.length - 1].value;
  const medianValue = snapshot[metric.medianKey];
  const chartValues = [
    ...series.map((point) => point.value),
    currentValue,
    medianValue,
  ].filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)));
  const minValue = Math.min(...chartValues);
  const maxValue = Math.max(...chartValues);
  const range = maxValue - minValue || 1;
  const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

  const xAt = (index) =>
    padding.left +
    (index / Math.max(series.length - 1, 1)) * (width - padding.left - padding.right);
  const yAt = (value) =>
    height -
    padding.bottom -
    ((value - minValue) / range) * (height - padding.top - padding.bottom);

  const polylinePoints = series
    .map((point, index) => `${xAt(index).toFixed(1)},${yAt(point.value).toFixed(1)}`)
    .join(" ");

  const currentY = yAt(currentValue);
  const medianY = medianValue !== null && medianValue !== undefined ? yAt(medianValue) : null;
  let currentLabelY = clamp(currentY - 6, 13, height - padding.bottom - 8);
  let medianLabelY = medianY === null ? null : clamp(medianY - 6, 13, height - padding.bottom - 8);
  if (medianLabelY !== null && Math.abs(currentLabelY - medianLabelY) < 18) {
    if (currentY <= medianY) {
      currentLabelY = clamp(currentY - 10, 13, height - padding.bottom - 8);
      medianLabelY = clamp(medianY + 16, 13, height - padding.bottom - 8);
    } else {
      currentLabelY = clamp(currentY + 16, 13, height - padding.bottom - 8);
      medianLabelY = clamp(medianY - 10, 13, height - padding.bottom - 8);
    }
  }
  const tickCount = 5;
  const yTicks = Array.from({ length: tickCount }, (_, index) => {
    const value = minValue + (range * index) / (tickCount - 1);
    const y = yAt(value);
    return `
      <line class="chart-grid" x1="${padding.left}" y1="${y.toFixed(1)}" x2="${width - padding.right}" y2="${y.toFixed(1)}"></line>
      <text class="chart-y-label" x="${padding.left - 10}" y="${(y + 4).toFixed(1)}" text-anchor="end">${formatNumber(value)}</text>
    `;
  }).join("");

  const labelInterval = Math.max(1, Math.ceil(series.length / 12));
  const labels = series.map((point, index) => {
    const shouldShowLabel =
      series.length <= 16 ||
      index === 0 ||
      index === series.length - 1 ||
      index % labelInterval === 0;
    if (!shouldShowLabel) {
      return "";
    }
    const year = point.date ? String(point.date).slice(0, 4) : "";
    return `<text x="${xAt(index).toFixed(1)}" y="${height - 10}" text-anchor="middle">${year}</text>`;
  }).join("");

  const valueLabels = series
    .map((point, index) => {
      const shouldShowValue =
        series.length <= 16 ||
        index === 0 ||
        index === series.length - 1 ||
        index % labelInterval === 0;
      if (!shouldShowValue) {
        return "";
      }
      const x = xAt(index);
      const rawY = yAt(point.value) + (index % 2 === 0 ? -10 : 17);
      const y = clamp(rawY, 13, height - padding.bottom - 8);
      return `<text class="chart-point-label" x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="middle">${formatNumber(point.value, 1)}</text>`;
    })
    .join("");

  const dots = series
    .map(
      (point, index) => `
        <circle cx="${xAt(index).toFixed(1)}" cy="${yAt(point.value).toFixed(1)}" r="3.5">
          <title>${point.date ?? "-"} ${metric.chartLabel}: ${formatNumber(point.value, 2)}</title>
        </circle>
      `
    )
    .join("");

  const medianLine = medianY === null
    ? ""
    : `<line class="chart-guide" x1="${padding.left}" y1="${medianY.toFixed(1)}" x2="${width - padding.right}" y2="${medianY.toFixed(1)}"></line>
       <text class="chart-guide-label" x="${width - padding.right}" y="${medianLabelY.toFixed(1)}" text-anchor="end">历史中位数 ${formatNumber(medianValue)}</text>`;

  valuationHistoryChart.className = "valuation-history-chart";
  valuationHistoryChart.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="历史 ${metric.chartLabel} 曲线">
      ${yTicks}
      <line class="chart-axis" x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}"></line>
      <line class="chart-axis" x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${height - padding.bottom}"></line>
      ${medianLine}
      <polyline class="chart-line" fill="none" points="${polylinePoints}"></polyline>
      ${dots}
      ${valueLabels}
      <line class="chart-current" x1="${padding.left}" y1="${currentY.toFixed(1)}" x2="${width - padding.right}" y2="${currentY.toFixed(1)}"></line>
      <text class="chart-current-label" x="${width - padding.right}" y="${currentLabelY.toFixed(1)}" text-anchor="end">当前 ${formatNumber(currentValue)}</text>
      ${labels}
    </svg>
  `;
}

function renderFinancials(periods, currency) {
  if (!periods || !periods.length) {
    financialTrendSummary.innerHTML = '<p class="empty-state">没有足够的季度数据来判断趋势。</p>';
    financialTableBody.innerHTML = '<tr><td colspan="9" class="empty-cell">没有历史财务数据</td></tr>';
    return;
  }

  const latest = periods[0];
  financialTrendSummary.innerHTML = [
    {
      label: "最新季度收入趋势",
      qoq: latest.revenue_qoq_growth,
      yoy: latest.revenue_yoy_growth,
      description: describeTrend(
        latest.revenue_yoy_growth ?? latest.revenue_qoq_growth,
        "业务规模"
      ),
    },
    {
      label: "最新季度 EPS 趋势",
      qoq: latest.diluted_eps_qoq_growth,
      yoy: latest.diluted_eps_yoy_growth,
      description: describeTrend(
        latest.diluted_eps_yoy_growth ?? latest.diluted_eps_qoq_growth,
        "单股盈利"
      ),
    },
    {
      label: "最新季度净利润趋势",
      qoq: latest.net_income_qoq_growth,
      yoy: latest.net_income_yoy_growth,
      description: describeTrend(
        latest.net_income_yoy_growth ?? latest.net_income_qoq_growth,
        "利润表现"
      ),
    },
  ]
    .map(
      (item) => `
        <div class="trend-chip">
          <span>${item.label}</span>
          <div class="trend-values">
            <div class="trend-value-row">
              <em>环比</em>
              <strong class="${trendClass(item.qoq)}">${formatPercent(item.qoq)}</strong>
            </div>
            <div class="trend-value-row">
              <em>同比</em>
              <strong class="${trendClass(item.yoy)}">${formatPercent(item.yoy)}</strong>
            </div>
          </div>
          <p>${item.description}</p>
        </div>
      `
    )
    .join("");

  const yoyCoverage = periods.filter(
    (period) =>
      period.revenue_yoy_growth !== null ||
      period.diluted_eps_yoy_growth !== null ||
      period.net_income_yoy_growth !== null
  ).length;

  if (yoyCoverage <= 1 && periods.length <= 5) {
    financialTrendSummary.innerHTML += `
      <p class="financial-trend-note">
        当前 FMP 返回的季度历史仍然较短，因此同比数据可能只覆盖最新季度；其余季度可优先参考环比变化判断短期经营趋势。
      </p>
    `;
  }

  financialTableBody.innerHTML = periods
    .map(
      (period) => `
        <tr>
          <td>${period.period_end ?? "-"}</td>
          <td>${formatCompactCurrency(period.revenue, currency)}</td>
          <td class="${trendClass(period.revenue_qoq_growth)}">${formatPercent(period.revenue_qoq_growth)}</td>
          <td class="${trendClass(period.revenue_yoy_growth)}">${formatPercent(period.revenue_yoy_growth)}</td>
          <td>${formatNumber(period.diluted_eps)}</td>
          <td class="${trendClass(period.diluted_eps_qoq_growth)}">${formatPercent(period.diluted_eps_qoq_growth)}</td>
          <td class="${trendClass(period.diluted_eps_yoy_growth)}">${formatPercent(period.diluted_eps_yoy_growth)}</td>
          <td class="${trendClass(period.net_income_qoq_growth)}">${formatPercent(period.net_income_qoq_growth)}</td>
          <td class="${trendClass(period.net_income_yoy_growth)}">${formatPercent(period.net_income_yoy_growth)}</td>
        </tr>
      `
    )
    .join("");
}

function renderEarningsEvents(events) {
  if (!events || !events.length) {
    earningsEvents.innerHTML = '<p class="empty-state">没有近期财报 surprise 数据。</p>';
    return;
  }

  earningsEvents.innerHTML = events
    .map(
      (event) => `
        <article class="event-item ${event.status ?? ""}">
          <strong>${event.date ?? "-"}</strong>
          <p>结果: ${event.status ?? "-"}</p>
          <p>预期 EPS: ${formatNumber(event.eps_estimate)}</p>
          <p>${event.reported_eps_label ?? "实际 EPS"}: ${formatNumber(event.reported_eps)}</p>
          <p>Surprise: ${formatSurprisePercent(event.surprise_pct)}</p>
        </article>
      `
    )
    .join("");
}

function renderAnalysis(data) {
  const currency = data.company.currency || "USD";

  companyName.textContent = data.company.name || "未知公司";
  companySymbol.textContent = data.company.symbol || "-";
  companyIndustry.textContent = data.company.industry || "-";
  companySector.textContent = data.company.sector || "-";
  companyMarketCap.textContent = formatCompactCurrency(data.company.market_cap, currency);

  setAssessmentBadge(data.assessment.label);
  assessmentSummary.textContent = `系统判断为 ${data.assessment.label}，以自身历史估值分位为主，结合成长质量、财报执行力和低权重同行参考给出当前结论。`;
  valuationScore.textContent = formatNumber(data.assessment.valuation_score, 0);
  executionScore.textContent = formatNumber(data.assessment.execution_score, 0);
  growthScore.textContent = formatNumber(data.assessment.growth_score, 0);
  renderRationale(data.assessment.rationale);

  renderMetricList(marketMetrics, [
    { label: "最新股价", value: formatCurrency(data.market.price, currency) },
    { label: "前一交易日收盘价", value: formatCurrency(data.market.previous_close, currency) },
    { label: "5日均价", value: formatCurrency(data.market.average_price_5d, currency) },
    { label: "30日均价", value: formatCurrency(data.market.average_price_30d, currency) },
    { label: "Trailing P/E", value: formatNumber(data.market.trailing_pe) },
    { label: "Forward P/E", value: formatNumber(data.market.forward_pe) },
    { label: "Trailing EPS", value: formatNumber(data.market.trailing_eps) },
    { label: "Forward EPS", value: formatNumber(data.market.forward_eps) },
    { label: "PEG", value: formatNumber(data.market.peg_ratio) },
    { label: "EV / EBITDA", value: formatNumber(data.market.enterprise_to_ebitda) },
    {
      label: "52 周区间",
      value: `${formatCurrency(data.market.week_52_low, currency)} - ${formatCurrency(data.market.week_52_high, currency)}`,
    },
  ]);
  renderPriceSimulation(data, currency);

  renderMetricList(forecastMetrics, [
    { label: "本年 EPS", value: formatNumber(data.forecast.current_year_eps) },
    { label: "明年 EPS", value: formatNumber(data.forecast.next_year_eps) },
    { label: "本年收入", value: formatCompactCurrency(data.forecast.current_year_revenue, currency) },
    { label: "明年收入", value: formatCompactCurrency(data.forecast.next_year_revenue, currency) },
    { label: "EPS 增长", value: formatPercent(data.forecast.earnings_growth) },
    { label: "收入增长", value: formatPercent(data.forecast.revenue_growth) },
  ]);

  renderMetricList(earningsMetrics, [
    { label: "财报样本数", value: formatNumber(data.earnings_execution.observations, 0) },
    { label: "Beat 次数", value: formatNumber(data.earnings_execution.beat_count, 0) },
    { label: "Miss 次数", value: formatNumber(data.earnings_execution.miss_count, 0) },
    { label: "Meet 次数", value: formatNumber(data.earnings_execution.meet_count, 0) },
    { label: "Beat Rate", value: formatPercent(data.earnings_execution.beat_rate) },
    { label: "平均 Surprise", value: formatSurprisePercent(data.earnings_execution.average_surprise_pct) },
  ]);

  renderValuationHistory(data.valuation_history);
  renderPeers(data.peers);
  renderFinancials(data.financial_history, currency);
  renderEarningsEvents(data.earnings_execution.recent_events);
}

async function handleSubmit(event) {
  event.preventDefault();
  const symbol = symbolInput.value.trim().toUpperCase();
  const peerCount = Number(peerCountInput.value || 5);

  if (!symbol) {
    setStatus("请输入有效的股票代码。", "error");
    symbolInput.focus();
    return;
  }

  submitButton.disabled = true;
  clearLogs();
  setStatus(`正在分析 ${symbol}，这会拉取最新市场与财务数据。`, "loading");
  appendLog(`已提交 ${symbol} 的分析请求。`, "start", new Date().toLocaleTimeString("zh-CN", { hour12: false }));

  if (activeEventSource) {
    activeEventSource.close();
  }

  activeEventSource = new EventSource(
    `/analyze-stream/${encodeURIComponent(symbol)}?peer_count=${peerCount}`
  );

  activeEventSource.addEventListener("log", (event) => {
    const payload = JSON.parse(event.data);
    appendLog(payload.message, payload.level, payload.timestamp);
    if (payload.level === "error") {
      setStatus(payload.message, "error");
    } else {
      setStatus(payload.message, "loading");
    }
  });

  activeEventSource.addEventListener("result", (event) => {
    const payload = JSON.parse(event.data);
    renderAnalysis(payload);
    appendLog("估值结果已返回，前端正在刷新各模块。", "success", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    setStatus(`${symbol} 分析完成，已更新估值、同行与财报执行看板。`, "success");
  });

  activeEventSource.addEventListener("analysis-error", (event) => {
    let message = "分析过程中出现错误。";
    try {
      const payload = JSON.parse(event.data);
      message = payload.message || message;
    } catch (_err) {
      // EventSource 连接异常时 event.data 不一定存在。
    }
    appendLog(message, "error", new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    setStatus(message, "error");
    submitButton.disabled = false;
    if (activeEventSource) {
      activeEventSource.close();
      activeEventSource = null;
    }
  });

  activeEventSource.onerror = () => {
    appendLog(
      "实时日志连接中断，请检查服务是否仍在运行，然后刷新页面重试。",
      "error",
      new Date().toLocaleTimeString("zh-CN", { hour12: false })
    );
    setStatus("实时日志连接中断，请检查服务状态。", "error");
    submitButton.disabled = false;
    if (activeEventSource) {
      activeEventSource.close();
      activeEventSource = null;
    }
  };

  activeEventSource.addEventListener("done", () => {
    submitButton.disabled = false;
    if (activeEventSource) {
      activeEventSource.close();
      activeEventSource = null;
    }
  });
}

form.addEventListener("submit", handleSubmit);
statusLogCard.addEventListener("toggle", syncStatusToggleLabel);
syncStatusToggleLabel();
