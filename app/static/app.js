const API = "/api/v1";
const $ = (selector) => document.querySelector(selector);

const state = {
  status: null,
  account: null,
  performance: null,
  marketSymbol: "005930",
  marketInterval: "1d",
  marketName: "-",
  marketDayOpen: null,
  marketPreviousClose: null,
  chartCandles: [],
  chartQuote: null,
  chartVisibleCount: 250,
  marketSocket: null,
  marketSocketGeneration: 0,
  marketLive: false,
  recommendationLevels: {},
  chartCollapsed: false,
  reportPeriod: "daily",
  reportLookup: {},
};
let toastTimer;
let marketRefreshInFlight = false;
let chartRenderPending = false;

function applyTheme(theme) {
  const selected = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = selected;
  const isLight = selected === "light";
  $("#themeIcon").textContent = isLight ? "☀" : "☾";
  $("#themeLabel").textContent = isLight ? "LIGHT" : "DARK";
  $("#themeToggle").setAttribute("aria-pressed", String(isLight));
  try { localStorage.setItem("trader-theme", selected); } catch {}
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.detail || `요청 실패 (${response.status})`);
  return payload;
}

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, 3000);
}

function number(value, currency) {
  const numeric = Number(value || 0);
  return new Intl.NumberFormat("ko-KR", {
    maximumFractionDigits: currency === "KRW" ? 0 : 4,
  }).format(numeric);
}

function money(value, currency) {
  return `${number(value, currency)} ${currency}`;
}

function marketCap(value) {
  const amount = Number(value || 0);
  if (amount >= 1e12) return `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(amount / 1e12)}조 원`;
  return `${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(amount / 1e8)}억 원`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function setTossConnection(connected, checking = false) {
  const button = $("#tossConnectionButton");
  if (checking) {
    button.textContent = "API 확인 중";
    button.className = "button status-button status-checking";
    return;
  }
  button.textContent = connected ? "연결 성공" : "연결실패";
  button.className = `button status-button ${connected ? "status-connected" : "status-failed"}`;
}

async function checkTossConnection(showToast = true) {
  setTossConnection(false, true);
  try {
    const status = state.status || await request(`${API}/system/status`);
    if (status.market_data !== "toss") throw new Error("토스 API가 설정되지 않았습니다.");
    await request(`${API}/market/lookup?symbol=${encodeURIComponent(state.marketSymbol)}`);
    setTossConnection(true);
    if (showToast) toast("토스 API 연결에 성공했습니다.");
    return true;
  } catch (error) {
    setTossConnection(false);
    if (showToast) toast(error.message, true);
    return false;
  }
}

function renderStatus(status, risk) {
  state.status = status;
  $("#engineLabel").textContent = status.running ? "자동매매 실행 중" : "자동매매 중지됨";
  $("#engineDetail").textContent = status.last_error
    ? `오류: ${status.last_error}`
    : `v${status.version} · 전략 ${status.strategy_count}개 · ${status.market_data.toUpperCase()} 시세`;
  $("#startButton").disabled = status.running || status.kill_switch;
  $("#stopButton").disabled = !status.running;
  $("#killButton").disabled = status.kill_switch;
  $("#clearKillButton").disabled = !status.kill_switch;
  $("#killSwitchLabel").textContent = status.kill_switch ? "긴급 중지" : "정상";
  $("#killSwitchLabel").className = `metric-value small ${status.kill_switch ? "negative" : "positive"}`;
  $("#marketMode").textContent = status.market_data.toUpperCase();
  $("#marketMode").className = `badge ${status.market_data === "toss" ? "badge-ok" : "badge-muted"}`;
  $("#marketDescription").textContent = status.market_data === "toss"
    ? "토스증권 REST API에서 활성 전략 종목의 실제 현재가를 조회합니다."
    : "토스 API 키가 없어 수동 테스트 시세 모드로 실행 중입니다.";
  $("#tossRefreshButton").disabled = status.market_data !== "toss";
  const isPaper = status.mode === "paper";
  $("#tradingModeButton").textContent = isPaper ? "PAPER" : "실계좌 주문";
  $("#tradingModeButton").className = `button status-button ${isPaper ? "status-paper" : "status-live"}`;
  if (status.market_data !== "toss") setTossConnection(false);
  if (risk) $("#killSwitchLabel").title = `KRW 주문 한도 ${risk.max_order_amount.KRW}`;
}

function renderAccount(account, performance) {
  state.account = account;
  state.performance = performance;
  $("#krwCash").textContent = money(account.cash.KRW, "KRW");
  $("#usdCash").textContent = money(account.cash.USD, "USD");
  $("#krwEquity").textContent = money(account.total_equity.KRW, "KRW");
  $("#usdEquity").textContent = money(account.total_equity.USD, "USD");
  for (const currency of ["KRW", "USD"]) {
    const target = $(`#${currency.toLowerCase()}Return`);
    const row = performance.by_currency[currency];
    const rate = Number(row.return_rate_percent);
    target.textContent = `${rate >= 0 ? "+" : ""}${rate.toFixed(2)}% · 손익 ${money(row.profit_loss, currency)}`;
    target.className = `metric-change ${rate > 0 ? "positive" : rate < 0 ? "negative" : ""}`;
  }
  $("#filledOrders").textContent = performance.orders.filled;
  $("#rejectedOrders").textContent = performance.orders.rejected;
  renderPositions(account.positions);
}

function renderCapital(account, risk, status) {
  const isPaper = status.mode === "paper";
  const accountAmount = Number(account.total_equity.KRW || 0);
  const availableCash = Number(account.cash.KRW || 0);
  const tradeRatio = Number(risk.recommended_trade_ratio ?? 0.5);
  const recommendationAmount = Math.min(availableCash, accountAmount * tradeRatio);
  const ratioPercent = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(tradeRatio * 100);
  $("#capitalTitle").textContent = isPaper ? "가상계좌 주문 자금" : "실계좌 주문 자금";
  $("#capitalAccountLabel").textContent = isPaper ? "가상계좌 평가금액" : "실계좌 평가금액";
  $("#capitalMode").textContent = isPaper ? "PAPER" : "REAL";
  $("#capitalMode").className = `badge ${isPaper ? "badge-paper" : "status-live"}`;
  $("#capitalAccountAmount").textContent = money(accountAmount, "KRW");
  $("#capitalRecommendationAmount").textContent = money(recommendationAmount, "KRW");
  $("#capitalRecommendationHelp").textContent = `계좌 평가금액의 ${ratioPercent}% 이내에서 가용 현금 사용`;
}

function renderRecommendations(payload) {
  const container = $("#recommendationResults");
  container.hidden = false;
  $("#recommendationBudget").textContent = `사용 금액 ${money(payload.budget, "KRW")}`;
  $("#recommendationDisclaimer").textContent = payload.disclaimer;
  const funnel = payload.funnel || {};
  $("#recommendationFunnel").textContent = `전체 ${number(funnel.universe || 0, "KRW")}개 → 예산·유동성 ${number(funnel.budget_liquidity || 0, "KRW")}개 → 위험 제외 ${number(funnel.risk_filtered || 0, "KRW")}개 → 지표 분석 ${number(funnel.analyzed || 0, "KRW")}개`;
  state.recommendationLevels = Object.fromEntries(
    payload.candidates.map((item) => [item.symbol, {
      daily_support: item.daily_support,
      daily_resistance: item.daily_resistance,
      weekly_support: item.weekly_support,
      weekly_resistance: item.weekly_resistance,
    }]),
  );
  $("#recommendationList").innerHTML = payload.candidates.length
    ? payload.candidates.map((item, index) => `<article class="recommendation-card">
        <div class="recommendation-rank">#${index + 1} · <strong>${item.score}점</strong></div>
        <h3 title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</h3>
        <span class="recommendation-symbol">${escapeHtml(item.symbol)}</span>
        <div class="recommendation-price">${money(item.price, item.currency)}</div>
        <div class="recommendation-metrics">
          <div>시가총액 ${marketCap(item.market_cap)}</div>
          <div>일봉 지지 ${number(item.daily_support, item.currency)} · 저항 ${number(item.daily_resistance, item.currency)}</div>
          <div>주봉 지지 ${number(item.weekly_support, item.currency)} · 저항 ${number(item.weekly_resistance, item.currency)}</div>
          <div>일봉 지지 거리 ${item.support_distance_percent}% · 저항 여력 ${item.daily_resistance_upside_percent}%</div>
          <div>MA5 ${number(item.ma5, item.currency)}</div>
          <div>MA20 ${number(item.ma20, item.currency)}</div>
          <div>RSI ${item.rsi} · 거래량 ${item.volume_ratio}배</div>
        </div>
        <p class="recommendation-reason">${escapeHtml(item.reason)}</p>
        <div class="recommendation-quantity">추천 금액으로 최대 ${number(item.quantity, "KRW")}주</div>
        <button class="button button-secondary recommendation-view" type="button" data-candidate-symbol="${escapeHtml(item.symbol)}">차트에서 보기</button>
      </article>`).join("")
    : '<p class="empty">현재 조건에 맞는 추천 후보가 없습니다.</p>';
  container.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

const REPORT_FAVORITES_KEY = "paper-trader-report-favorites";

function reportFavorites() {
  try { return JSON.parse(localStorage.getItem(REPORT_FAVORITES_KEY) || "[]"); }
  catch { return []; }
}

function saveReportFavorites(items) {
  localStorage.setItem(REPORT_FAVORITES_KEY, JSON.stringify(items));
  renderReportFavorites();
}

function isReportFavorite(item) {
  return reportFavorites().some((saved) => saved.url === item.url && saved.title === item.title);
}

function renderWeeklyIssues(selector, issues, scope) {
  issues.forEach((item, index) => { state.reportLookup[`${scope}-${index}`] = item; });
  $(selector).innerHTML = issues.length
    ? issues.map((item, index) => `<div class="weekly-issue">
        <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer"><strong>${escapeHtml(item.title)}</strong></a>
        <small>${escapeHtml(item.source || "출처 미상")} · ${new Date(item.published_at).toLocaleDateString("ko-KR")}</small>
        <button class="weekly-bookmark ${isReportFavorite(item) ? "saved" : ""}" type="button" data-report-bookmark="${scope}-${index}" aria-label="즐겨찾기">★</button>
      </div>`).join("")
    : '<p class="empty">해당 기간의 이슈를 불러오지 못했습니다.</p>';
}

function renderReportFavorites() {
  const items = reportFavorites();
  $("#reportFavorites").innerHTML = items.length
    ? items.map((item, index) => `<div class="favorite-item">
        <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title)}</a>
        <button class="favorite-delete" type="button" data-favorite-delete="${index}">삭제</button>
      </div>`).join("")
    : '<p class="empty">즐겨찾기한 이슈가 없습니다.</p>';
}

function renderWeeklyReport(report) {
  const labels = { risk_on: "위험선호", neutral: "중립", defensive: "방어" };
  const direction = labels[report.direction] || "중립";
  const badge = $("#weeklyDirection");
  badge.textContent = `${direction} · ${report.sentiment_score > 0 ? "+" : ""}${report.sentiment_score}`;
  badge.className = `badge ${report.direction === "risk_on" ? "status-live" : report.direction === "defensive" ? "status-failed" : "badge-muted"}`;
  const periodLabel = report.period === "daily" ? "일간" : "주간";
  $("#weeklyReportPeriod").textContent = `${periodLabel} 리포트 · ${new Date(report.generated_at).toLocaleString("ko-KR")} 작성 · 다음 갱신 ${new Date(report.next_refresh_at).toLocaleString("ko-KR")}`;
  $("#weeklyGuidance").textContent = report.guidance;
  $("#weeklySourceNote").textContent = report.source_note;
  state.reportLookup = {};
  renderWeeklyIssues("#globalIssues", report.global || [], "global");
  renderWeeklyIssues("#domesticIssues", report.domestic || [], "domestic");
  renderReportFavorites();
}

async function loadWeeklyReport() {
  try {
    renderWeeklyReport(await request(`${API}/weekly-report?period=${state.reportPeriod}`));
  } catch (error) {
    $("#weeklyGuidance").textContent = "시장 리포트를 불러오지 못했습니다. 추천에는 중립 방향을 적용합니다.";
    $("#weeklySourceNote").textContent = error.message;
  }
}

function renderPositions(positions) {
  $("#positionCount").textContent = positions.length;
  $("#positionsEmpty").hidden = positions.length > 0;
  $("#positionsTable").innerHTML = positions.map((position) => {
    const pnl = Number(position.unrealized_profit_loss);
    return `<tr>
      <td><strong>${escapeHtml(position.symbol)}</strong><br><span class="muted">${position.currency}</span></td>
      <td>${number(position.quantity, position.currency)}</td>
      <td>${number(position.average_price, position.currency)}</td>
      <td>${number(position.market_price, position.currency)}</td>
      <td class="${pnl > 0 ? "positive" : pnl < 0 ? "negative" : ""}">${number(pnl, position.currency)}</td>
    </tr>`;
  }).join("");
}

function renderOrders(orders) {
  $("#ordersEmpty").hidden = orders.length > 0;
  $("#ordersTable").innerHTML = orders.slice(0, 30).map((order) => `<tr>
    <td>${new Date(order.created_at).toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
    <td><strong>${escapeHtml(order.symbol)}</strong></td>
    <td class="${order.side === "BUY" ? "positive" : "negative"}">${order.side === "BUY" ? "매수" : "매도"}</td>
    <td>${number(order.quantity, order.currency)}</td>
    <td>${order.filled_price ? number(order.filled_price, order.currency) : "-"}</td>
    <td>${order.status === "FILLED" ? "체결" : `거부 · ${escapeHtml(order.reason || "")}`}</td>
  </tr>`).join("");
}

function renderStrategies(strategies) {
  $("#strategyList").innerHTML = strategies.length ? strategies.map((item) => `<div class="strategy-item">
    <div><strong>${escapeHtml(item.strategy_id)}</strong><span>${escapeHtml(item.symbol)} · ${item.buy_below} 이하 매수 / ${item.sell_above} 이상 매도</span></div>
    <div class="strategy-actions">
      <span class="badge ${item.enabled ? "badge-ok" : "badge-muted"}">${item.enabled ? "ON" : "OFF"}</span>
      <button class="mini-button" data-strategy="${escapeHtml(item.strategy_id)}" data-enable="${!item.enabled}">${item.enabled ? "끄기" : "켜기"}</button>
    </div>
  </div>`).join("") : '<p class="empty">등록된 전략이 없습니다.</p>';
}

function renderQuotes(quotes) {
  $("#quotesList").innerHTML = quotes.length ? quotes.map((quote) => `<div class="quote-item">
    <div><strong>${escapeHtml(quote.symbol)}</strong><span>${escapeHtml(quote.source)} · ${new Date(quote.timestamp).toLocaleTimeString("ko-KR")}</span></div>
    <strong>${money(quote.price, quote.currency)}</strong>
  </div>`).join("") : '<p class="empty">수신한 시세가 없습니다.</p>';
}

function svgElement(name, attributes = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  return element;
}

function renderChart(candles, quote) {
  const grid = $("#chartGrid");
  const levelLayer = $("#chartLevels");
  const volumeLayer = $("#chartVolumeBars");
  const movingAverageLayer = $("#chartMovingAverages");
  const candleLayer = $("#chartCandles");
  const axis = $("#chartAxis");
  grid.replaceChildren();
  levelLayer.replaceChildren();
  volumeLayer.replaceChildren();
  movingAverageLayer.replaceChildren();
  candleLayer.replaceChildren();
  axis.replaceChildren();

  const allCandles = [...candles].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
  const visibleCount = Math.min(state.chartVisibleCount, allCandles.length);
  const sorted = allCandles.slice(-visibleCount);
  $("#chartEmpty").hidden = allCandles.length > 0;
  if (!allCandles.length) return;

  const width = 1000;
  const height = 360;
  const padding = { top: 18, right: 82, bottom: 34, left: 10 };
  const plotWidth = width - padding.left - padding.right;
  const priceBottom = 260;
  const plotHeight = priceBottom - padding.top;
  const volumeTop = 278;
  const volumeBottom = height - padding.bottom;
  const volumeHeight = volumeBottom - volumeTop;
  const lows = sorted.map((item) => Number(item.low_price));
  const highs = sorted.map((item) => Number(item.high_price));
  let low = Math.min(...lows);
  let high = Math.max(...highs);
  const rawRange = high - low;
  const pricePadding = rawRange > 0 ? rawRange * 0.08 : Math.max(high * 0.01, 1);
  low -= pricePadding;
  high += pricePadding;
  const range = high - low;
  const y = (price) => padding.top + ((high - price) / range) * plotHeight;

  if (["1d", "1w"].includes(state.marketInterval)) {
    const levels = state.recommendationLevels[state.marketSymbol];
    if (levels) {
      const support = Number(state.marketInterval === "1d" ? levels.daily_support : levels.weekly_support);
      const resistance = Number(state.marketInterval === "1d" ? levels.daily_resistance : levels.weekly_resistance);
      for (const [kind, value, title] of [["support", support, "지지"], ["resistance", resistance, "저항"]]) {
        if (!Number.isFinite(value) || value < low || value > high) continue;
        const yPosition = y(value);
        levelLayer.append(svgElement("line", {
          x1: padding.left, y1: yPosition, x2: width - padding.right, y2: yPosition,
          class: `chart-level-line ${kind}`,
        }));
        const label = svgElement("text", {
          x: padding.left + 8, y: yPosition - 5, class: `chart-level-label ${kind}`,
        });
        label.textContent = `${title} ${number(value, quote.currency)}`;
        levelLayer.append(label);
      }
    }
  }

  for (let index = 0; index <= 4; index += 1) {
    const yPosition = padding.top + (plotHeight / 4) * index;
    const price = high - (range / 4) * index;
    grid.append(svgElement("line", {
      x1: padding.left, y1: yPosition, x2: width - padding.right, y2: yPosition, class: "chart-grid-line",
    }));
    const label = svgElement("text", {
      x: width - padding.right + 9, y: yPosition + 4, class: "chart-axis-label",
    });
    label.textContent = number(price, quote.currency);
    axis.append(label);
  }

  const slot = plotWidth / Math.max(sorted.length, 1);
  const bodyWidth = Math.max(2.5, Math.min(12, slot * 0.58));
  const showMovingAverages = state.marketInterval === "1d";
  $("#movingAverageLegend").hidden = !showMovingAverages;
  if (showMovingAverages) {
    const visibleStart = allCandles.length - sorted.length;
    for (const period of [5, 20, 60, 120]) {
      const points = [];
      for (let absoluteIndex = Math.max(period - 1, visibleStart); absoluteIndex < allCandles.length; absoluteIndex += 1) {
        const window = allCandles.slice(absoluteIndex - period + 1, absoluteIndex + 1);
        const average = window.reduce((sum, item) => sum + Number(item.close_price), 0) / period;
        const visibleIndex = absoluteIndex - visibleStart;
        points.push(`${padding.left + slot * visibleIndex + slot / 2},${y(average)}`);
      }
      if (points.length > 1) {
        movingAverageLayer.append(svgElement("polyline", {
          points: points.join(" "), class: `moving-average-line ma${period}`,
        }));
      }
    }
  }
  const maxVolume = Math.max(...sorted.map((item) => Number(item.volume) || 0), 1);
  sorted.forEach((item, index) => {
    const open = Number(item.open_price);
    const close = Number(item.close_price);
    const x = padding.left + slot * index + slot / 2;
    const className = close > open ? "candle-up" : close < open ? "candle-down" : "candle-flat";
    candleLayer.append(svgElement("line", {
      x1: x, y1: y(Number(item.high_price)), x2: x, y2: y(Number(item.low_price)), class: `candle-wick ${className}`,
    }));
    const bodyTop = Math.min(y(open), y(close));
    candleLayer.append(svgElement("rect", {
      x: x - bodyWidth / 2,
      y: bodyTop,
      width: bodyWidth,
      height: Math.max(1.8, Math.abs(y(open) - y(close))),
      rx: 1,
      class: className,
    }));
    const volume = Number(item.volume) || 0;
    const volumeBarHeight = Math.max(1, volume / maxVolume * volumeHeight);
    volumeLayer.append(svgElement("rect", {
      x: x - bodyWidth / 2,
      y: volumeBottom - volumeBarHeight,
      width: bodyWidth,
      height: volumeBarHeight,
      rx: 1,
      class: `volume-bar ${className}`,
    }));
  });

  grid.append(svgElement("line", {
    x1: padding.left, y1: volumeTop - 8, x2: width - padding.right, y2: volumeTop - 8, class: "chart-volume-divider",
  }));
  const volumeLabel = svgElement("text", {
    x: padding.left + 4, y: volumeTop + 10, class: "chart-volume-label",
  });
  volumeLabel.textContent = "거래량";
  axis.append(volumeLabel);

  const timeIndexes = [...new Set([0, Math.floor((sorted.length - 1) / 2), sorted.length - 1])];
  timeIndexes.forEach((index) => {
    const item = sorted[index];
    const label = svgElement("text", {
      x: padding.left + slot * index + slot / 2,
      y: height - 8,
      "text-anchor": index === 0 ? "start" : index === sorted.length - 1 ? "end" : "middle",
      class: "chart-axis-label",
    });
    const date = new Date(item.timestamp);
    label.textContent = ["1d", "1w", "1M"].includes(state.marketInterval)
      ? date.toLocaleDateString("ko-KR", { month: "2-digit", day: "2-digit" })
      : date.toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
    axis.append(label);
  });

  const latest = sorted.at(-1);
  const first = sorted[0];
  const dayOpen = Number(state.marketDayOpen ?? first.open_price);
  const previousClose = Number(state.marketPreviousClose ?? dayOpen);
  const change = Number(quote.price) - previousClose;
  const changeRate = previousClose ? change / previousClose * 100 : 0;
  $("#chartSymbol").textContent = quote.symbol;
  $("#chartSource").textContent = quote.source.toUpperCase();
  $("#chartSource").className = `badge ${quote.source === "toss" ? "badge-ok" : "badge-muted"}`;
  $("#chartName").textContent = state.marketName;
  $("#chartPrice").textContent = number(quote.price, quote.currency);
  $("#chartCurrency").textContent = quote.currency;
  $("#chartChange").textContent = `${change >= 0 ? "+" : ""}${number(change, quote.currency)} (${changeRate >= 0 ? "+" : ""}${changeRate.toFixed(2)}%)`;
  $("#chartChange").className = `chart-change ${change > 0 ? "positive" : change < 0 ? "negative" : ""}`;
  $("#chartOpen").textContent = number(dayOpen, quote.currency);
  $("#chartHigh").textContent = number(latest.high_price, quote.currency);
  $("#chartLow").textContent = number(latest.low_price, quote.currency);
  $("#chartVolume").textContent = number(latest.volume, quote.currency);
  $("#chartUpdated").textContent = new Date(quote.timestamp).toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function candleBucket(timestamp, interval) {
  const date = new Date(timestamp);
  date.setMilliseconds(0);
  if (["1m", "5m", "15m", "50m", "60m"].includes(interval)) {
    const minutes = Number(interval.slice(0, -1));
    const minutesSinceMidnight = date.getHours() * 60 + date.getMinutes();
    const bucketMinutes = Math.floor(minutesSinceMidnight / minutes) * minutes;
    date.setHours(0, bucketMinutes, 0, 0);
  } else if (interval === "1d") {
    date.setHours(0, 0, 0, 0);
  } else if (interval === "1w") {
    const daysFromMonday = (date.getDay() + 6) % 7;
    date.setDate(date.getDate() - daysFromMonday);
    date.setHours(0, 0, 0, 0);
  } else if (interval === "1M") {
    date.setDate(1);
    date.setHours(0, 0, 0, 0);
  }
  return date;
}

function applyRealtimeTrade(trade) {
  if (trade.symbol !== state.marketSymbol || !state.chartQuote) return;
  const price = Number(trade.price);
  const volume = Number(trade.volume) || 0;
  const bucket = candleBucket(trade.timestamp, state.marketInterval);
  const latest = state.chartCandles.at(-1);
  const latestBucket = latest ? candleBucket(latest.timestamp, state.marketInterval) : null;

  if (!latest || bucket > latestBucket) {
    state.chartCandles.push({
      timestamp: bucket.toISOString(),
      open_price: String(price),
      high_price: String(price),
      low_price: String(price),
      close_price: String(price),
      volume: String(volume),
      currency: trade.currency,
    });
    if (state.chartCandles.length > 100) state.chartCandles.shift();
  } else if (bucket.getTime() === latestBucket.getTime()) {
    latest.high_price = String(Math.max(Number(latest.high_price), price));
    latest.low_price = String(Math.min(Number(latest.low_price), price));
    latest.close_price = String(price);
    latest.volume = String((Number(latest.volume) || 0) + volume);
  }

  state.chartQuote = {
    ...state.chartQuote,
    price: String(price),
    currency: trade.currency,
    timestamp: trade.timestamp,
    source: "toss",
  };
  if (!chartRenderPending) {
    chartRenderPending = true;
    requestAnimationFrame(() => {
      chartRenderPending = false;
      renderChart(state.chartCandles, state.chartQuote);
      if (state.marketLive) $("#chartSource").textContent = "TOSS LIVE";
    });
  }
}

function connectMarketStream(symbol) {
  state.marketSocketGeneration += 1;
  const generation = state.marketSocketGeneration;
  state.marketLive = false;
  if (state.marketSocket) state.marketSocket.close(1000, "switch symbol");
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(
    `${protocol}//${location.host}/ws/market/trades?symbol=${encodeURIComponent(symbol)}`,
  );
  state.marketSocket = socket;

  socket.addEventListener("message", (event) => {
    if (generation !== state.marketSocketGeneration) return;
    const message = JSON.parse(event.data);
    if (message.type === "connected") {
      state.marketLive = true;
      setTossConnection(true);
      $("#chartSource").textContent = "TOSS LIVE";
    } else if (message.type === "trade") {
      applyRealtimeTrade(message);
    }
  });
  socket.addEventListener("close", () => {
    if (generation !== state.marketSocketGeneration) return;
    state.marketLive = false;
    setTossConnection(false);
    if (state.chartQuote?.source === "toss") $("#chartSource").textContent = "TOSS 재연결";
    setTimeout(async () => {
      if (generation !== state.marketSocketGeneration || document.hidden) return;
      await loadMarket(state.marketSymbol, state.marketInterval, true, true);
      connectMarketStream(state.marketSymbol);
    }, 2000);
  });
}

async function loadMarket(symbol, interval = state.marketInterval, silent = false, preserveZoom = false) {
  const normalized = symbol.trim().toUpperCase();
  if (!normalized) return;
  if (normalized !== state.marketSymbol) setChartCollapsed(false);
  state.marketSymbol = normalized;
  state.marketInterval = interval;
  $("#marketSymbolInput").value = normalized;
  $("#chartLoading").hidden = false;
  try {
    const lookup = await request(`${API}/market/lookup?symbol=${encodeURIComponent(normalized)}`);
    const candleCount = interval === "1d" ? 250 : interval === "1w" ? 104 : 60;
    const candlePayload = await request(
      `${API}/market/candles?symbol=${encodeURIComponent(normalized)}&interval=${interval}&count=${candleCount}`,
    );
    if (normalized !== state.marketSymbol || interval !== state.marketInterval) return;
    state.chartCandles = candlePayload.candles;
    state.chartQuote = lookup.quote;
    if (lookup.day_open != null) state.marketDayOpen = lookup.day_open;
    else if (!preserveZoom) state.marketDayOpen = null;
    if (lookup.previous_close != null) state.marketPreviousClose = lookup.previous_close;
    else if (!preserveZoom) state.marketPreviousClose = null;
    if (lookup.stock?.name) state.marketName = lookup.stock.name;
    else if (!preserveZoom) state.marketName = "-";
    if (!preserveZoom) {
      state.chartVisibleCount = Math.max(10, candlePayload.candles.length);
    } else {
      const minimum = Math.min(10, candlePayload.candles.length);
      state.chartVisibleCount = Math.max(minimum, Math.min(state.chartVisibleCount, candlePayload.candles.length));
    }
    renderChart(state.chartCandles, state.chartQuote);
    if (!preserveZoom && lookup.quote.source === "toss") connectMarketStream(normalized);
    if (!silent) toast(`${normalized} 시세를 조회했습니다.`);
  } catch (error) {
    if (!silent) toast(error.message, true);
    if (!silent || !state.chartCandles.length) {
      $("#chartEmpty").hidden = false;
      $("#chartEmpty").textContent = state.status?.market_data === "toss"
        ? "시세를 불러오지 못했습니다. 종목 코드와 API 설정을 확인하세요."
        : "수동 모드에서는 먼저 아래에서 해당 종목의 테스트 시세를 입력하세요.";
    }
  } finally {
    $("#chartLoading").hidden = true;
  }
}

async function resolveMarketSymbol(query) {
  const value = query.trim();
  const normalized = value.toUpperCase();
  const isAsciiSymbol = /^[A-Za-z0-9.\-]+$/.test(value);
  const isUnambiguousCode = isAsciiSymbol && /[0-9.\-]/.test(value);
  if (isUnambiguousCode) return { symbol: normalized, name: null };
  try {
    const payload = await request(`${API}/market/search?q=${encodeURIComponent(value)}&limit=10`);
    const exact = payload.results.find((item) =>
      item.name?.toUpperCase() === normalized || item.symbol?.toUpperCase() === normalized
    );
    if (exact) return exact;
    if (!isAsciiSymbol && payload.results.length) return payload.results[0];
  } catch (error) {
    if (!isAsciiSymbol) throw error;
  }
  if (isAsciiSymbol) return { symbol: normalized, name: null };
  throw new Error(`'${value}' 종목을 찾을 수 없습니다.`);
}

async function refreshCurrentMarket() {
  if (document.hidden || state.marketLive || !state.marketSymbol || marketRefreshInFlight) return;
  marketRefreshInFlight = true;
  try {
    await loadMarket(state.marketSymbol, state.marketInterval, true, true);
  } finally {
    marketRefreshInFlight = false;
  }
}

async function loadAll(silent = false) {
  try {
    const [status, account, performance, orders, strategies, quotes, risk] = await Promise.all([
      request(`${API}/system/status`), request(`${API}/paper/account`), request(`${API}/performance`),
      request(`${API}/orders`), request(`${API}/strategies`), request(`${API}/market/quotes`), request(`${API}/risk/status`),
    ]);
    renderStatus(status, risk);
    renderAccount(account, performance);
    renderCapital(account, risk, status);
    renderOrders(orders.orders);
    renderStrategies(strategies.strategies);
    renderQuotes(quotes.quotes);
    if (!silent) toast("최신 상태를 불러왔습니다.");
  } catch (error) {
    setTossConnection(false);
    if (!silent) toast(error.message, true);
  }
}

async function action(path, message) {
  try {
    await request(path, { method: "POST" });
    toast(message);
    await loadAll(true);
  } catch (error) { toast(error.message, true); }
}

$("#startButton").addEventListener("click", () => action(`${API}/engine/start`, "자동매매 엔진을 시작했습니다."));
$("#stopButton").addEventListener("click", () => action(`${API}/engine/stop`, "엔진을 안전하게 중지했습니다."));
$("#killButton").addEventListener("click", () => {
  if (confirm("신규 가상 주문을 즉시 차단하고 엔진을 중지할까요?")) action(`${API}/risk/kill-switch`, "킬 스위치를 활성화했습니다.");
});
$("#clearKillButton").addEventListener("click", () => action(`${API}/risk/kill-switch/clear`, "킬 스위치를 해제했습니다."));
$("#refreshAllButton").addEventListener("click", async () => {
  await loadAll();
  await loadMarket(state.marketSymbol, state.marketInterval, true, true);
  await checkTossConnection(false);
});
$("#tossConnectionButton").addEventListener("click", () => checkTossConnection());
$("#tradingModeButton").addEventListener("click", () => {
  toast(state.status?.mode === "paper" ? "현재 가상금액 주문 모드입니다." : "현재 실제계좌 주문 모드입니다.");
});
$("#recommendationButton").addEventListener("click", async () => {
  const button = $("#recommendationButton");
  const results = $("#recommendationResults");
  results.hidden = false;
  $("#recommendationBudget").textContent = "분석 중";
  $("#recommendationList").innerHTML = '<p class="recommendation-loading">토스 시세와 기술적 지표를 분석하고 있습니다...</p>';
  $("#recommendationDisclaimer").textContent = "";
  $("#recommendationFunnel").textContent = "전체 종목 → 예산·유동성 30개 → 위험 제외 12개 → 지표 분석 중";
  results.scrollIntoView({ behavior: "smooth", block: "nearest" });
  button.disabled = true;
  button.textContent = "후보 분석 중...";
  try {
    const payload = await request(`${API}/recommendations`);
    renderRecommendations(payload);
  } catch (error) {
    $("#recommendationBudget").textContent = "분석 실패";
    $("#recommendationList").innerHTML = `<p class="recommendation-error">${escapeHtml(error.message)}</p>`;
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "추천 후보 찾기";
  }
});
$("#recommendationList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-candidate-symbol]");
  if (!button) return;
  await loadMarket(button.dataset.candidateSymbol, "1d");
  document.querySelector(".market-workspace").scrollIntoView({ behavior: "smooth" });
});
$("#themeToggle").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(next);
});
$("#ordersRefreshButton").addEventListener("click", () => loadAll());
document.querySelectorAll("[data-report-period]").forEach((button) => {
  button.addEventListener("click", async () => {
    state.reportPeriod = button.dataset.reportPeriod;
    document.querySelectorAll("[data-report-period]").forEach((item) => item.classList.toggle("active", item === button));
    await loadWeeklyReport();
  });
});
$(".weekly-report-grid").addEventListener("click", (event) => {
  const button = event.target.closest("[data-report-bookmark]");
  if (!button) return;
  const item = state.reportLookup[button.dataset.reportBookmark];
  if (!item) return;
  const favorites = reportFavorites();
  const index = favorites.findIndex((saved) => saved.url === item.url && saved.title === item.title);
  if (index >= 0) favorites.splice(index, 1);
  else favorites.unshift({ ...item, saved_at: new Date().toISOString() });
  saveReportFavorites(favorites);
  button.classList.toggle("saved", index < 0);
});
$("#reportFavorites").addEventListener("click", (event) => {
  const button = event.target.closest("[data-favorite-delete]");
  if (!button) return;
  const favorites = reportFavorites();
  favorites.splice(Number(button.dataset.favoriteDelete), 1);
  saveReportFavorites(favorites);
  loadWeeklyReport();
});
$("#clearReportFavorites").addEventListener("click", () => {
  if (!reportFavorites().length || confirm("즐겨찾기를 모두 삭제할까요?")) {
    saveReportFavorites([]);
    loadWeeklyReport();
  }
});
$("#tossRefreshButton").addEventListener("click", async () => {
  try {
    await request(`${API}/market/refresh`, { method: "POST" });
    toast("토스증권 현재가를 갱신했습니다.");
    await loadAll(true);
    await loadMarket(state.marketSymbol, state.marketInterval, true);
  } catch (error) { toast(error.message, true); }
});

$("#quoteForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await request(`${API}/market/quotes`, { method: "POST", body: JSON.stringify(Object.fromEntries(form)) });
    toast("테스트 시세를 반영했습니다.");
    await loadAll(true);
    await loadMarket(form.get("symbol"), state.marketInterval, true);
  } catch (error) { toast(error.message, true); }
});

$("#marketSearchForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = String(new FormData(event.currentTarget).get("symbol"));
  try {
    const stock = await resolveMarketSymbol(query);
    await loadMarket(stock.symbol, state.marketInterval);
    if (stock.name) toast(`${stock.name} (${stock.symbol}) 종목을 조회했습니다.`);
  } catch (error) {
    toast(error.message, true);
  }
});

$("#marketSymbolInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    $("#marketSearchForm").requestSubmit();
  }
});

document.querySelectorAll("[data-interval]").forEach((button) => {
  button.addEventListener("click", async () => {
    document.querySelectorAll("[data-interval]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    await loadMarket(state.marketSymbol, button.dataset.interval);
  });
});

function setChartCollapsed(collapsed) {
  state.chartCollapsed = collapsed;
  document.querySelector(".market-panel").classList.toggle("chart-collapsed", collapsed);
  $("#chartName").setAttribute("aria-expanded", String(!collapsed));
  $("#chartName").title = collapsed ? "클릭하여 차트 펼치기" : "클릭하여 차트 접기";
}

$("#chartName").addEventListener("click", () => setChartCollapsed(!state.chartCollapsed));
$("#chartName").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    setChartCollapsed(!state.chartCollapsed);
  }
});

$("#chartContainer").addEventListener("wheel", (event) => {
  if (!state.chartCandles.length || !state.chartQuote) return;
  event.preventDefault();
  const total = state.chartCandles.length;
  const minimum = Math.min(10, total);
  const step = Math.max(2, Math.round(state.chartVisibleCount * 0.12));
  const next = event.deltaY < 0
    ? state.chartVisibleCount - step
    : state.chartVisibleCount + step;
  state.chartVisibleCount = Math.max(minimum, Math.min(total, next));
  renderChart(state.chartCandles, state.chartQuote);
}, { passive: false });

$("#strategyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget));
  payload.enabled = true;
  try {
    await request(`${API}/strategies`, { method: "POST", body: JSON.stringify(payload) });
    toast("전략을 저장했습니다.");
    await loadAll(true);
  } catch (error) { toast(error.message, true); }
});

$("#strategyList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-strategy]");
  if (!button) return;
  const actionName = button.dataset.enable === "true" ? "enable" : "disable";
  await action(`${API}/strategies/${encodeURIComponent(button.dataset.strategy)}/${actionName}`, "전략 상태를 변경했습니다.");
});

applyTheme(document.documentElement.dataset.theme);
loadAll(true);
loadWeeklyReport();
setInterval(loadWeeklyReport, 300000);
checkTossConnection(false);
setInterval(() => loadAll(true), 3000);
loadMarket(state.marketSymbol, state.marketInterval, true);
setInterval(refreshCurrentMarket, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshCurrentMarket();
});
