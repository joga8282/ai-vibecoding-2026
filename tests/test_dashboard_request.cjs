// Authentication request tests use mocked fetch only; no browser or account calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

let session = false;
let failBootstrap = false;
let alwaysUnauthorized = false;
let bootstraps = 0;
let attempts = 0;
const requests = [];
const context = vm.createContext({
  document: {}, window: { prompt() { throw new Error('Token dialog must never appear'); } },
  sessionStorage: { removeItem() {}, getItem() { throw new Error('Must not read stored API secret'); } },
  fetch: async (url, options) => {
    requests.push({url, options});
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['X-API-Token'], undefined);
    if (url === '/api/v1/auth/local-session') {
      bootstraps++;
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['X-Dashboard-Request'], '1');
      await new Promise(resolve => setTimeout(resolve, 5));
      if (!failBootstrap) session = true;
      return {status: failBootstrap ? 403 : 200, ok: !failBootstrap};
    }
    attempts++;
    const ok = session && !alwaysUnauthorized;
    return {status: ok ? 200 : 401, ok, json: async () => ({synced: true})};
  },
});
const source = fs.readFileSync(path.join(__dirname, '../app/static/app.js'), 'utf8');
vm.runInContext(source.slice(0, source.indexOf('\nfunction toast(')), context);

async function run() {
  const results = await vm.runInContext("Promise.all(Array.from({length: 5}, () => request('/api/v1/live/account')))", context);
  assert.equal(results.length, 5);
  assert.equal(bootstraps, 1, 'Simultaneous 401s share one automatic session request');
  assert.equal(attempts, 10);
  session = false; // Simulate a backend restart; no token dialog is needed.
  await vm.runInContext("request('/api/v1/live/qualified-buy/005930', {method: 'POST', body: JSON.stringify({confirm_real_order: true})})", context);
  assert.equal(bootstraps, 2);
  const last = requests.at(-1);
  assert.equal(last.options.method, 'POST');
  assert.equal(JSON.parse(last.options.body).confirm_real_order, true);
  session = false;
  failBootstrap = true;
  await assert.rejects(vm.runInContext("request('/api/v1/live/account')", context));
  failBootstrap = false;
  alwaysUnauthorized = true;
  const before = attempts;
  await assert.rejects(vm.runInContext("request('/api/v1/live/account')", context));
  assert.equal(attempts - before, 2, 'Retries are bounded to one');
  assert.doesNotMatch(source, /window\.prompt\(/);
  console.log('Automatic local sessions, concurrent refresh, restart recovery and no token prompts: OK');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
