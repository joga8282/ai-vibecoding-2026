const API = "/api/v1";
const $ = (selector) => document.querySelector(selector);

const state = {
  status: null,
  account: null,
  performance: null,
  risk: null,
  reportPeriod: "daily",
  reportLookup: {},
  recommendations: null,
};
let toastTimer;
const closingPositions = new Set();
let positionQuoteRefreshAt = 0;
let positionQuoteRefreshPromise = null;
let dashboardSessionPromise = null;

// Remove the legacy browser copy; the API secret stays on the server.
try { sessionStorage.removeItem("api-access-token"); } catch {}

function applyTheme(theme) {
  const selected = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = selected;
  const isLight = selected === "light";
  $("#themeIcon").textContent = isLight ? "☀" : "☾";
  $("#themeLabel").textContent = isLight ? "LIGHT" : "DARK";
  $("#themeToggle").setAttribute("aria-pressed", String(isLight));
  try { localStorage.setItem("trader-theme", selected); } catch {}
}

async function ensureDashboardSession() {
  if (!dashboardSessionPromise) {
    dashboardSessionPromise = (async () => {
      const response = await fetch(`${API}/auth/local-session`, {
        method: "POST", headers: { "X-Dashboard-Request": "1" },
        credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error("대시보드 자동 인증에 실패했습니다. 현재 PC의 127.0.0.1 또는 localhost 화면에서 다시 연결하세요.");
    })().finally(() => { dashboardSessionPromise = null; });
  }
  return dashboardSessionPromise;
}

async function request(path, options = {}, allowSessionRefresh = true) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await fetch(path, { ...options, credentials: "same-origin", cache: "no-store", headers });
  if (response.status === 401 && allowSessionRefresh) {
    await ensureDashboardSession();
    return request(path, options, false);
  }
  if (response.status === 401) {
    throw new Error("LIVE 인증 연결이 만료되었거나 실패했습니다. 화면을 새로고침하세요.");
  }
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.detail || (response.status >= 500
    ? `서버가 요청을 처리하지 못했습니다 (${response.status}). 잠시 후 다시 조회해 주세요.`
    : `요청 실패 (${response.status})`));
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

function liveExposureLabel(limits) {
  const ratio = Number(limits?.max_total_exposure_ratio ?? 0.15);
  return `총자산 ${ratio * 100}% ${ratio >= 1 ? "이내" : "미만"}`;
}

function liveOrderLimitLabel(limits) {
  return Number(limits?.max_order_amount_krw) > 0 ? money(limits.max_order_amount_krw, "KRW") : "가용 현금 한도";
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
    await request(`${API}/market/lookup?symbol=005930`);
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
    : [status.automation?.execution_state, status.automation?.message].filter(Boolean).join(" · ") || `v${status.version} · ${status.market_data.toUpperCase()} 시세`;
  if (status.mode === "live" && !status.live_readiness?.reconciled) {
    $("#engineLabel").textContent = "LIVE 주문 잠금";
    $("#engineDetail").textContent = "LIVE 계좌 동기화 실패 또는 미완료 · 연결을 복구한 뒤 다시 동기화하세요.";
  } else if (status.mode === "live" && !status.live_readiness?.armed) {
    $("#engineLabel").textContent = "LIVE 주문 잠금";
    const unresolved = Number(status.live_readiness?.unresolved_order_count || 0);
    $("#engineDetail").textContent = unresolved > 0
      ? `미확인 주문 ${unresolved}건 · 접수 여부를 확인한 뒤 계좌 동기화·무장을 진행하세요.`
      : "LIVE 자동 실행은 계좌 동기화와 별도 수동 무장 후에만 가능합니다.";
  }
  if (status.mode === "live" && status.live_readiness?.last_order_error) {
    $("#engineDetail").textContent += ` · ${status.live_readiness.last_order_error}`;
  }
  const auto = status.automation;
  const minProfit = Number(risk?.swing_exit_policy?.min_net_profit_percent ?? 3);
  $("#strategyExitValue").textContent = `상단 + 예상 순수익 ${minProfit}% 이상 익절`;
  $("#strategyExitHelp").textContent = `추적 기준에서 순수익 ${minProfit}% 확보 가능 시 활성화 · 이후 고점 -2% 보호 매도는 ${minProfit}% 미만에서도 가능 · 평균 매입가 -3% 손절`;
  if ($("#signalObservationCount")) $("#signalObservationCount").textContent = `${number(auto?.signal_observation_count || 0, "KRW")}건`;
  renderDiagnostics(auto?.diagnostics);
  $("#autoBudgetStatus").textContent = auto && Number(auto.budget) > 0
    ? `스윙 예산 ${money(auto.budget, "KRW")} · ${auto.holding_count ?? 0}/5종목 보유 · 보유 ${status.mode === "live" ? "평가액" : "원가"} ${money(auto.spent, "KRW")} · 매수 가능 ${money(auto.remaining, "KRW")}`
    : `최대 5종목 · -3% 손절 · 상단 + 예상 순수익 ${minProfit}% 이상 익절 · 수익 기준 충족 후 고점 -2% 보호 매도`;
  const weekly = auto?.weekly;
  $("#scalpSummary").textContent = weekly ? `누적 실현손익 ${money(weekly.realized_profit, "KRW")} · 청산 매수원가 대비 ${weekly.return_percent ?? "-"}% · 보유 중 제외` : "기록 대기";
  $("#scalpDays").innerHTML = (weekly?.days || []).map(day => `<tr><td>${escapeHtml(day.date)} · ${escapeHtml(day.name || day.symbol || "")}</td><td>${escapeHtml(day.status)}</td><td>${money(day.cost, "KRW")}</td><td>${day.profit === null ? "-" : money(day.profit, "KRW")}</td><td>${day.return_percent === null ? "-" : `${escapeHtml(day.return_percent)}%`}</td></tr>`).join("");
  $("#startButton").textContent = auto && Number(auto.budget) > 0 ? "가상 자동매매 재개" : "가상 자동매매 시작";
  const liveArmButton = $("#liveArmButton");
  liveArmButton.hidden = status.mode !== "live";
  liveArmButton.textContent = status.live_readiness?.armed ? "LIVE 무장 해제" : "LIVE 계좌 동기화·무장";
  liveArmButton.disabled = status.running || status.kill_switch;
  if (status.mode === "live") {
    $("#startButton").textContent = status.running && status.live_readiness?.armed
      && status.live_readiness?.reconciled ? "LIVE 자동매매 실행 중" : "LIVE 자동매매 시작";
  }
  const liveNotReady = status.mode === "live"
    && !(status.live_readiness?.reconciled && status.live_readiness?.armed);
  $("#startButton").disabled = status.running || status.kill_switch || liveNotReady;
  $("#stopButton").disabled = !status.running;
  $("#killButton").disabled = status.kill_switch;
  $("#clearKillButton").disabled = !status.kill_switch;
  $("#killSwitchLabel").textContent = status.kill_switch ? "긴급 중지" : "정상";
  $("#killSwitchLabel").className = `metric-value small ${status.kill_switch ? "negative" : "positive"}`;
  $("#marketMode").textContent = status.market_data.toUpperCase();
  $("#marketMode").className = `badge ${status.market_data === "toss" ? "badge-ok" : "badge-muted"}`;
  $("#marketDescription").textContent = status.market_data === "toss"
    ? "토스증권 REST API에서 활성 전략 종목의 실제 현재가를 조회합니다."
    : "토스 API 키가 설정되지 않았습니다. 시세 연동을 위해 .env 설정을 확인하세요.";
  $("#tossRefreshButton").disabled = status.market_data !== "toss";
  const isPaper = status.mode === "paper";
  $("#marketEyebrow").textContent = isPaper ? "TOSS MARKET · PAPER" : "TOSS MARKET · LIVE ACCOUNT";
  $("#accountHeading").textContent = isPaper ? "가상계좌" : "토스 실계좌";
  $("#capitalTitle").textContent = isPaper ? "가상계좌 주문 자금" : "실계좌 주문 자금";
  $("#capitalAccountLabel").textContent = isPaper ? "가상계좌 평가금액" : "실계좌 평가금액";
  $("#capitalMode").textContent = isPaper ? "PAPER" : "LIVE";
  $("#investmentSettingsButton").disabled = false;
  const maxRatio = isPaper ? 50 : Math.min(Number(risk?.live_limits?.max_total_exposure_ratio ?? 0.15),
    Number(risk?.live_limits?.auto_max_total_exposure_ratio ?? 0.15)) * 100;
  $("#investmentRatioInput").max = String(maxRatio);
  $("#capitalRatioInput").max = String(maxRatio);
  $("#manualRatioInput").max = String(isPaper ? 100 : Number(risk?.live_limits?.max_total_exposure_ratio ?? 0.15) * 100);
  $("#investmentRatioHelp").textContent = isPaper
    ? "매수 신호가 오면 설정한 자산 비율과 가용 현금으로 주문합니다. 최대 5종목을 보유합니다."
    : `총 투자예산을 최대 5종목에 균등 배분하여 종목당 1/5 이내에서 매수합니다. 보유 평가액·미체결 매수 합계에 ${liveExposureLabel(risk?.live_limits)}를 적용합니다. ${risk?.live_limits?.symbol_policy === "recommended" ? "최신 추천 조건 통과 종목만 매수합니다." : "종목 허용목록도 적용합니다."} 가용 현금·수수료를 적용하며 1주 예산이 부족한 종목은 건너뜁니다.`;
  $("#tradingHoursHelp").textContent = `평일 정규장 09:00~15:30 · 애프터마켓 16:00~20:00 · 5분 간격 확인 · ${isPaper ? "PAPER" : "LIVE 실계좌"} 주문`;
  $("#strategyCapitalHelp").textContent = isPaper ? "1회 매수 비율 설정 · PAPER" : `${liveExposureLabel(risk?.live_limits)} · LIVE`;
  $("#screeningEyebrow").textContent = isPaper ? "PAPER SCREENING" : "LIVE SCREENING";
  $("#manualCloseHelp").textContent = isPaper
    ? "최신 거래 분봉으로 PAPER 전량 매도합니다. 당일 자동 재매수 제외"
    : "최신 호가와 매도 가능 수량을 확인하여 실계좌 시장가 매도를 제출합니다. 부분 체결 시 잔량 취소를 확인합니다. 당일 자동 재매수 제외";
  $("#tradingModeButton").textContent = isPaper ? "PAPER" : "실계좌 주문";
  $("#tradingModeButton").className = `button status-button ${isPaper ? "status-paper" : "status-live"}`;
  if (status.market_data !== "toss") setTossConnection(false);
  if (risk) $("#killSwitchLabel").title = `KRW 주문 한도 ${isPaper ? risk.max_order_amount.KRW : liveOrderLimitLabel(risk.live_limits)}`;
  if (state.recommendations) renderRecommendations(state.recommendations, false);
}

function renderDiagnostics(diagnostics) {
  const data = diagnostics || {};
  $("#diagnosticsDate").textContent = data.date || "기록 없음";
  $("#diagnosticsResult").textContent = data.legacy_untracked
    ? "조건 미충족으로 진입하지 않았습니다. 이 날짜는 상세 진단 적용 전 기록이라 검색 횟수와 탈락 사유는 남아 있지 않습니다."
    : data.outcome === "진입 없음"
    ? `조건 미충족으로 매매를 건너뛰었습니다. 조건 검색 ${data.scan_count || 0}회, 실제 매수 주문 시도 ${data.buy_order_attempts || 0}회입니다.`
    : `현재 결과: ${data.outcome || "대기"}`;
  $("#diagnosticScans").textContent = `${data.scan_count || 0}회`;
  $("#diagnosticAnalyzed").textContent = `${data.analyzed_count || 0}개`;
  $("#diagnosticQualified").textContent = `${data.qualified_count || 0}개`;
  $("#diagnosticBuyAttempts").textContent = `${data.buy_order_attempts || 0}회`;
  $("#diagnosticBuyFilled").textContent = `${data.buy_orders_filled || 0}회`;
  $("#diagnosticErrors").textContent = `${data.scan_errors || 0}회`;
  const reasons = Object.entries(data.rejection_counts || {}).sort((a, b) => b[1] - a[1]);
  $("#diagnosticReasons").innerHTML = reasons.length
    ? reasons.map(([reason, count]) => `<div><span>${escapeHtml(reason)}</span><strong>${number(count, "KRW")}회</strong></div>`).join("")
    : '<p class="empty">미충족 사유 기록이 없습니다.</p>';
  const symbols = data.last_symbols || [];
  $("#diagnosticSymbols").innerHTML = symbols.length
    ? symbols.map(item => `<div><span><strong>${escapeHtml(item.name || item.symbol)}</strong> <small>${escapeHtml(item.symbol)}</small></span><strong class="${item.eligible ? "positive" : ""}">${item.eligible ? "통과" : escapeHtml((item.reasons || []).join(", "))}</strong></div>`).join("")
    : '<p class="empty">마지막 검색 종목 기록이 없습니다.</p>';
  const lastScan = data.last_scan_at ? new Date(data.last_scan_at).toLocaleString("ko-KR") : "없음";
  $("#diagnosticLastScan").textContent = `마지막 검색: ${lastScan}${data.last_error ? ` · 오류: ${data.last_error}` : ""}`;
}

function renderAccount(account, performance) {
  state.account = account;
  state.performance = performance;
  const isLive = account.mode === "live";
  const cash = account.buying_power || account.cash || {};
  const rows = performance.by_currency || {};
  $("#accountHeading").textContent = isLive ? "\ud1a0\uc2a4 \uacc4\uc88c" : "\uac00\uc0c1 \uacc4\uc88c";
  $("#accountModeBadge").textContent = isLive ? "LIVE" : "PAPER";
  $("#accountModeBadge").className = `badge ${isLive ? "status-live" : "badge-paper"}`;
  $("#krwCashLabel").textContent = isLive ? "KRW \uc8fc\ubb38 \uac00\ub2a5 \uae08\uc561" : "KRW \ud604\uae08";
  $("#usdCashLabel").textContent = isLive ? "USD \uc8fc\ubb38 \uac00\ub2a5 \uae08\uc561" : "USD \ud604\uae08";
  $("#krwCash").textContent = money(cash.KRW, "KRW");
  $("#usdCash").textContent = money(cash.USD, "USD");
  $("#krwEquity").textContent = money(account.total_equity?.KRW ?? rows.KRW?.current_equity, "KRW");
  $("#usdEquity").textContent = money(account.total_equity?.USD ?? rows.USD?.current_equity, "USD");
  for (const currency of ["KRW", "USD"]) {
    const target = $(`#${currency.toLowerCase()}Return`);
    if (isLive) {
      target.textContent = "\uc2e4\uacc4\uc88c \uc190\uc775 \ubbf8\uc9d1\uacc4";
      target.className = "metric-change";
      continue;
    }
    const row = rows[currency] || {};
    const rate = Number(row.return_rate_percent || 0);
    target.textContent = `${rate >= 0 ? "+" : ""}${rate.toFixed(2)}% P/L (realized + unrealized) ${money(row.profit_loss, currency)}`;
    target.className = `metric-change ${rate > 0 ? "positive" : rate < 0 ? "negative" : ""}`;
  }
  const orders = performance.orders || {};
  $("#filledOrders").textContent = orders.filled ?? 0;
  $("#rejectedOrders").textContent = orders.rejected ?? 0;
  renderPositions(account.positions || []);
}

function renderCapital(account, risk, status, performance) {
  const isPaper = status.mode === "paper";
  const accountAmount = Number((isPaper ? performance.by_currency?.KRW?.current_equity : null) || account.total_equity?.KRW || 0);
  const availableCash = Number(account.cash.KRW || 0);
  const tradeRatio = Number(risk.recommended_trade_ratio ?? 0.1);
  const hasSession = Number(status.automation?.budget) > 0;
  const recommendationAmount = hasSession ? Number(status.automation.remaining) : Math.min(availableCash, accountAmount * tradeRatio);
  const ratioPercent = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 2 }).format(tradeRatio * 100);
  $("#investmentRatioInput").value = String(Math.round(tradeRatio * 100));
  $("#investmentRatioValue").textContent = `${ratioPercent}%`;
  $("#capitalRatioInput").value = String(Math.round(tradeRatio * 100));
  $("#capitalRatioInput").disabled = false;
  $("#capitalRatioValue").textContent = `${ratioPercent}%`;
  $("#manualRatioInput").value = String(Number(risk.manual_trade_ratio ?? 0.15) * 100);
  $("#manualRatioInput").disabled = false;
  previewManualRatio($("#manualRatioInput").value);
  $("#capitalRatioPreview").textContent = `${money(Math.min(availableCash, accountAmount * tradeRatio), "KRW")} 예상 주문 예산 · 가용 현금 한도 적용`;
  $("#capitalTitle").textContent = isPaper ? "가상계좌 주문 자금" : "실계좌 주문 자금";
  $("#capitalAccountLabel").textContent = isPaper ? "가상계좌 평가금액" : "실계좌 평가금액";
  $("#capitalMode").textContent = isPaper ? "PAPER" : "LIVE";
  $("#capitalMode").className = `badge ${isPaper ? "badge-paper" : "status-live"}`;
  $("#capitalAccountAmount").textContent = money(accountAmount, "KRW");
  $("#capitalRecommendationAmount").textContent = money(recommendationAmount, "KRW");
  $("#capitalRecommendationHelp").textContent = hasSession
    ? `평가금액의 ${ratioPercent}% 한도에서 보유 원가를 제외한 예산 · 최대 5종목 · 매도 후 재사용`
    : `평가금액의 ${ratioPercent}%를 스윙 예산으로 사용 · 최대 5종목 · 종목별 주문 한도 적용`;
  $("#capitalRecommendationHelp").textContent = `총평가금액의 ${ratioPercent}%를 1회 매수 목표로 사용 · 분할매수 없음 · 최대 5종목`;
  if (!isPaper) {
    const perSymbol = Number(status.automation?.per_symbol_budget || recommendationAmount / 5);
    $("#capitalRecommendationHelp").textContent = `자동매매 총자산의 ${ratioPercent}% 예산에서 보유 평가액·미체결 매수를 차감 · 종목당 총 투자예산의 1/5, 현재 최대 ${money(perSymbol, "KRW")} · ${liveExposureLabel(risk.live_limits)} · ${liveOrderLimitLabel(risk.live_limits)}`;
    $("#capitalRatioPreview").textContent = `${money(recommendationAmount, "KRW")} 현재 추가 매수 가능 예산`;
  }
}

function buyBlockReason(symbol) {
  if (!state.status?.automation?.market_open) return "거래시간 외";
  if (state.status?.kill_switch) return "긴급 중지 상태";
  if (state.status?.mode !== "live") return "";
  if (!(state.status.live_readiness?.armed && state.status.live_readiness?.reconciled)) return "LIVE 동기화·무장 필요";
  if (state.risk?.live_limits?.symbol_policy !== "recommended"
      && !state.risk?.live_limits?.allowed_symbols?.includes(symbol)) return "LIVE 허용목록에 없음";
  if (Number(state.status.automation.manual_buy_capacity ?? state.status.automation.remaining) <= 0) return "추가 매수 한도 없음";
  return "";
}

function candidateBuyBlockReason(item) {
  const blocked = buyBlockReason(item.symbol);
  if (blocked || state.status?.mode !== "live") return blocked;
  if (state.account?.positions?.some(position => position.symbol === item.symbol)) return "보유 중 · 추가 매수 없음";
  return manualQuantity(item) < 1 ? "직접 매수 예산으로 1주 매수 불가" : "";
}

function manualRatioPercent() {
  return Number($("#manualRatioInput").value || Number(state.risk?.manual_trade_ratio ?? 0.15) * 100);
}

function manualBuyAmount(percent = manualRatioPercent()) {
  const equity = Number(state.account?.total_equity?.KRW || 0);
  const cash = Number(state.account?.cash?.KRW || 0);
  const selected = equity * Number(percent) / 100;
  if (state.status?.mode !== "live") return Math.max(0, Math.min(cash, selected));
  const capacity = Number(state.status?.automation?.manual_buy_capacity ?? state.status?.automation?.remaining ?? 0);
  return Math.max(0, Math.min(cash, selected, capacity));
}

function manualQuantity(item) {
  // Preview uses the displayed price; the server rechecks the actual ask and fees.
  if (!state.account) return Number(item.quantity || 0);
  const price = Number(item.price || 0);
  if (!(price > 0)) return 0;
  const live = state.status?.mode === "live";
  const fill = price * (live ? 1 : 1 + Number(state.risk?.slippage_bps || 0) / 10000);
  let quantity = Math.floor(manualBuyAmount() / (fill * (1 + Number(state.risk?.fee_rate || 0))));
  const cap = Number(live ? state.risk?.live_limits?.max_order_amount_krw : state.risk?.max_order_amount?.KRW);
  if (cap > 0) quantity = Math.min(quantity, Math.floor(cap / fill));
  return quantity;
}

function previewManualRatio(percent) {
  $("#manualRatioValue").textContent = `${percent}%`;
  $("#manualRatioPreview").textContent = `직접 매수 1회 예상 예산 ${money(manualBuyAmount(percent), "KRW")} · 총자산의 ${percent}% 이내 · 보유·미체결·가용 현금·주문 한도 재검사`;
}

function renderRecommendations(payload, scroll = true) {
  state.recommendations = payload;
  if (Number(payload.minimum_market_cap_krw) > 0) {
    $("#strategyMarketCap").textContent = `국내 보통주 · 시가총액 ${marketCap(payload.minimum_market_cap_krw)} 이상`;
  }
  const container = $("#recommendationResults");
  container.hidden = false;
  $("#recommendationBudget").textContent = `사용 금액 ${money(payload.budget, "KRW")}`;
  $("#recommendationDisclaimer").textContent = payload.disclaimer;
  const funnel = payload.funnel || {};
  $("#recommendationFunnel").textContent = `${new Date().toLocaleTimeString("ko-KR")} 조회 완료 · 등록 테마 ${number(funnel.universe || 0, "KRW")}개 → 시세 확인 ${number(funnel.budget_liquidity || 0, "KRW")}개 → 대형주 필터 ${number(funnel.risk_filtered || 0, "KRW")}개 → 지표 분석 ${number(funnel.analyzed || 0, "KRW")}개 → 조건 충족 ${number(funnel.qualified ?? payload.candidates.length, "KRW")}개`;
  $("#recommendationList").innerHTML = payload.candidates.length
    ? `<h3 class="result-heading">매수 자리 도달 · 자동 매수 조건 통과</h3>` + payload.candidates.map((item, index) => `<article class="recommendation-card" data-chart-symbol="${escapeHtml(item.symbol)}" data-chart-name="${escapeHtml(item.name)}" tabindex="0" role="button" aria-label="${escapeHtml(item.name)} 차트 보기">
        <div class="recommendation-rank">#${index + 1} · <strong>${item.score}점</strong></div>
        <h3 title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</h3>
        <span class="recommendation-symbol">${escapeHtml(item.symbol)}</span>
        <div class="recommendation-price">${money(item.price, item.currency)}</div>
        <div class="recommendation-metrics">
          ${String(item.strategy || "").startsWith("swing-v") ? `<div>테마 ${escapeHtml((item.themes || []).join(" · "))}</div><div>시가총액 ${marketCap(item.market_cap)}</div><div>주봉 관심 가격 ${number(item.weekly_support_floor, item.currency)} ~ ${number(item.weekly_entry_ceiling, item.currency)} · 최근 20주 하위 40%</div><div>4시간봉 볼린저 하단 ${number(item.bollinger_lower, item.currency)} · 밴드 하위 25% 및 하단 3% 이내</div><div>최근 20일 고저 범위 하위 40% · 최종 매수 상한 ${number(item.entry_ceiling, item.currency)}</div><div>손절 평균 매입가 -3% · 상단 + 예상 순수익 ${Number(state.risk?.swing_exit_policy?.min_net_profit_percent ?? 3)}% 이상 익절 · 수익 기준 충족 후 고점 -2% 보호 매도</div><div>주봉 MA10 ${number(item.weekly_ma10, item.currency)} · 일봉 MA20 ${number(item.ma20, item.currency)} · MA60 ${number(item.ma60, item.currency)}</div><div>주봉·일봉 방향 확인 · 최대 5종목 · 다일 보유</div>` : `
          <div>시가총액 ${marketCap(item.market_cap)}</div>
          <div>일봉 지지 ${number(item.daily_support, item.currency)} · 저항 ${number(item.daily_resistance, item.currency)}</div>
          <div>주봉 지지 ${number(item.weekly_support, item.currency)} · 저항 ${number(item.weekly_resistance, item.currency)}</div>
          <div>일봉 지지 거리 ${item.support_distance_percent}% · 저항 여력 ${item.daily_resistance_upside_percent}%</div>
          <div>MA5 ${number(item.ma5, item.currency)}</div>
          <div>MA20 ${number(item.ma20, item.currency)}</div>
          <div>MA60 ${number(item.ma60, item.currency)} · 상승 추세 확인</div>
          <div>볼린저 하단 ${number(item.bollinger_lower, item.currency)} · ${item.bollinger_touched ? "접촉 후 회복" : "미접촉"}</div>
          <div>5일 ${item.return_5d_percent}% · 20일 ${item.return_20d_percent}%</div>
          <div>20일선 이격 ${item.ma20_distance_percent}% · 과열 제외 통과</div>
          <div>RSI ${item.rsi} · 거래량 ${item.volume_ratio}배</div>
          `}
        </div>
        <p class="recommendation-reason">${escapeHtml(item.reason)}</p>
        <div class="recommendation-quantity">자동 배분 최대 ${number(item.quantity, "KRW")}주 · 직접 ${manualRatioPercent()}% 선택 시 예상 ${number(manualQuantity(item), "KRW")}주</div>
        <button type="button" class="button button-primary full" data-qualified-buy="${escapeHtml(item.symbol)}" ${candidateBuyBlockReason(item) ? "disabled" : ""}>${escapeHtml(candidateBuyBlockReason(item) || (state.status?.mode === "live" ? "LIVE 실계좌 조건 확인·매수" : "조건 통과 종목 자산 비율 매수"))}</button>
      </article>`).join("")
    : '<p class="empty">자동 매수 조건을 모두 통과한 종목은 없습니다.</p>';
  const watchlist = payload.watchlist || [];
  $("#recommendationWatchlist").innerHTML = watchlist.length
    ? `<h3 class="result-heading">매수 자리 근접 · 조건 대기 <small>매수 추천 아님 · 자동 매수 안 함</small></h3><div class="recommendation-list">${watchlist.map((item, index) => `<article class="recommendation-card watch-card" data-chart-symbol="${escapeHtml(item.symbol)}" data-chart-name="${escapeHtml(item.name)}" tabindex="0" role="button" aria-label="${escapeHtml(item.name)} 차트 보기">
        <div class="recommendation-rank">관찰 #${index + 1} · <strong>${item.conditions_passed}/${item.conditions_total} 조건 통과</strong></div>
        <h3>${escapeHtml(item.name)}</h3><span class="recommendation-symbol">${escapeHtml(item.symbol)}</span>
        <div class="recommendation-price">${money(item.price, item.currency)}</div>
        <div class="recommendation-metrics"><div>테마 ${escapeHtml((item.themes || []).join(" · "))}</div><div>주봉 관심 가격 ${number(item.weekly_support_floor, item.currency)} ~ ${number(item.weekly_entry_ceiling, item.currency)} · 주봉 범위 내 위치 ${escapeHtml(item.weekly_range_position_percent ?? "-")}%</div><div>MA20 ${number(item.ma20, item.currency)} · MA60 ${number(item.ma60, item.currency)}</div><div>볼린저 하단 ${number(item.bollinger_lower, item.currency)} · 현재가 거리 ${escapeHtml(item.band_distance_percent)}%</div><div>4시간 밴드 내 위치 ${escapeHtml(item.four_hour_band_position_percent ?? "-")}% · 최근 20일 범위 내 위치 ${escapeHtml(item.daily_range_position_percent ?? "-")}%</div><div>매수 상한 ${number(item.entry_ceiling, item.currency)} · 주봉·일봉·4시간 조건 확인</div></div>
        <p class="recommendation-reason">미충족: ${escapeHtml((item.rejection_reasons || []).join(" · "))}</p>
        ${state.status?.mode === "paper" ? `<button type="button" class="button button-secondary full" data-test-buy="${escapeHtml(item.symbol)}" ${state.status?.automation?.market_open ? "" : "disabled"}>1주 PAPER 테스트 매수</button>` : '<p class="muted">조건 미충족 · LIVE 매수 불가</p>'}
      </article>`).join("")}</div>`
    : "";
  if (scroll) container.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

const chartState = { symbol: null, name: null, interval: "1d" };
const chartLabels = { "1d": "일봉", "1w": "주봉", "1M": "월봉", "15m": "15분봉", "30m": "30분봉", "60m": "60분봉", "4h": "4시간봉" };

function fourHourCandleCompleted(timestamp, now) {
  const offset = 9 * 60 * 60 * 1000;
  const local = new Date(new Date(timestamp).getTime() + offset);
  const morning = local.getUTCHours() < 13;
  const end = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate(),
    morning ? 13 : 15, morning ? 0 : 30) - offset;
  return end <= now.getTime();
}

function kstWeekStart(timestamp) {
  const offset = 9 * 60 * 60 * 1000;
  const local = new Date(new Date(timestamp).getTime() + offset);
  const mondayOffset = (local.getUTCDay() + 6) % 7;
  return Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate() - mondayOffset) - offset;
}

function renderCandlestickChart(candles, interval, dailyCandles = []) {
  const canvas = $("#candidateChartCanvas");
  if (!candles.length) {
    canvas.innerHTML = '<p class="empty">표시할 시세 데이터가 없습니다.</p>';
    return;
  }
  const rows = candles.slice().sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
  const daily = dailyCandles.slice().sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
  const periods = [20, 60, 120, 240];
  const maColors = { 20: "#f7c948", 60: "#ff8c42", 120: "#b779ff", 240: "#25c2a0" };
  const dailyAverages = new Map();
  const closes = daily.map(candle => Number(candle.close_price));
  daily.forEach((candle, index) => {
    const values = {};
    periods.forEach(period => {
      if (index + 1 >= period) {
        const sum = closes.slice(index + 1 - period, index + 1).reduce((total, value) => total + value, 0);
        values[period] = sum / period;
      }
    });
    if (index + 1 >= 20) {
      const window = closes.slice(index + 1 - 20, index + 1);
      const middle = window.reduce((total, value) => total + value, 0) / 20;
      const deviation = Math.sqrt(window.reduce((total, value) => total + (value - middle) ** 2, 0) / 20);
      values.bollingerUpper = middle + deviation * 2;
      values.bollingerLower = middle - deviation * 2;
    }
    dailyAverages.set(new Date(candle.timestamp).toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" }), values);
  });
  const intervalBands = new Map();
  if (["4h", "1w"].includes(interval)) {
    const period = interval === "4h" ? 6 : 20;
    const now = new Date(), completedCloses = [];
    rows.forEach(candle => {
      const completed = interval === "4h" ? fourHourCandleCompleted(candle.timestamp, now)
        : kstWeekStart(candle.timestamp) < kstWeekStart(now);
      if (completed) completedCloses.push(Number(candle.close_price));
      if (completedCloses.length < period) return;
      const window = completedCloses.slice(-period);
      const middle = window.reduce((sum, value) => sum + value, 0) / period;
      const deviation = Math.sqrt(window.reduce((sum, value) => sum + (value - middle) ** 2, 0) / period);
      intervalBands.set(candle.timestamp, {bollingerUpper: middle + 2 * deviation, bollingerLower: middle - 2 * deviation});
    });
  }
  const averagesFor = candle => {
    const values = dailyAverages.get(new Date(candle.timestamp).toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" })) || {};
    if (!["4h", "1w"].includes(interval)) return values;
    const {bollingerUpper, bollingerLower, ...dailyMA} = values;
    return {...dailyMA, ...(intervalBands.get(candle.timestamp) || {})};
  };
  const width = 960, height = 470, left = 18, right = 78, top = 42, priceBottom = 326, volumeTop = 350, volumeBottom = 430, bottom = 30;
  const plotWidth = width - left - right, plotHeight = priceBottom - top;
  const highs = rows.map(c => Number(c.high_price));
  const lows = rows.map(c => Number(c.low_price));
  const averageValues = rows.flatMap(candle => Object.values(averagesFor(candle)));
  let maximum = Math.max(...highs, ...averageValues), minimum = Math.min(...lows, ...averageValues);
  const padding = Math.max((maximum - minimum) * .08, maximum * .002, 1);
  maximum += padding; minimum -= padding;
  const y = value => top + (maximum - value) / (maximum - minimum) * plotHeight;
  const step = plotWidth / rows.length;
  const bodyWidth = Math.max(2, Math.min(10, step * .62));
  let weeklyZone = "";
  if (interval === "1w") {
    const completed = rows.filter(candle => kstWeekStart(candle.timestamp) < kstWeekStart(new Date())).slice(-20);
    if (completed.length === 20 && completed.every(c => Number.isFinite(Number(c.low_price))
        && Number.isFinite(Number(c.high_price)) && Number(c.low_price) > 0
        && Number(c.low_price) <= Number(c.close_price) && Number(c.close_price) <= Number(c.high_price))) {
      const low = Math.min(...completed.map(c => Number(c.low_price)));
      const high = Math.max(...completed.map(c => Number(c.high_price)));
      if (high > low) {
        const ceiling = low + (high - low) * .4, floor = low * .97;
        const zoneTop = Math.max(top, Math.min(priceBottom, y(ceiling)));
        const zoneBottom = Math.max(top, Math.min(priceBottom, y(floor)));
        const zoneLeft = left + step * rows.indexOf(completed[0]);
        weeklyZone = `<g class="weekly-entry-zone"><title>최근 20개 완료 주봉으로 계산한 현재 관심 가격 · 하위 40% · 저점 3% 하회 제외 · 일봉·4시간 조건도 통과해야 매수 추천</title><rect x="${zoneLeft}" y="${zoneTop}" width="${width-right-zoneLeft}" height="${zoneBottom-zoneTop}" fill="#25c2a0" opacity=".12"/><line x1="${zoneLeft}" y1="${zoneTop}" x2="${width-right}" y2="${zoneTop}" style="stroke:#25c2a0;stroke-dasharray:5 4"/><text x="${zoneLeft+8}" y="${Math.max(top+14, zoneTop-6)}" class="chart-axis" style="fill:#25c2a0">현재 주봉 매수 관심 ${number(floor, "KRW")} ~ ${number(ceiling, "KRW")}</text></g>`;
      }
    }
  }
  const grid = Array.from({ length: 5 }, (_, index) => {
    const value = maximum - (maximum - minimum) * index / 4;
    const py = y(value);
    return `<line x1="${left}" y1="${py}" x2="${width-right}" y2="${py}" class="chart-grid"/><text x="${width-right+9}" y="${py+4}" class="chart-axis">${new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(value)}</text>`;
  }).join("");
  const bodies = rows.map((candle, index) => {
    const open = Number(candle.open_price), close = Number(candle.close_price);
    const high = Number(candle.high_price), low = Number(candle.low_price);
    const x = left + step * index + step / 2;
    const rising = close >= open;
    const bodyTop = y(Math.max(open, close));
    const bodyHeight = Math.max(1.5, Math.abs(y(open) - y(close)));
    const cls = rising ? "candle-up" : "candle-down";
    const previousClose = index > 0 ? Number(rows[index - 1].close_price) : null;
    const candleChange = open ? (close / open - 1) * 100 : 0;
    const formatChange = value => `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
    const priceWithChange = value => `${number(value, "KRW")} ${previousClose ? formatChange((value / previousClose - 1) * 100) : "(-)"}`;
    const volume = number(candle.volume || 0, "KRW");
    const indicators = averagesFor(candle);
    const averageDetails = periods.filter(period => indicators[period] != null)
      .map(period => `MA${period} ${number(indicators[period], "KRW")}`).join(" · ");
    const bandDetails = indicators.bollingerUpper == null ? "볼린저밴드 데이터 없음"
      : `볼린저 상단 ${number(indicators.bollingerUpper, "KRW")} · 하단 ${number(indicators.bollingerLower, "KRW")}`;
    const title = `${new Date(candle.timestamp).toLocaleString("ko-KR")}\n시가 ${priceWithChange(open)}\n고가 ${priceWithChange(high)}\n저가 ${priceWithChange(low)}\n종가 ${priceWithChange(close)} · 시가 대비 ${formatChange(candleChange)}\n거래량 ${volume}\n${averageDetails || "이동평균 데이터 부족"}\n${bandDetails}`;
    return `<g class="${cls}"><title>${escapeHtml(title)}</title><line x1="${x}" y1="${y(high)}" x2="${x}" y2="${y(low)}"/><rect x="${x-bodyWidth/2}" y="${bodyTop}" width="${bodyWidth}" height="${bodyHeight}" rx="1"/></g>`;
  }).join("");
  const maxVolume = Math.max(...rows.map(candle => Number(candle.volume || 0)), 1);
  const volumeBars = rows.map((candle, index) => {
    const open = Number(candle.open_price), close = Number(candle.close_price), volume = Number(candle.volume || 0);
    const x = left + step * index + step / 2;
    const barHeight = Math.max(1, volume / maxVolume * (volumeBottom - volumeTop));
    const cls = close >= open ? "volume-up" : "volume-down";
    return `<rect class="${cls}" x="${x-bodyWidth/2}" y="${volumeBottom-barHeight}" width="${bodyWidth}" height="${barHeight}"><title>거래량 ${number(volume, "KRW")}</title></rect>`;
  }).join("");
  const averageLines = periods.map(period => {
    const points = rows.map((candle, index) => {
      const value = averagesFor(candle)[period];
      return value == null ? null : `${left + step * index + step / 2},${y(value)}`;
    }).filter(Boolean).join(" ");
    return points ? `<polyline class="moving-average" style="stroke:${maColors[period]}" points="${points}"/>` : "";
  }).join("");
  const bandLines = [
    ["bollingerUpper", "#4d65ff"],
    ["bollingerLower", "#f2c500"],
  ].map(([key, color]) => {
    const points = rows.map((candle, index) => {
      const value = averagesFor(candle)[key];
      return value == null ? null : `${left + step * index + step / 2},${y(value)}`;
    }).filter(Boolean).join(" ");
    return points ? `<polyline class="moving-average" style="stroke:${color};stroke-width:2.4" points="${points}"/>` : "";
  }).join("");
  const bandLabel = interval === "4h" ? "BB6" : interval === "1w" ? "주 BB20" : "일 BB20";
  const legend = [...periods.map(period => ({ label: `일 MA${period}`, color: maColors[period] })), { label: `${bandLabel} 상단`, color: "#4d65ff" }, { label: `${bandLabel} 하단`, color: "#f2c500" }]
    .map((item, index) => `<g transform="translate(${left + index * 105},18)"><line x1="0" y1="0" x2="18" y2="0" style="stroke:${item.color};stroke-width:2"/><text x="24" y="4" class="chart-legend">${item.label}</text></g>`).join("");
  const labelIndexes = [...new Set([0, Math.floor((rows.length-1)/2), rows.length-1])];
  const dateLabels = labelIndexes.map(index => {
    const x = left + step * index + step / 2;
    const date = new Date(rows[index].timestamp);
    const label = interval.endsWith("m") || interval === "4h"
      ? date.toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })
      : date.toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" });
    return `<text x="${x}" y="${height-8}" text-anchor="${index === 0 ? "start" : index === rows.length-1 ? "end" : "middle"}" class="chart-axis">${escapeHtml(label)}</text>`;
  }).join("");
  const first = Number(rows[0].open_price), last = Number(rows.at(-1).close_price);
  const change = first ? (last / first - 1) * 100 : 0;
  const candleCountLabel = interval === "4h" ? `${rows.length}개 확보 / 최대 50개 요청` : `${rows.length}개`;
  const bandMeta = interval === "4h" ? "4시간 BB(6,2) · 완료봉 기준"
    : interval === "1w" ? "주봉 BB(20,2) · 완료봉 기준 · 녹색은 20주 매수 관심 구간" : "일봉 BB(20,2)";
  $("#candidateChartMeta").textContent = `${chartLabels[interval]} · ${candleCountLabel} · ${bandMeta} · 이동평균은 일봉 기준 · 구간 ${change >= 0 ? "+" : ""}${change.toFixed(2)}% · 캔들에 마우스를 올리면 OHLC 확인`;
  canvas.innerHTML = `<svg class="price-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(chartState.name)} ${chartLabels[interval]} 캔들·거래량·이동평균·볼린저밴드 차트">${legend}${grid}${weeklyZone}${bandLines}${averageLines}${bodies}<line x1="${left}" y1="${volumeTop-10}" x2="${width-right}" y2="${volumeTop-10}" class="chart-grid"/><text x="${left}" y="${volumeTop-15}" class="chart-axis">거래량</text>${volumeBars}${dateLabels}</svg>`;
}

async function loadCandidateChart(symbol, name, interval = "1d") {
  chartState.symbol = symbol; chartState.name = name; chartState.interval = interval;
  const loadId = chartState.loadId = (chartState.loadId || 0) + 1;
  const panel = $("#candidateChartPanel");
  panel.hidden = false;
  $("#candidateChartTitle").textContent = `${name} (${symbol})`;
  $("#candidateChartMeta").textContent = `${chartLabels[interval]} 불러오는 중`;
  $("#candidateChartCanvas").innerHTML = '<p class="recommendation-loading">시세 데이터를 불러오고 있습니다...</p>';
  document.querySelectorAll("[data-chart-interval]").forEach(button => button.classList.toggle("active", button.dataset.chartInterval === interval));
  const minute = ["15m", "30m", "60m", "4h"].includes(interval);
  $("#minutePeriods").hidden = !minute;
  document.querySelector("[data-chart-minute-toggle]").classList.toggle("active", minute);
  const count = interval === "4h" ? 50 : interval === "1M" ? 36 : interval === "1w" ? 52 : 60;
  try {
    const chartRequest = request(`${API}/market/candles?symbol=${encodeURIComponent(symbol)}&interval=${encodeURIComponent(interval)}&count=${count}`);
    const dailyRequest = request(`${API}/market/candles?symbol=${encodeURIComponent(symbol)}&interval=1d&count=300`);
    const [chartResult, dailyResult] = await Promise.allSettled([chartRequest, dailyRequest]);
    if (chartState.loadId !== loadId || panel.hidden) return;
    if (chartResult.status === 'rejected') throw chartResult.reason;
    const daily = dailyResult.status === 'fulfilled' ? dailyResult.value.candles || [] : [];
    renderCandlestickChart(chartResult.value.candles || [], interval, daily);
    if (dailyResult.status === 'rejected') {
      $('#candidateChartMeta').textContent += ' · 일봉 지표 조회 실패: 캔들만 표시';
    }
  } catch (error) {
    if (chartState.loadId !== loadId || panel.hidden) return;
    $('#candidateChartMeta').textContent = `${chartLabels[interval]} 조회 실패`;
    $("#candidateChartCanvas").innerHTML = `<p class="recommendation-error">${escapeHtml(error.message)}</p>`;
  }
}

function openChartFromCard(card) {
  if (!card) return;
  loadCandidateChart(card.dataset.chartSymbol, card.dataset.chartName, "1d");
  requestAnimationFrame(() => $("#candidateChartPanel").scrollIntoView({ behavior: "smooth", block: "nearest" }));
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

function stockLabel(stock) {
  return `<strong>${escapeHtml(stock.name || stock.symbol)}</strong>${stock.name ? `<br><span class="muted">${escapeHtml(stock.symbol)}</span>` : ""}`;
}

function renderPositions(positions) {
  $("#positionCount").textContent = positions.length;
  $("#positionsEmpty").hidden = positions.length > 0;
  $("#positionsTable").innerHTML = positions.map((position) => {
    const hasQuote = Boolean(position.quote_timestamp);
    const pnl = hasQuote ? Number(position.unrealized_profit_loss) : null;
    const cost = Number(position.average_price) * Number(position.quantity);
    const pnlRate = hasQuote && cost ? pnl / cost * 100 : null;
    return `<tr>
      <td>${stockLabel(position)}<br><span class="muted">${position.currency}</span></td>
      <td>${number(position.quantity, position.currency)}</td>
      <td>${number(position.average_price, position.currency)}</td>
      <td>${hasQuote ? number(position.market_price, position.currency) : '<span class="muted">시세 없음</span>'}</td>
      <td class="${pnl > 0 ? "positive" : pnl < 0 ? "negative" : ""}">${hasQuote
        ? `${number(pnl, position.currency)}<br><span class="muted">${pnlRate > 0 ? "+" : ""}${pnlRate.toFixed(2)}%</span>`
        : '<span class="muted">계산 대기</span>'}</td>
      <td><button type="button" class="button button-danger" data-close-position="${escapeHtml(position.symbol)}" ${closingPositions.has(position.symbol) || position.currency !== "KRW" || (state.status?.mode === "live" && (!(state.status.live_readiness?.armed && state.status.live_readiness?.reconciled) || state.status.kill_switch || !state.status.automation?.market_open)) ? "disabled" : ""} aria-label="${escapeHtml(position.name || position.symbol)} 수동 전량 매도">${closingPositions.has(position.symbol) ? "매도 중…" : state.status?.mode === "live" ? "LIVE 전량매도" : "수동 전량매도"}</button></td>
    </tr>`;
  }).join("");
}

function renderOrders(orders) {
  $("#ordersEmpty").hidden = orders.length > 0;
  $("#ordersTable").innerHTML = orders.slice(0, 30).map((order) => `<tr>
    <td>${new Date(order.created_at).toLocaleString("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
    <td>${stockLabel(order)}</td>
    <td class="${order.side === "BUY" ? "positive" : "negative"}">${order.side === "BUY" ? "매수" : "매도"}</td>
    <td>${number(order.quantity, order.currency)}</td>
    <td>${order.filled_price ? number(order.filled_price, order.currency) : "-"}</td>
    <td>${order.status === "FILLED" ? "체결" : `거부 · ${escapeHtml(order.reason || "")}`}</td>
  </tr>`).join("");
}

function renderQuotes(quotes) {
  $("#quotesList").innerHTML = quotes.length ? quotes.map((quote) => `<div class="quote-item">
    <div>${stockLabel(quote)}<span>${escapeHtml(quote.source)} · ${new Date(quote.timestamp).toLocaleTimeString("ko-KR")}</span></div>
    <strong>${money(quote.price, quote.currency)}</strong>
  </div>`).join("") : '<p class="empty">수신한 시세가 없습니다.</p>';
}

function renderOrderReview(orders) {
  $("#liveOrderReview").hidden = !orders.length;
  $("#liveOrderReviewList").innerHTML = orders.map(order => {
    const canReview = order.status === "UNKNOWN" && !order.has_broker_id && !state.status?.running;
    return `<div class="control-row"><span>${escapeHtml(order.symbol)} · ${escapeHtml(order.side)} · ${escapeHtml(order.quantity)}주 · ${escapeHtml(order.status)}${order.error_code ? ` · ${escapeHtml(order.error_code)}` : ""}</span><button class="button button-secondary" type="button" data-review-unsubmitted="${escapeHtml(order.client_order_id)}" ${canReview ? "" : "disabled"}>토스 내역 확인 후 미접수 정리</button></div>`;
  }).join("");
}

$("#liveOrderReviewList").addEventListener("click", async event => {
  const button = event.target.closest("button[data-review-unsubmitted]");
  if (!button || button.disabled) return;
  if (!confirm("토스 앱 주문내역에서 이 주문이 접수·체결되지 않았음을 직접 확인했나요? 서버에서도 미체결·종료 주문을 조회한 뒤 미접수로 정리합니다. 실제 주문은 전송하지 않습니다.")) return;
  button.disabled = true;
  try {
    await request(`${API}/live/order-review/confirm-unsubmitted`, {method: "POST",
      body: JSON.stringify({client_order_id: button.dataset.reviewUnsubmitted, confirm_no_broker_order: true})});
    toast("미접수 확인을 기록했습니다. 계좌 동기화·무장 후 시작할 수 있습니다.");
    await loadAll(true);
  } catch (error) { button.disabled = false; toast(error.message, true); }
});

async function loadAll(silent = false) {
  try {
    const previousStatus = state.status || await request(`${API}/system/status`);
    const heldCount = state.account?.positions?.length || previousStatus.automation?.holding_count || 0;
    if (previousStatus.mode === "paper" && previousStatus.market_data === "toss" && heldCount > 0
        && Date.now() - positionQuoteRefreshAt >= 25000) {
      if (!positionQuoteRefreshPromise) {
        positionQuoteRefreshAt = Date.now();
        positionQuoteRefreshPromise = request(`${API}/market/refresh`, { method: "POST" })
          .catch(() => { positionQuoteRefreshAt = 0; })
          .finally(() => { positionQuoteRefreshPromise = null; });
      }
      await positionQuoteRefreshPromise;
    }
    const status = await request(`${API}/system/status`);
    const risk = await request(`${API}/risk/status`);
    state.risk = risk;
    renderStatus(status, risk);
    if (status.mode === "live") {
      const review = await request(`${API}/live/order-review`);
      renderOrderReview(review.orders || []);
    } else {
      renderOrderReview([]);
    }
    const accountPath = status.mode === "live" ? `${API}/live/account` : `${API}/paper/account`;
    const [account, performance, orders, quotes] = await Promise.all([
      request(accountPath), request(`${API}/performance`), request(`${API}/orders`),
      request(`${API}/market/quotes`),
    ]);
    renderAccount(account, performance);
    renderCapital(account, risk, status, performance);
    renderOrders(orders.orders);
    renderQuotes(quotes.quotes);
    if (!silent) toast("최신 상태를 불러왔습니다.");
  } catch (error) {
    setTossConnection(false);
    if (state.status?.mode === "live") {
      $("#accountModeBadge").textContent = "LIVE · SYNC FAILED";
      $("#accountModeBadge").title = error.message;
      if (!state.account) {
        $("#krwEquity").textContent = "조회 불가";
        $("#usdEquity").textContent = "조회 불가";
      }
    }
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

const investmentSettingsDialog = $("#investmentSettingsDialog");
$("#investmentSettingsButton").addEventListener("click", () => {
  if (!state.status) return;
  const currentPercent = Math.round(Number(state.risk?.recommended_trade_ratio ?? 0.1) * 100);
  $("#investmentRatioInput").value = String(currentPercent);
  $("#investmentRatioValue").textContent = `${currentPercent}%`;
  investmentSettingsDialog.showModal();
});
$("#investmentRatioInput").addEventListener("input", event => {
  $("#investmentRatioValue").textContent = `${event.currentTarget.value}%`;
  $("#capitalRatioInput").value = event.currentTarget.value;
  previewInvestmentRatio(event.currentTarget.value);
});
$("#capitalRatioInput").addEventListener("input", event => {
  const percent = event.currentTarget.value;
  $("#capitalRatioValue").textContent = `${percent}%`;
  $("#investmentRatioInput").value = percent;
  $("#investmentRatioValue").textContent = `${percent}%`;
  previewInvestmentRatio(percent);
});
$("#capitalRatioInput").addEventListener("change", async event => {
  const slider = event.currentTarget;
  slider.disabled = true;
  try {
    await saveInvestmentRatio(Number(slider.value));
    toast(`총 투자예산 비율을 ${slider.value}%로 저장했습니다.`);
    await loadAll(true);
  } catch (error) {
    toast(error.message, true);
    await loadAll(true);
  }
});
$("#manualRatioInput").addEventListener("input", event => {
  previewManualRatio(event.currentTarget.value);
  if (state.recommendations) renderRecommendations(state.recommendations, false);
});
$("#manualRatioInput").addEventListener("change", async event => {
  const slider = event.currentTarget;
  slider.disabled = true;
  try {
    await request(`${API}/settings/manual-investment-ratio`, {
      method: "PUT", body: JSON.stringify({ ratio_percent: Number(slider.value) }),
    });
    toast(`직접 매수 비율을 ${slider.value}%로 저장했습니다.`);
  } catch (error) { toast(error.message, true); }
  finally { await loadAll(true); slider.disabled = false; }
});
$("#investmentSettingsClose").addEventListener("click", () => investmentSettingsDialog.close());
$("#investmentSettingsCancel").addEventListener("click", () => investmentSettingsDialog.close());
$("#investmentSettingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const saveButton = event.currentTarget.querySelector('button[type="submit"]');
  saveButton.disabled = true;
  try {
    const ratioPercent = Number($("#investmentRatioInput").value);
    await saveInvestmentRatio(ratioPercent);
    investmentSettingsDialog.close();
    toast(`총 투자예산 비율을 ${ratioPercent}%로 저장했습니다.`);
    await loadAll(true);
  } catch (error) {
    toast(error.message, true);
  } finally {
    saveButton.disabled = false;
  }
});

function previewInvestmentRatio(percent) {
  const equity = Number(state.performance?.by_currency?.KRW?.current_equity || state.account?.total_equity?.KRW || 0);
  const cash = Number(state.account?.cash?.KRW || 0);
  const amount = Math.min(cash, equity * Number(percent) / 100);
  const liveRemaining = Math.max(0, Math.min(cash, equity * Number(percent) / 100 - Number(state.status?.automation?.spent || 0) - 1));
  $("#capitalRatioPreview").textContent = state.status?.mode === "live"
    ? `${money(liveRemaining, "KRW")} 예상 예산 · 미체결 매수·주문 한도는 서버에서 재검사`
    : `${money(amount, "KRW")} 예상 주문 예산 · 가용 현금 한도 적용`;
}

async function saveInvestmentRatio(ratioPercent) {
  await request(`${API}/settings/investment-ratio`, {
    method: "PUT",
    body: JSON.stringify({ ratio_percent: ratioPercent }),
  });
}

$("#positionsTable").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-close-position]");
  if (!button || button.disabled) return;
  const symbol = button.dataset.closePosition;
  if (closingPositions.has(symbol)) return;
  const live = state.status?.mode === "live";
  if (live && !confirm(`${symbol}: 실제 보유 수량을 시장가로 전량 매도할까요? 주문 한도와 매도 가능 수량을 다시 확인합니다.`)) return;
  closingPositions.add(symbol);
  button.disabled = true;
  button.textContent = "매도 중…";
  try {
    const result = await request(`${API}/${live ? "live" : "paper"}/positions/${encodeURIComponent(symbol)}/close`, {
      method: "POST", ...(live ? { body: JSON.stringify({ confirm_real_order: true }) } : {}),
    });
    toast(`${symbol} ${result.message} · 당일 자동 재매수 제외`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    await loadAll(true);
    closingPositions.delete(symbol);
    if (state.account) renderPositions(state.account.positions);
  }
});

$("#recommendationList").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-qualified-buy]");
  if (!button || button.disabled) return;
  const symbol = button.dataset.qualifiedBuy;
  const live = state.status?.mode === "live";
  if (buyBlockReason(symbol)) return;
  const ratioPercent = manualRatioPercent();
  if (!confirm(`${symbol}: 총자산의 ${ratioPercent}% 이내, 예상 예산 ${money(manualBuyAmount(ratioPercent), "KRW")}으로 조건을 다시 확인하고 ${live ? "실계좌 시장가" : "PAPER"} 매수할까요?${live ? ` ${liveExposureLabel(state.risk?.live_limits)}와 최신 추천·가용 현금 기준을 적용합니다.` : ""}`)) return;
  button.disabled = true;
  button.textContent = live ? "LIVE 매수 확인 중…" : "PAPER 매수 중…";
  try {
    const result = await request(`${API}/${live ? "live" : "paper"}/qualified-buy/${encodeURIComponent(symbol)}`, {
      method: "POST", body: JSON.stringify({ ratio_percent: ratioPercent, ...(live ? { confirm_real_order: true } : {}) }),
    });
    toast(`${symbol} ${result.message}`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    await loadAll(true);
    if (state.recommendations) renderRecommendations(state.recommendations, false);
  }
});

$("#recommendationWatchlist").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-test-buy]");
  if (!button || button.disabled) return;
  const symbol = button.dataset.testBuy;
  if (state.status?.mode !== "paper") return;
  if (!confirm(`${symbol}을 현재 시세로 1주 PAPER 테스트 매수할까요? 실제 주문은 발생하지 않습니다.`)) return;
  button.disabled = true;
  button.textContent = "테스트 주문 중…";
  try {
    await request(`${API}/paper/test-buy/${encodeURIComponent(symbol)}`, { method: "POST" });
    toast(`${symbol} 1주 PAPER 테스트 매수 완료`);
    await loadAll(true);
  } catch (error) {
    toast(error.message, true);
    button.disabled = false;
    button.textContent = "1주 PAPER 테스트 매수";
  }
});

function recommendationChartInteraction(event) {
  if (event.target.closest("button")) return;
  const card = event.target.closest("[data-chart-symbol]");
  if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return;
  if (event.type === "keydown") event.preventDefault();
  openChartFromCard(card);
}

for (const selector of ["#recommendationList", "#recommendationWatchlist"]) {
  $(selector).addEventListener("click", recommendationChartInteraction);
  $(selector).addEventListener("keydown", recommendationChartInteraction);
}

document.querySelectorAll("[data-chart-interval]").forEach(button => button.addEventListener("click", () => {
  if (chartState.symbol) loadCandidateChart(chartState.symbol, chartState.name, button.dataset.chartInterval);
}));

document.querySelector("[data-chart-minute-toggle]").addEventListener("click", () => {
  $("#minutePeriods").hidden = false;
  if (chartState.symbol) loadCandidateChart(chartState.symbol, chartState.name, "15m");
});

$("#candidateChartClose").addEventListener("click", () => { $("#candidateChartPanel").hidden = true; });

$("#startButton").addEventListener("click", async () => {
  if (state.status?.mode === "live"
      && !confirm("실계좌 자동매매를 시작합니다. 추천 종목과 위험 한도를 통과한 주문은 실제 계좌에 제출됩니다. 시작할까요?")) return;
  $("#startButton").disabled = true;
  await action(`${API}/engine/start`, state.status?.mode === "live" ? "LIVE 실계좌 자동매매를 시작했습니다." : "추천 예산으로 가상 자동매매를 시작했습니다.");
  await loadAll(true);
});
$("#stopButton").addEventListener("click", () => action(`${API}/engine/stop`, "엔진을 안전하게 중지했습니다."));
$("#liveArmButton").addEventListener("click", async () => {
  const armed = state.status?.live_readiness?.armed;
  const message = armed
    ? "LIVE 무장을 해제하고 엔진을 중지할까요?"
    : "토스 계좌를 다시 동기화하고 LIVE 주문을 허용 상태로 무장합니다. 추천 종목 매수·보유 종목 매도 버튼과 자동매매에서 실제 주문을 제출할 수 있습니다. 계속할까요?";
  if (!confirm(message)) return;
  try {
    await request(`${API}/live/${armed ? "disarm" : "arm"}`, { method: "POST" });
    toast(armed ? "LIVE 무장을 해제했습니다." : "LIVE 계좌 동기화와 무장이 완료됐습니다.");
    await loadAll(true);
  } catch (error) { toast(error.message, true); }
});
$("#killButton").addEventListener("click", () => {
  if (confirm("신규 주문을 즉시 차단하고 엔진을 중지할까요?")) action(`${API}/risk/kill-switch`, "킬 스위치를 활성화했습니다.");
});
$("#clearKillButton").addEventListener("click", () => action(`${API}/risk/kill-switch/clear`, "킬 스위치를 해제했습니다."));
$("#refreshAllButton").addEventListener("click", async () => {
  await loadAll();
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
  $("#recommendationFunnel").textContent = "등록 테마 종목 → 시가총액 기준 → 주봉·일봉 추세 → 4시간봉 진입 구간 확인 중";
  results.scrollIntoView({ behavior: "smooth", block: "nearest" });
  button.disabled = true;
  button.textContent = "후보 분석 중...";
  try {
    const payload = await request(`${API}/recommendations`);
    renderRecommendations(payload);
  } catch (error) {
    $("#recommendationBudget").textContent = "분석 실패";
    $("#recommendationFunnel").textContent = `${new Date().toLocaleTimeString("ko-KR")} 조회 실패 · 분석이 완료되지 않았습니다`;
    $("#recommendationDisclaimer").textContent = "아래 내용은 마지막 조회의 실패 결과입니다. ‘추천 후보 찾기’를 다시 누르면 현재 상태로 재조회합니다. 이 오류는 조건 충족 종목이 없다는 뜻이 아닙니다.";
    $("#recommendationList").innerHTML = `<p class="recommendation-error">${escapeHtml(error.message)}</p>`;
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "추천 후보 찾기";
  }
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
  } catch (error) { toast(error.message, true); }
});

applyTheme(document.documentElement.dataset.theme);
loadAll(true);
loadWeeklyReport();
setInterval(loadWeeklyReport, 300000);
checkTossConnection(false);
setInterval(() => loadAll(true), 30000);
