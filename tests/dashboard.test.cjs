const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

function dashboard() {
  const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      value: '', innerHTML: '', textContent: '', addEventListener() {},
      classList: { add() {}, remove() {} },
    });
    return elements.get(id);
  };
  const ctx = vm.createContext({
    console, document: { getElementById: get }, setInterval() {},
    localStorage: { getItem() { return ''; }, setItem() {} },
    fetch: async url => ({ status: 200, ok: true, json: async () => url.includes('/clickhouse/') ? {
      rows: 1, table: 'salesforce_accounts', engine: 'ReplacingMergeTree',
      sorting_key: 'organisation_id, id', partition_key: 'organisation_id',
      columns: [{ name: 'id', type: 'String' }], sample: [{ id: 'synthetic' }],
    } : [] }),
  });
  vm.runInContext(script, ctx);
  return { ctx, get };
}

test('inspector renders the API sorting key string and sample', async () => {
  const { ctx, get } = dashboard();
  await vm.runInContext("inspect('Accounts')", ctx);
  assert.match(get('modalContent').innerHTML, /ORDER BY \(organisation_id, id\)/);
  assert.match(get('modalContent').innerHTML, /synthetic/);
  assert.doesNotMatch(get('modalContent').innerHTML, /not a function/);
});

test('all backend resumable states offer a Resume action', async () => {
  const { ctx, get } = dashboard();
  await vm.runInContext('refresh()', ctx);
  for (const status of ['paused', 'failed', 'interrupted']) {
    ctx.job = { job_id: 'synthetic', status, objects: ['Accounts'] };
    vm.runInContext('renderJobs([job])', ctx);
    assert.match(get('jobs').innerHTML, /Resume/);
  }
});

test('completed and paused jobs retain progress for requested objects', async () => {
  const { ctx, get } = dashboard();
  await vm.runInContext('refresh()', ctx);
  for (const status of ['completed', 'paused']) {
    ctx.job = { status, objects: ['Accounts', 'Contacts'], completed_objects: ['Accounts'] };
    await vm.runInContext('renderObjects([job])', ctx);
    assert.match(get('objects').innerHTML, /DONE/);
    assert.match(get('objects').innerHTML, /Contacts/);
    assert.doesNotMatch(get('objects').innerHTML, /Opportunities/);
  }
});

test('active jobs take priority over the latest completed job', async () => {
  const { ctx, get } = dashboard();
  await vm.runInContext('refresh()', ctx);
  ctx.jobs = [
    { status: 'completed', objects: ['Accounts'], completed_objects: ['Accounts'] },
    { status: 'running', objects: ['Contacts'], current_object: 'Contacts' },
  ];
  await vm.runInContext('renderObjects(jobs)', ctx);
  assert.match(get('objects').innerHTML, /Contacts/);
  assert.match(get('objects').innerHTML, /RUNNING/);
  assert.doesNotMatch(get('objects').innerHTML, /Accounts/);
});
