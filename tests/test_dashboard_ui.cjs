// Mock DOM, requests and timers: never contact an account or submit a real order.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const elements = new Map();
function element(selector) {
  if (!elements.has(selector)) elements.set(selector, {
    textContent: '', innerHTML: '', value: '', dataset: {}, disabled: false,
    classList: { toggle() {}, add() {}, remove() {} },
    events: {}, addEventListener(name, fn) {
      const previous = this.events[name];
      this.events[name] = previous ? async event => { await previous(event); await fn(event); } : fn;
    },
    setAttribute() {}, scrollIntoView() {}, showModal() {}, close() {},
  });
  return elements.get(selector);
}
const document = { querySelector: element, querySelectorAll: () => [],
                   documentElement: { dataset: { theme: 'dark' } } };
const context = vm.createContext({ document, Intl, Date, Number, String, Boolean, Set, Map,
  console, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0,
  localStorage: { getItem: () => null, setItem() {} },
  sessionStorage: { getItem: () => null, setItem() {} }, confirm: () => true });
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
vm.runInContext(source.slice(0, source.lastIndexOf('\napplyTheme(document.documentElement.dataset.theme);')), context);

async function run() {
  await vm.runInContext(`(async () => {
    state.risk = {recommended_trade_ratio: '.15', max_order_amount: {KRW: '100000'},
      live_limits: {allowed_symbols: ['005930'], max_total_exposure_ratio: '.15', max_order_amount_krw: '100000'}};
    renderStatus({mode: 'live', market_data: 'toss', running: true, kill_switch: false,
      live_readiness: {armed: true, reconciled: true},
      automation: {market_open: true, remaining: '140000', budget: '150000', spent: '10000'}}, state.risk);
    renderRecommendations({budget: '140000', minimum_market_cap_krw: '500000000000', candidates: [
      {symbol: '005930', name: 'mock', currency: 'KRW', price: '70000', quantity: 1},
      {symbol: '082740', name: 'restricted', currency: 'KRW', price: '40000', quantity: 1}],
      watchlist: [{symbol: '000001', name: 'watch', currency: 'KRW', price: '10000'}]}, false);
    globalThis.requests = [];
    request = async (url, opts) => { requests.push({url, opts}); return {message: 'LIVE result'}; };
    loadAll = async () => {};
    await $('#recommendationList').events.click({target: {closest: () => ({
      dataset: {qualifiedBuy: '005930'}, disabled: false})}});
    await $('#positionsTable').events.click({target: {closest: () => ({
      dataset: {closePosition: '005930'}, disabled: false})}});
    await $('#recommendationWatchlist').events.click({target: {closest: () => ({
      dataset: {testBuy: '000001'}, disabled: false})}});
  })()`, context);
  assert.equal(element('#capitalMode').textContent, 'LIVE');
  assert.equal(element('#screeningEyebrow').textContent, 'LIVE SCREENING');
  assert.equal(element('#strategyMarketCap').textContent, '국내 보통주 · 시가총액 5,000억 원 이상');
  const html = fs.readFileSync(path.join(__dirname, '../app/static/index.html'), 'utf8');
  assert.match(html, /id="strategyMarketCap">국내 보통주 · 시가총액 5,000억 원 이상/);
  assert.equal(element('#capitalRatioInput').max, '15');
  assert.match(element('#strategyExitValue').textContent, /예상 순수익 3% 이상 익절/);
  assert.match(element('#strategyExitHelp').textContent, /3% 미만에서도 가능/);
  assert.equal(element('#startButton').textContent, 'LIVE 자동매매 실행 중');
  assert.match(element('#recommendationList').innerHTML, /LIVE 실계좌 조건 확인·매수/);
  assert.match(element('#recommendationList').innerHTML, /disabled.*LIVE 허용목록에 없음/);
  assert.doesNotMatch(element('#recommendationWatchlist').innerHTML, /data-test-buy|PAPER/);
  assert.equal(context.requests.length, 2);
  assert.equal(context.requests[0].url, '/api/v1/live/qualified-buy/005930');
  assert.equal(context.requests[1].url, '/api/v1/live/positions/005930/close');
  for (const r of context.requests) assert.equal(JSON.parse(r.opts.body).confirm_real_order, true);
  assert.equal(JSON.parse(context.requests[0].opts.body).ratio_percent, 15);
  vm.runInContext("state.status.automation.remaining = '0'; renderRecommendations(state.recommendations, false)", context);
  assert.match(element('#recommendationList').innerHTML, /disabled.*추가 매수 한도 없음/);
  vm.runInContext(`
    state.risk.recommended_trade_ratio = '.15';
    state.risk.manual_trade_ratio = '.5';
    state.risk.live_limits = {symbol_policy: 'recommended', allowed_symbols: [],
      max_total_exposure_ratio: '1', auto_max_total_exposure_ratio: '.15', max_order_amount_krw: '0'};
    state.status.automation.remaining = '180000';
    state.status.automation.manual_buy_capacity = '999999';
    state.account = {cash: {KRW: '1000000'}, total_equity: {KRW: '1200000'}};
    renderStatus(state.status, state.risk);
    renderCapital(state.account, state.risk, state.status, {by_currency: {}});
  `, context);
  assert.equal(element('#capitalRatioInput').max, '15');
  assert.equal(element('#capitalRatioInput').value, '15');
  assert.equal(element('#manualRatioInput').max, '100');
  assert.equal(element('#manualRatioInput').value, '50');
  assert.match(element('#capitalRecommendationHelp').textContent, /100% 이내/);
  assert.match(element('#capitalRecommendationHelp').textContent, /가용 현금 한도/);
  assert.doesNotMatch(element('#recommendationList').innerHTML, /LIVE 허용목록에 없음/);
  assert.match(element('#investmentRatioHelp').textContent, /최신 추천/);
  assert.match(element('#investmentRatioHelp').textContent, /1\/5/);
  vm.runInContext(`renderRecommendations({budget: '1000000', candidates: [
    {symbol: '012450', name: 'expensive', currency: 'KRW', price: '2000000', quantity: 0}],
    watchlist: []}, false)`, context);
  assert.match(element('#recommendationList').innerHTML, /disabled.*직접 매수 예산으로 1주 매수 불가/);
  await vm.runInContext(`(async () => {
    renderRecommendations({budget: '180000', candidates: [{symbol: '012450', name: 'manual',
      currency: 'KRW', price: '100000', quantity: 0}], watchlist: []}, false);
    $('#manualRatioInput').value = '25';
    await $('#manualRatioInput').events.input({currentTarget: $('#manualRatioInput')});
    await $('#manualRatioInput').events.change({currentTarget: $('#manualRatioInput')});
  })()`, context);
  assert.equal(context.requests.at(-1).url, '/api/v1/settings/manual-investment-ratio');
  assert.equal(JSON.parse(context.requests.at(-1).opts.body).ratio_percent, 25);
  assert.equal(element('#capitalRatioInput').value, '15');
  assert.match(element('#manualRatioPreview').textContent, /300,000/);
  assert.match(element('#recommendationList').innerHTML, /자동 배분 최대 0주 · 직접 25% 선택 시 예상 3주/);
  assert.doesNotMatch(element('#recommendationList').innerHTML, /disabled/);
  vm.runInContext(`
    state.status.live_readiness = {armed: false, reconciled: true, unresolved_order_count: 1};
    renderStatus(state.status, state.risk);
    renderOrderReview([{client_order_id: 'mock-key', symbol: '012450', side: 'BUY', quantity: '4',
      status: 'UNKNOWN', has_broker_id: false}]);
  `, context);
  assert.equal(element('#engineLabel').textContent, 'LIVE 주문 잠금');
  assert.match(element('#engineDetail').textContent, /미확인 주문 1건/);
  assert.notEqual(element('#startButton').textContent, 'LIVE 자동매매 실행 중');
  assert.equal(element('#liveOrderReview').hidden, false);
  assert.match(element('#liveOrderReviewList').innerHTML, /미접수 정리/);
  vm.runInContext(`
    renderRecommendations({budget: '1000000', candidates: [{symbol: '005930', name: 'low',
      strategy: 'swing-v2-mtf-4h', currency: 'KRW', price: '100', quantity: 1,
      entry_ceiling: '101', bollinger_lower: '99'}], watchlist: [{symbol: '000660',
      currency: 'KRW', entry_ceiling: '90', four_hour_band_position_percent: '70',
      daily_range_position_percent: '80', rejection_reasons: ['high zone']}]}, false);
  `, context);
  assert.match(element('#recommendationList').innerHTML, /밴드 하위 25%/);
  assert.match(element('#recommendationList').innerHTML, /최근 20일 고저 범위 하위 40%/);
  assert.match(element('#recommendationList').innerHTML, /매수 상한 101/);
  assert.match(element('#recommendationWatchlist').innerHTML, /4시간 밴드 내 위치 70%/);
  assert.match(element('#recommendationWatchlist').innerHTML, /high zone/);
  assert.match(element('#recommendationWatchlist').innerHTML, /매수 추천 아님/);
  assert.match(element('#recommendationList').innerHTML, /매수 자리 도달/);
  assert.match(element('#recommendationList').innerHTML, /최근 20주 하위 40%/);
  assert.equal(vm.runInContext("fourHourCandleCompleted('2026-10-07T09:00:00+09:00', new Date('2026-10-07T12:59:59+09:00'))", context), false);
  assert.equal(vm.runInContext("fourHourCandleCompleted('2026-10-07T09:00:00+09:00', new Date('2026-10-07T13:00:00+09:00'))", context), true);
  assert.equal(vm.runInContext("fourHourCandleCompleted('2026-10-07T13:00:00+09:00', new Date('2026-10-07T15:29:59+09:00'))", context), false);
  assert.equal(vm.runInContext("fourHourCandleCompleted('2026-10-07T13:00:00+09:00', new Date('2026-10-07T15:30:00+09:00'))", context), true);
  vm.runInContext(`
    const chartNow = new Date(), offset = 9 * 60 * 60 * 1000;
    const localNow = new Date(chartNow.getTime() + offset);
    const futureStart = new Date(Date.UTC(localNow.getUTCFullYear(), localNow.getUTCMonth(), localNow.getUTCDate() + 1, 9) - offset);
    const chartBars = [100, 101, 102, 103, 104, 105].map((value, index) => ({
      timestamp: new Date(futureStart.getTime() - (8 - index) * 86400000).toISOString(),
      open_price: value, high_price: value, low_price: value, close_price: value, volume: 10}));
    chartBars.push({timestamp: futureStart.toISOString(), open_price: 1000, high_price: 1000,
      low_price: 1000, close_price: 1000, volume: 10});
    const dailyBars = Array.from({length: 30}, (_, index) => ({
      timestamp: new Date(futureStart.getTime() - (30 - index) * 86400000).toISOString(),
      open_price: 500, high_price: 500, low_price: 500, close_price: 500, volume: 10}));
    renderCandlestickChart(chartBars, '4h', dailyBars);
    globalThis.expectedBandText = '볼린저 상단 ' + number(102.5 + 2 * Math.sqrt(35 / 12), 'KRW')
      + ' · 하단 ' + number(102.5 - 2 * Math.sqrt(35 / 12), 'KRW');
  `, context);
  const chart = element('#candidateChartCanvas').innerHTML;
  assert.match(chart, /BB6 상단/);
  assert.match(element('#candidateChartMeta').textContent, /4시간 BB\(6,2\) · 완료봉 기준/);
  assert.equal(chart.split(context.expectedBandText).length - 1, 2);
  assert.equal(vm.runInContext("kstWeekStart('2026-10-04T23:59:59+09:00') < kstWeekStart('2026-10-05T00:00:00+09:00')", context), true);
  vm.runInContext(`
    const weekStart = kstWeekStart(new Date());
    const weeklyChart = Array.from({length: 22}, (_, index) => ({
      timestamp: new Date(weekStart - (22-index) * 7 * 86400000).toISOString(),
      open_price: 100+index, high_price: 102+index, low_price: 98+index,
      close_price: 100+index, volume: 10}));
    for (const weekOffset of [0, 1]) weeklyChart.push({
      timestamp: new Date(weekStart + weekOffset * 7 * 86400000).toISOString(),
      open_price: 1000, high_price: 2000, low_price: 1, close_price: 1000, volume: 10});
    renderCandlestickChart(weeklyChart, '1w', dailyBars);
    globalThis.expectedWeeklyBand = '볼린저 상단 ' + number(111.5 + 2 * Math.sqrt(399 / 12), 'KRW')
      + ' · 하단 ' + number(111.5 - 2 * Math.sqrt(399 / 12), 'KRW');
  `, context);
  const weeklyChart = element('#candidateChartCanvas').innerHTML;
  assert.match(weeklyChart, /주 BB20 상단/);
  assert.match(weeklyChart, /weekly-entry-zone/);
  assert.ok(Number(weeklyChart.match(/class="weekly-entry-zone"[\s\S]*?<rect x="([^"]+)"/)[1]) > 18);
  assert.match(weeklyChart, /주봉 매수 관심 97 ~ 109/);
  assert.match(element('#candidateChartMeta').textContent, /주봉 BB\(20,2\)/);
  assert.equal(weeklyChart.split(context.expectedWeeklyBand).length - 1, 3);
  await vm.runInContext(`(async () => {
    globalThis.chartTestBars = [{timestamp: '2026-10-06T09:00:00+09:00',
      open_price: 100, high_price: 110, low_price: 90, close_price: 105, volume: 10}];
    request = async url => {
      if (url.includes('interval=4h')) return {candles: chartTestBars};
      throw new Error('일봉 요청 제한');
    };
    await loadCandidateChart('082740', '한화엔진', '4h');
  })()`, context);
  assert.match(element('#candidateChartCanvas').innerHTML, /<svg/);
  assert.match(element('#candidateChartMeta').textContent, /일봉 지표 조회 실패/);
  await vm.runInContext(`(async () => {
    request = async url => {
      if (url.includes('interval=4h')) throw new Error('토스 캔들 조회 요청 한도를 초과했습니다. 3초 후 다시 조회해 주세요.');
      return {candles: []};
    };
    await loadCandidateChart('082740', '한화엔진', '4h');
  })()`, context);
  assert.match(element('#candidateChartCanvas').innerHTML, /3초 후/);
  assert.equal(element('#candidateChartMeta').textContent, '4시간봉 조회 실패');
  await vm.runInContext(`(async () => {
    const pending = [];
    request = url => new Promise((resolve, reject) => pending.push({url, resolve, reject}));
    const previous = loadCandidateChart('082740', '한화엔진', '4h');
    const current = loadCandidateChart('082740', '한화엔진', '4h');
    pending[2].resolve({candles: chartTestBars});
    pending[3].resolve({candles: []});
    await current;
    pending[0].reject(new Error('이전 요청 실패'));
    pending[1].resolve({candles: []});
    await previous;
  })()`, context);
  assert.match(element('#candidateChartCanvas').innerHTML, /<svg/);
  assert.doesNotMatch(element('#candidateChartCanvas').innerHTML, /이전 요청 실패/);
  vm.runInContext(`
    state.risk = {recommended_trade_ratio: '.15', manual_trade_ratio: '.05', fee_rate: '.00015',
      max_order_amount: {KRW: '0'}, live_limits: {symbol_policy: 'recommended', allowed_symbols: [],
      allocation_mode: 'per_order', max_buy_ratio: '.15', max_total_exposure_ratio: '.75',
      auto_max_total_exposure_ratio: '.75', max_order_amount_krw: '0'}};
    state.account = {cash: {KRW: '3500000'}, total_equity: {KRW: '5000000'}, positions: []};
    state.recommendations = null;
    renderStatus({mode: 'live', market_data: 'toss', running: false, kill_switch: false,
      live_readiness: {armed: false, reconciled: true}, automation: {market_open: true,
      remaining: '2249999', budget: '3750000', spent: '1500000', per_symbol_budget: '750000',
      manual_buy_capacity: '750000', buy_block_reasons: ['자동매매 엔진 중지', 'LIVE 무장 해제']}}, state.risk);
    renderCapital(state.account, state.risk, state.status, {by_currency: {}});
    renderRecommendations({budget: '750000', candidates: [{symbol: '082740', name: 'mock',
      currency: 'KRW', price: '100000', quantity: 7}]}, false);
  `, context);
  assert.equal(element('#capitalRatioInput').max, '15');
  assert.equal(element('#manualRatioInput').max, '15');
  assert.equal(element('#capitalRatioLabel').textContent, '자동매수 1회 총자산 비율');
  assert.equal(element('#investmentSettingsTitle').textContent, '자동매수 1회 총자산 비율');
  assert.match(element('#autoAllocationHelp').textContent, /1회 최대 15%.*75%/);
  assert.match(element('#capitalRecommendationAmount').textContent, /750,000/);
  assert.match(element('#capitalRecommendationHelp').textContent, /1회 매수 총자산의 15%/);
  assert.match(element('#capitalRecommendationHelp').textContent, /75%/);
  assert.doesNotMatch(element('#capitalRecommendationHelp').textContent, /1\/5/);
  assert.match(element('#capitalRatioPreview').textContent, /2,249,999/);
  assert.match(element('#strategyCapitalHelp').textContent, /1회 최대 15%.*75%/);
  assert.equal(element('#autoBuyBlockReasons').hidden, false);
  assert.match(element('#autoBuyBlockReasons').textContent, /엔진 중지.*무장 해제/);
  assert.match(element('#recommendationList').innerHTML, /자동 1회 최대 7주/);
  assert.match(element('#recommendationBudget').textContent, /1회 최대 750,000.*합산 잔여/);
  assert.equal(element('#manualRatioInput').value, '5');
  vm.runInContext('previewInvestmentRatio(10);', context);
  assert.match(element('#capitalRatioPreview').textContent, /500,000/);
  vm.runInContext(`
    state.status.automation.per_symbol_budget = '0';
    state.status.automation.remaining = '0';
    renderCapital(state.account, state.risk, state.status, {by_currency: {}});
  `, context);
  assert.equal(element('#capitalRecommendationAmount').textContent, '0 KRW');
  vm.runInContext(`renderRecommendations({budget: '0', candidates: [{symbol: '012450',
    name: 'expensive', currency: 'KRW', price: '2000000', quantity: 0}]}, false);`, context);
  assert.match(element('#recommendationList').innerHTML, /자동매수 제외: 자동매수 예산으로 1주 매수 불가/);
  vm.runInContext(`state.status.automation.buy_block_reasons = []; renderStatus(state.status, state.risk);`, context);
  assert.equal(element('#autoBuyBlockReasons').hidden, true);
  vm.runInContext(`
    state.risk.swing_exit_policy = {min_net_profit_percent: '10', stop_loss_enabled: false};
    state.risk.swing_averaging_policy = {enabled: true, trigger_loss_percent: '15'};
    state.status.automation.averaging = {checks: [{symbol: '082740', reason: '추가 매수 1회 결과 PARTIALLY_FILLED · 반복 주문 없음'}]};
    renderStatus(state.status, state.risk);
    renderCapital(state.account, state.risk, state.status, {by_currency: {}});
    renderRecommendations({budget: '0', candidates: [{symbol: '082740', name: 'mock',
      strategy: 'swing-v2-mtf-4h', currency: 'KRW', price: '100000', quantity: 0}]}, false);
  `, context);
  assert.match(element('#strategyExitHelp').textContent, /고정 손절 사용 안 함/);
  assert.match(element('#strategyExitValue').textContent, /예상 순수익 10% 이상 익절/);
  assert.match(element('#strategyExitHelp').textContent, /순수익 10% 확보 가능 시 활성화/);
  assert.match(element('#strategyExitHelp').textContent, /10% 미만에서도 가능/);
  assert.doesNotMatch(element('#strategyExitHelp').textContent, /-3% 손절/);
  assert.match(element('#strategyAveragingHelp').textContent, /-15%.*1회 추가 매수/);
  assert.match(element('#capitalRecommendationHelp').textContent, /1회 추가 매수/);
  assert.equal(element('#averagingStatus').hidden, false);
  assert.match(element('#averagingStatus').textContent, /PARTIALLY_FILLED.*반복 주문 없음/);
  assert.match(element('#recommendationList').innerHTML, /고정 손절 사용 안 함/);
  assert.match(element('#recommendationList').innerHTML, /예상 순수익 10% 이상 익절/);
  assert.doesNotMatch(element('#recommendationList').innerHTML, /순수익 3%/);
  assert.doesNotMatch(element('#recommendationList').innerHTML, /-3%.*손절/);
  vm.runInContext(`
    state.risk.live_limits.daily_loss_limit_enabled = false;
    state.risk.live_limits.daily_loss_krw = '53554';
    state.risk.live_limits.max_daily_loss_krw = '50000';
    state.risk.live_limits.daily_loss_limit_reached = false;
    renderStatus(state.status, state.risk);
  `, context);
  assert.equal(element('#dailyLossStatus').hidden, false);
  assert.match(element('#dailyLossStatus').textContent, /일일 손실 매수 제한 해제.*53,554/);
  vm.runInContext(`
    state.risk.live_limits.daily_loss_limit_enabled = true;
    state.risk.live_limits.daily_loss_limit_reached = true;
    renderStatus(state.status, state.risk);
  `, context);
  assert.match(element('#dailyLossStatus').textContent, /일일 한도 50,000.*신규·추가 매수 보류/);
  vm.runInContext(`state.status.mode = 'paper'; renderStatus(state.status, state.risk);`, context);
  assert.equal(element('#dailyLossStatus').hidden, true);
  vm.runInContext(`
    state.status.mode = 'live';
    state.status.automation = {remaining: '2999999', budget: '3500000', spent: '500000',
      per_symbol_budget: '749999.75', manual_buy_capacity: '2999999',
      remaining_allocation_slots: 4, holding_count: 1, buy_block_reasons: []};
    state.risk.recommended_trade_ratio = '.70';
    state.risk.manual_trade_ratio = '.15';
    state.risk.live_limits = {allocation_mode: 'per_order', budget_split: 'remaining_slots',
      min_cash_ratio: '.30', max_buy_ratio: '1', max_total_exposure_ratio: '.70',
      auto_max_total_exposure_ratio: '.70', effective_total_exposure_ratio: '.70',
      effective_auto_exposure_ratio: '.70', max_order_amount_krw: '0',
      symbol_policy: 'recommended', daily_loss_limit_enabled: false, daily_loss_krw: '53554'};
    state.account = {cash: {KRW: '4500000'}, total_equity: {KRW: '5000000'}, positions: []};
    renderStatus(state.status, state.risk);
    renderCapital(state.account, state.risk, state.status, {by_currency: {}});
    renderRecommendations({budget: '2999999', candidates: [{symbol: '082740', name: 'mock',
      price: '100000', currency: 'KRW', quantity: 7}]}, false);
  `, context);
  assert.equal(element('#cashReserveStatus').hidden, false);
  assert.match(element('#cashReserveStatus').textContent, /최소 30% 현금 유지/);
  assert.equal(element('#manualRatioInput').max, '70');
  assert.equal(element('#capitalRatioInput').max, '70');
  assert.match(element('#capitalRatioLabel').textContent, /균등 배분/);
  assert.match(element('#capitalRecommendationHelp').textContent, /30%.*4자리.*균등 배분/);
  assert.doesNotMatch(element('#autoAllocationHelp').textContent, /1회 최대 15%/);
  assert.match(element('#autoAllocationHelp').textContent, /30%.*남은 보유 자리/);
  assert.match(element('#strategyCapitalHelp').textContent, /30%.*균등 배분/);
  assert.match(element('#recommendationList').innerHTML, /자동 균등 배분 최대 7주/);
  vm.runInContext('previewInvestmentRatio(70); previewManualRatio(50);', context);
  assert.match(element('#capitalRatioPreview').textContent, /750,000/);
  assert.match(element('#manualRatioPreview').textContent, /2,500,000/);
  vm.runInContext(`
    state.status.automation.buy_window_limit_enabled = false;
    state.status.automation.trading_hours = '자동매수 평일 09:00~15:30, 16:00~20:00 · 별도 자동매수 시간 제한 해제';
    renderStatus(state.status, state.risk);
  `, context);
  assert.match(element('#tradingHoursHelp').textContent, /09:00~15:30.*별도 자동매수 시간 제한 해제.*5분/);
  assert.doesNotMatch(element('#tradingHoursHelp').textContent, /09:00~10:00|12:00~14:00/);
  console.log('LIVE dashboard routes, averaging policy, per-buy limits and candle loading: OK');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
