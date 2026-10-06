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
    renderRecommendations({budget: '140000', candidates: [
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
  assert.equal(element('#capitalRatioInput').max, '15');
  assert.equal(element('#startButton').textContent, 'LIVE 자동매매 실행 중');
  assert.match(element('#recommendationList').innerHTML, /LIVE 실계좌 조건 확인·매수/);
  assert.match(element('#recommendationList').innerHTML, /disabled.*LIVE 허용목록에 없음/);
  assert.doesNotMatch(element('#recommendationWatchlist').innerHTML, /data-test-buy|PAPER/);
  assert.equal(context.requests.length, 2);
  assert.equal(context.requests[0].url, '/api/v1/live/qualified-buy/005930');
  assert.equal(context.requests[1].url, '/api/v1/live/positions/005930/close');
  for (const r of context.requests) assert.equal(JSON.parse(r.opts.body).confirm_real_order, true);
  vm.runInContext("state.status.automation.remaining = '0'; renderRecommendations(state.recommendations, false)", context);
  assert.match(element('#recommendationList').innerHTML, /disabled.*추가 매수 한도 없음/);
  vm.runInContext(`
    state.risk.recommended_trade_ratio = '1';
    state.risk.live_limits = {symbol_policy: 'recommended', allowed_symbols: [],
      max_total_exposure_ratio: '1', max_order_amount_krw: '0'};
    state.status.automation.remaining = '1000000';
    renderStatus(state.status, state.risk);
    renderCapital({cash: {KRW: '1000000'}, total_equity: {KRW: '1200000'}},
      state.risk, state.status, {by_currency: {}});
  `, context);
  assert.equal(element('#capitalRatioInput').max, '100');
  assert.equal(element('#capitalRatioInput').value, '100');
  assert.match(element('#capitalRecommendationHelp').textContent, /100% 이내/);
  assert.match(element('#capitalRecommendationHelp').textContent, /가용 현금 한도/);
  assert.doesNotMatch(element('#recommendationList').innerHTML, /LIVE 허용목록에 없음/);
  assert.match(element('#investmentRatioHelp').textContent, /최신 추천/);
  console.log('LIVE dashboard rendering, route selection, confirmations and blocked buys: OK');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
