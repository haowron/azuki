// 実ページの純粋な表示ロジックと API 契約(api.js)を Node.js 標準ライブラリーで検証する。
// 画面の起動時序(トークン注入と hash による画面切替)は DOM/fetch スタブでの起動によって検証する。
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const base = resolve(__dirname, '../../src/trackeverything/api/static/js');
const load = file => {const context = {module:{exports:{}}, Intl, Date, Set, Map}; runInNewContext(readFileSync(resolve(base, file), 'utf8'), context); return context.module.exports;};

const jsonResponse = (status, body) => ({ok: status < 400, status, json: async () => body});
const makeElement = id => {
 const listeners = {};
 const element = {
  id, tagName: 'div', hidden: false, disabled: false, checked: false, value: '', textContent: '', innerHTML: '', title: '',
  dataset: {}, style: {setProperty() {}}, classList: {toggle() {}, add() {}, remove() {}, contains() {return false;}},
  setAttribute() {}, getAttribute() {return null;},
  addEventListener(type, handler) {(listeners[type] ??= []).push(handler);},
  dispatch(type, event) {for (const handler of listeners[type] ?? []) handler(event);},
  closest(selector) {return selector === 'label' || selector === 'figure' || (selector.startsWith('button') && element.tagName === 'button') || (selector === '[data-edit-field]' && element.dataset.editField) ? element : null;},
  querySelector() {return null;}, querySelectorAll() {return [];}, contains() {return false;}, focus() {},
 };
 return element;
};
const catalogBody = {
 agents: [
  {name: 'pr_event', description: 'Process one GitHub PR event.', arguments: {}, module: 'trackeverything.agent.pr_event', cls: 'PrEventAgent', entrances: ['pr_event'], parents: [], children: ['project_memory', 'triage'], tools: ['save', 'notify'], pre: [], post: []},
  {name: 'project_memory', description: '入口の材料 Agent。', arguments: {}, module: 'trackeverything.agent.project_memory', cls: 'ProjectMemoryAgent', entrances: ['project_memory'], parents: ['pr_event'], children: [], tools: ['read'], pre: [], post: []},
  {name: 'triage', description: 'コメント裁定。', arguments: {}, module: 'trackeverything.agent.triage', cls: 'TriageAgent', entrances: [], parents: ['pr_event'], children: [], tools: [], pre: [], post: []},
  {name: 'orphan', description: '未接続の宣言のみ。', arguments: {}, module: 'test.agents', cls: 'OrphanAgent', entrances: [], parents: [], children: ['triage'], tools: ['notify'], pre: [], post: []},
 ],
 tools: [
  {name: 'save', description: 'Save a value keyed by a Source identity.', arguments: {type: 'object', properties: {identity: {type: 'string'}, value: {type: 'string'}}, required: ['identity', 'value']}, module: 'trackeverything.tools.memory.store', cls: 'SaveTool', used_by: ['pr_event']},
  {name: 'read', description: 'Read a value.', arguments: {type: 'object', properties: {identity: {type: 'string'}}, required: ['identity']}, module: 'trackeverything.tools.memory.store', cls: 'ReadTool', used_by: ['project_memory']},
  {name: 'lonely', description: '未接続 Tool。', arguments: {}, module: 'trackeverything.tools.lonely', cls: 'LonelyTool', used_by: []},
 ],
 harnesses: [],
 sources: [
  {key: 'github_pull_request', description: 'GitHub source — a single PR.', module: 'trackeverything.sources.github_pr', cls: 'GithubPullRequest'},
  {key: 'notion_page', description: 'Notion page snapshot.', module: 'trackeverything.sources.notion_page', cls: 'NotionPage'},
 ],
 webhook_entrances: ['pr_event'],
};
// /api/usage の応答 fixture。catalogBody の agents / tools と名前で突き合わせる。
const usageBody = {
 agents: [
  {name: 'pr_event', total: 3, by_status: {ok: 2, error: 1}, last_used_at: '2026-10-01T00:00:00+00:00'},
  {name: 'retired_agent', total: 1, by_status: {ok: 1}, last_used_at: '2026-09-01T00:00:00+00:00'},
 ],
 tools: [
  {name: 'save', total: 2, by_status: {ok: 2}, last_used_at: '2026-10-02T00:00:00+00:00'},
 ],
 agents_since: '2026-09-01T00:00:00+00:00',
 tools_since: '2026-10-02T00:00:00+00:00',
};

const bootCatalog = (options = {}) => {
 const serverToken = options.serverToken ?? 'tok-123';
 const calls = [];
 const elements = new Map();
 const element = id => {if (!elements.has(id)) elements.set(id, makeElement(id)); return elements.get(id);};
 element('tb-catalog').hidden = true;  // HTML の初期状態(hidden 属性)を模倣する
 const windowListeners = {};
 const context = {
  module: {exports: {}}, Intl, Date, Set, Map, AbortController,
  IntersectionObserver: class {observe() {}},
  fetch: async (path, init = {}) => {
   const headers = {...(init.headers ?? {})};
   calls.push({path: String(path), method: init.method ?? 'GET', headers, body: init.body ? JSON.parse(init.body) : null});
   if (init.method === 'PATCH' && options.patch) return options.patch(path, JSON.parse(init.body));
   if (path === 'api/catalog') return options.catalog ? options.catalog(headers) : jsonResponse(200, catalogBody);
   if (path === 'api/usage') return options.usage ? options.usage(headers) : jsonResponse(200, usageBody);
   return jsonResponse(404, {detail: 'not found'});
  },
  document: {
   getElementById: element, addEventListener() {}, querySelectorAll() {return [];}, querySelector() {return null;},
   createElement: () => ({src: '', onload: null, onerror: null}), head: {appendChild() {}}, activeElement: null,
  },
  localStorage: {getItem: () => serverToken, setItem() {}, removeItem() {}},
  setInterval: () => 0, clearInterval() {},
  window: {matchMedia: () => ({matches: false, addEventListener() {}}), addEventListener(type, handler) {(windowListeners[type] ??= []).push(handler);}},
  location: {hash: options.hash ?? ''},
 };
 runInNewContext(readFileSync(resolve(base, 'api.js'), 'utf8'), context);
 context.DashboardApi = context.module.exports;
 context.module = {exports: {}};
 // 本ページと同じ順で app.js も走らせ、api.js への token 注入を再現する。
 runInNewContext(readFileSync(resolve(base, 'app.js'), 'utf8'), context);
 context.module = {exports: {}};
 // 実行統計画面も本ページと同じ順で起動する(index.html の script 順)。
 runInNewContext(readFileSync(resolve(base, 'usage-model.js'), 'utf8'), context);
 context.UsageModel = context.module.exports;
 context.module = {exports: {}};
 runInNewContext(readFileSync(resolve(base, 'usage.js'), 'utf8'), context);
 context.module = {exports: {}};
 runInNewContext(readFileSync(resolve(base, 'catalog-model.js'), 'utf8'), context);
 context.CatalogModel = context.module.exports;
 context.module = {exports: {}};
 runInNewContext(readFileSync(resolve(base, 'catalog-poster.js'), 'utf8'), context);
 context.CatalogPoster = context.module.exports;
 context.module = {exports: {}};
 runInNewContext(readFileSync(resolve(base, 'catalog.js'), 'utf8'), context);
 const tick = async () => {for (let i = 0; i < 8; i++) await new Promise(resolve => setTimeout(resolve, 0));};
 const hashTo = hash => {context.location.hash = hash; for (const handler of windowListeners.hashchange ?? []) handler();};
 return {calls, element, tick, hashTo};
};

test('U06: カタログから統計へ移り、関係と未実行の成分を参照する', async () => {
 const app=bootCatalog();
 await app.tick();app.hashTo('#/catalog');await app.tick();
 assert.equal(app.element('tb-catalog').hidden,false);
 const catalog=app.element('tb-catalog-rows').innerHTML;
 assert.match(catalog,/pr_event/);assert.match(catalog,/project_memory/);assert.match(catalog,/triage/);
 app.hashTo('#/usage');await app.tick();
 assert.equal(app.element('tb-usage').hidden,false);
 const agents=app.element('tb-usage-agent-rows').innerHTML;
 assert.match(agents,/pr_event/);assert.match(agents,/triage/);assert.match(agents,/ok 2 · error 1/);
 assert.match(agents,/未実行|0 回|>0</);
 assert.match(app.element('tb-usage-tool-rows').innerHTML,/save/);
 assert.ok(app.calls.some(call=>call.path==='api/catalog') && app.calls.some(call=>call.path==='api/usage'));
});

// ---- 実行フロー画面(#/runs) ----
const R = load('runs-model.js');
const at = clock => '2026-10-09T02:' + clock + '+00:00';
const runStep = (id, parent, kind, name, clock, duration_ms, status = 'ok') => ({id, parent, kind, name, started_at: at(clock), duration_ms, status, cost_usd: null, tokens: null, errors: [], preview: null});
const runSummary = (run_id, agent, status, clock, duration_ms, extra = {}) => ({run_id, session_id: null, agent, status, complete: true, started_at: at(clock), duration_ms, step_count: 1, cost_usd: null, error_count: 0, ...extra});
const runDetail = {
 run: runSummary('run-1', 'ci_failure', 'ok', '08:25.940000', 1259380, {session_id: 'sess-1', step_count: 8, error_count: 1}),
 truncated: false,
 steps: [
  runStep('a1', null, 'agent', 'ci_failure', '08:25.940000', 1259380),
  runStep('t1', 'a1', 'tool', 'fetch_source', '08:25.941000', 1450),
  runStep('a2', 'a1', 'agent', 'investigator', '08:29.580000', 1169134),
  runStep('l1', 'a2', 'llm', 'zai/glm-5.3', '08:29.581000', 12657),
  runStep('t2', 'a2', 'tool', 'bad"<b>', '08:42.300000', 49, 'error'),
  runStep('l2', 'a2', 'llm', 'zai/glm-5.3', '08:43.000000', 9000),
  runStep('t3', 'a1', 'tool', 'notify', '29:24.447000', 863),
  runStep('o1', 'ghost', 'tool', 'read', '29:30.000000', 4),
 ],
};

const bootRuns = (options = {}) => {
 const calls = [], renders = [], elements = new Map(), windowListeners = {};
 const element = id => {if (!elements.has(id)) elements.set(id, makeElement(id)); return elements.get(id);};
 element('tb-runs').hidden = true;
 const routes = options.routes ?? {};
 const context = {
  module: {exports: {}}, Intl, Date, Set, Map, AbortController, Math, JSON, Number, String, Array, Object, Promise,
  IntersectionObserver: class {observe() {}},
  fetch: async (path, init = {}) => {
   calls.push({path: String(path), method: init.method ?? 'GET', headers: {...(init.headers ?? {})}});
   const route = routes[String(path)];
   return route ? jsonResponse(route[0], route[1]) : jsonResponse(404, {detail: 'not found'});
  },
  document: {getElementById: element, addEventListener() {}, querySelectorAll() {return [];}, querySelector() {return null;}, hidden: false, activeElement: null},
  location: {hash: options.hash ?? '#/runs'},
  setTimeout, clearTimeout,
 };
 context.window = {
  matchMedia: () => ({matches: false, addEventListener() {}}),
  addEventListener(type, handler) {(windowListeners[type] ??= []).push(handler);},
  requestAnimationFrame: () => 0, cancelAnimationFrame() {},
  mermaid: {initialize() {}, render: async (_id, source) => {renders.push(source); return {svg: '<svg></svg>'};}},
 };
 runInNewContext(readFileSync(resolve(base, 'api.js'), 'utf8'), context);
 context.DashboardApi = context.module.exports;
 context.DashboardApi.configure({getToken: () => 'tok-123'});
 context.module = {exports: {}};
 runInNewContext(readFileSync(resolve(base, 'runs-model.js'), 'utf8'), context);
 context.RunsModel = context.module.exports;
 context.module = {exports: {}};
 runInNewContext(readFileSync(resolve(base, 'runs.js'), 'utf8'), context);
 const tick = async () => {for (let i = 0; i < 12; i++) await new Promise(resolve => setTimeout(resolve, 0));};
 return {calls, renders, element, tick, location: context.location};
};

test('U07: run の木構造を開始順の Mermaid フローとタイムラインにする', () => {
 const flow = R.flow(runDetail, 'light');
 const key = node => node.key;
 // vm 内で作られた配列は realm が異なるため、比較前にテスト側の配列へ写す。
 assert.deepEqual(Array.from(flow.edges, edge => [edge.from, edge.to]), [
  ['start', 'a1'], ['a1', 't1'], ['t1', 'a2'], ['a2', 'l1'], ['l1', 't2'], ['t2', 'l2'], ['l2', 't3'],
  ['t3', 'missing:ghost'], ['missing:ghost', 'o1'], ['o1', 'end'],
 ]);
 assert.deepEqual(Array.from(flow.groups, group => [group.key, group.parent]), [['a1', null], ['a2', 'a1'], ['missing:ghost', null]]);
 const nodes = new Map(flow.nodes.map(node => [key(node), node]));
 assert.deepEqual(['a1', 't1', 'a2', 'l1', 't3', 'missing:ghost', 'o1'].map(id => nodes.get(id).group), ['a1', 'a1', 'a2', 'a2', 'a1', 'missing:ghost', 'missing:ghost']);
 assert.equal(nodes.get('t2').cls, 'error');
 assert.equal(nodes.get('l1').cls, 'ok');
 for (const text of ['20分59秒', '19分29秒', '12.7秒', '49ms', '未記録の実行', 'bad&quot;&lt;b&gt;']) assert.ok(flow.source.includes(text), text);
 assert.ok(!flow.source.includes('<b>') && !flow.source.includes('bad"'));
 assert.match(flow.source, /^flowchart TD/);
 const gantt = R.gantt(runDetail);
 assert.match(gantt, /^gantt/);
 assert.match(gantt, /dateFormat x/);
 const tasks = gantt.split('\n').filter(line => /:(crit|done|active), s\d+, \d+, \d+$/.test(line));
 assert.equal(tasks.length, 8);
 assert.equal(tasks.filter(line => line.includes(':crit,')).length, 1);
});

test('U08: 再生は開始順・入れ子・長さを保ち、時刻ごとの状態を示す', () => {
 const plan = R.runPlan(runDetail);
 const span = key => plan.spans.get(key);
 const order = [...plan.spans.keys()].filter(key => !['start', 'end'].includes(key)).sort((a, b) => span(a).start - span(b).start);
 assert.deepEqual(order, ['a1', 't1', 'a2', 'l1', 't2', 'l2', 't3', 'o1']);
 assert.ok(span('a2').start >= span('a1').start && span('a2').end <= span('a1').end);
 assert.ok(span('l1').start >= span('a2').start && span('l2').end <= span('a2').end);
 assert.ok(span('a2').end - span('a2').start > span('t2').end - span('t2').start);
 assert.ok(plan.total >= 4000 && plan.total <= 45000, String(plan.total));
 assert.equal(plan.elapsedAt(0), 0);
 assert.equal(plan.elapsedAt(plan.total), plan.realTotal);
 const middle = R.frame(plan, (span('l1').start + span('l1').end) / 2);
 assert.deepEqual(['start', 'a1', 't1', 'a2', 'l1', 't2', 'l2', 't3', 'o1', 'end'].map(id => middle.get(id)),
  ['done', 'running', 'done', 'running', 'running', 'pending', 'pending', 'pending', 'pending', 'pending']);
 const last = R.frame(plan, plan.total);
 assert.ok([...last.values()].every(state => state === 'done'));
});

test('U09: session ID を入力すると run の並びと最新 run の流れを表示する', async () => {
 const session = {session_id: 'sess-1', parent_session_id: null, created_at: at('00:00.000000'), total_runs: 3, runs: [
  runSummary('run-aaa1', 'discord', 'ok', '00:01.000000', 1200, {session_id: 'sess-1'}),
  runSummary('run-bbb2', 'discord', 'error', '05:00.000000', 800, {session_id: 'sess-1'}),
  runSummary('run-ccc3', 'issue', 'ok', '09:00.000000', 4000, {session_id: 'sess-1'}),
 ]};
 const app = bootRuns({routes: {
  'api/runs?limit=20': [200, {items: []}],
  'api/sessions/sess-1': [200, session],
  'api/runs/run-ccc3': [200, {...runDetail, run: {...runDetail.run, run_id: 'run-ccc3', session_id: 'sess-1'}}],
 }});
 await app.tick();
 app.element('tb-runs-input').value = '  sess-1 ';
 app.element('tb-runs-form').dispatch('submit', {preventDefault() {}});
 await app.tick();
 assert.equal(app.location.hash, '#/runs/sess-1');
 const paths = app.calls.map(call => call.path);
 const index = path => paths.indexOf(path);
 assert.ok(index('api/runs/sess-1') >= 0 && index('api/runs/sess-1') < index('api/sessions/sess-1') && index('api/sessions/sess-1') < index('api/runs/run-ccc3'), paths.join(','));
 assert.ok(app.calls.every(call => call.headers['X-Webhook-Token'] === 'tok-123'));
 assert.equal(app.element('tb-runs-session').hidden, false);
 assert.equal(app.element('tb-runs-run').hidden, false);
 const sessionSource = app.renders.find(source => source.includes('run-aaa1'));
 assert.ok(sessionSource, app.renders.join('\n---\n'));
 assert.ok(sessionSource.indexOf('run-aaa1') < sessionSource.indexOf('run-bbb2') && sessionSource.indexOf('run-bbb2') < sessionSource.indexOf('run-ccc3'));
});

test('U10: 最近の実行から選んだ run の流れと結果を表示する', async () => {
 const app = bootRuns({routes: {
  'api/runs?limit=20': [200, {items: [
   runSummary('run:1/a', 'pr_event', 'ok', '08:25.940000', 1259380, {session_id: 'sess-1'}),
   runSummary('run-2', 'discord', 'error', '07:00.000000', 2000),
  ]}],
  'api/runs/run%3A1%2Fa': [200, {...runDetail, run: {...runDetail.run, run_id: 'run:1/a'}}],
 }});
 await app.tick();
 const recent = app.element('tb-runs-recent').innerHTML;
 for (const text of ['pr_event', 'discord', '成功', '失敗', 'sess-1', '20分59秒']) assert.ok(recent.includes(text), text);
 const pick = makeElement('pick'); pick.tagName = 'button'; pick.dataset.runPick = 'run:1/a';
 pick.closest = selector => (selector === '[data-run-pick]' ? pick : null);
 app.element('tb-runs-recent').dispatch('click', {target: pick});
 await app.tick();
 assert.equal(app.location.hash, '#/runs/run%3A1%2Fa');
 assert.ok(app.calls.some(call => call.path === 'api/runs/run%3A1%2Fa'));
 assert.equal(app.element('tb-runs-run').hidden, false);
 const summary = app.element('tb-runs-summary').innerHTML;
 assert.ok(summary.includes('成功') && summary.includes('20分59秒'), summary);
 // 既定は Agent・LLM・Tool・Skill の 4 列の構成図。
 assert.ok(app.renders.some(source => ['"Agent"', '"LLM"', '"Tool"', '"Skill"'].every(title => source.includes(title))), app.renders.join('\n---\n'));
 const block = makeElement('block'); block.dataset.runNode = 'tool:notify';
 block.closest = selector => (selector === '[data-run-node]' ? block : null);
 app.element('tb-runs-body').dispatch('click', {type: 'click', target: block});
 const detail = app.element('tb-runs-detail').innerHTML;
 for (const text of ['notify', '#7', '863ms', '成功']) assert.ok(detail.includes(text), text + ' / ' + detail);
});

// 構成表示の fixture: 子 Agent、同じ model の LLM、3 回(1 回失敗)の Tool、2 つの Agent が呼ぶ Tool、Skill、親未記録の step。
const structureDetail = {
 run: runSummary('run-s', 'ci_failure', 'error', '00:00.000000', 60000, {step_count: 12, error_count: 1}),
 truncated: false,
 steps: [
  runStep('a1', null, 'agent', 'ci_failure', '00:00.000000', 60000),
  runStep('l1', 'a1', 'llm', 'zai/glm-5.3', '00:01.000000', 2000),
  runStep('a2', 'a1', 'agent', 'investigator', '00:04.000000', 30000),
  runStep('l2', 'a2', 'llm', 'zai/glm-5.3', '00:05.000000', 3000),
  runStep('t1', 'a2', 'tool', 'run_command', '00:09.000000', 1000),
  runStep('t2', 'a2', 'tool', 'run_command', '00:11.000000', 1000, 'error'),
  {...runStep('k1', 'a2', 'tool', 'read_skill', '00:13.000000', 10), preview: '{"name": "doc-analysis", "path": "SKILL.md"}'},
  runStep('k2', 'a2', 'tool', 'skill__doc-analysis__extract_text', '00:14.000000', 500),
  runStep('t3', 'a2', 'tool', 'run_command', '00:16.000000', 1000),
  runStep('n1', 'a2', 'tool', 'notify', '00:20.000000', 300),
  runStep('n2', 'a1', 'tool', 'notify', '00:40.000000', 300),
  runStep('o1', 'ghost', 'tool', 'bad"<b>', '00:50.000000', 5),
 ],
};

test('U11: 構成表示は 4 列に同じ名前を 1 block でまとめ、呼出しごとに番号付きの矢印を引く', () => {
 const view = R.structure(structureDetail, 'light');
 assert.deepEqual(Array.from(view.lanes, lane => lane.title), ['Agent', 'LLM', 'Tool', 'Skill']);
 const lane = key => Array.from(view.nodes.filter(node => node.lane === key), node => node.key);
 assert.deepEqual(lane('agent'), ['start', 'agent:ci_failure', 'agent:investigator', 'missing:ghost']);
 assert.deepEqual(lane('llm'), ['llm:zai/glm-5.3']);
 assert.deepEqual(lane('tool'), ['tool:run_command', 'tool:notify', 'tool:bad"<b>']);
 assert.deepEqual(lane('skill'), ['skill:doc-analysis:read', 'skill:doc-analysis:fn:extract_text']);
 assert.deepEqual(Array.from(view.edges, edge => [edge.label, edge.from, edge.to]), [
  ['1', 'start', 'agent:ci_failure'],
  ['2', 'agent:ci_failure', 'llm:zai/glm-5.3'],
  ['3', 'agent:ci_failure', 'agent:investigator'],
  ['4', 'agent:investigator', 'llm:zai/glm-5.3'],
  ['5', 'agent:investigator', 'tool:run_command'],
  ['✗6', 'agent:investigator', 'tool:run_command'],
  ['7', 'agent:investigator', 'skill:doc-analysis:read'],
  ['8', 'agent:investigator', 'skill:doc-analysis:fn:extract_text'],
  ['9', 'agent:investigator', 'tool:run_command'],
  ['10', 'agent:investigator', 'tool:notify'],
  ['11', 'agent:ci_failure', 'tool:notify'],
  ['12', 'missing:ghost', 'tool:bad"<b>'],
 ]);
 const node = key => view.nodes.find(item => item.key === key);
 assert.deepEqual([node('tool:run_command').calls, node('tool:run_command').failures, node('tool:run_command').cls], [3, 1, 'error']);
 assert.deepEqual([node('llm:zai/glm-5.3').calls, node('tool:notify').calls, node('tool:notify').cls], [2, 2, 'ok']);
 assert.deepEqual(Array.from(view.groups, group => [group.key, group.title]), [['skill:doc-analysis', 'doc-analysis']]);
 assert.deepEqual(lane('skill').map(key => node(key).group), ['skill:doc-analysis', 'skill:doc-analysis']);
 assert.match(view.source, /^flowchart LR/);
 for (const text of ['"Agent"', '"LLM"', '"Tool"', '"Skill"', '3回', '✗1', '2回', '本文の読込', 'extract_text', '未記録の実行', '✗6', 'bad&quot;&lt;b&gt;']) assert.ok(view.source.includes(text), text);
 assert.ok(!view.source.includes('<b>') && !view.source.includes('bad"'));
});

test('U12: 構成表示の再生は矢印を呼出し順に出し、繰り返しの block を実行中と完了で往復させる', () => {
 const plan = R.structurePlan(structureDetail);
 const span = key => plan.spans.get(key);
 const at = t => R.frame(plan, t);
 const middle = key => (span(key).start + span(key).end) / 2;
 assert.equal(at(middle('l2')).get('tool:run_command'), 'pending');
 assert.equal(at(middle('l2')).get('skill:doc-analysis:read'), 'pending');
 assert.equal(at(middle('l2')).get('t2'), 'pending');
 assert.equal(at(middle('t1')).get('tool:run_command'), 'running');
 assert.equal(at((span('t1').end + span('t2').start) / 2).get('tool:run_command'), 'done');
 assert.equal(at(middle('t2')).get('tool:run_command'), 'running');
 assert.equal(at(middle('t2')).get('t2'), 'running');
 assert.equal(at((span('k2').end + span('t3').start) / 2).get('tool:run_command'), 'done');
 assert.equal(at(middle('t3')).get('tool:run_command'), 'running');
 assert.equal(at(middle('t3')).get('t2'), 'done');
 assert.equal(at(middle('l1')).get('start'), 'done');
 assert.ok([...at(plan.total).values()].every(state => state === 'done'));
});

test('U13: 期限で中断した run は 中断 と表示し、警告として数える', () => {
 assert.equal(R.statusLabel('cancelled'), '中断');
 assert.equal(R.statusClass('cancelled'), 'warn');
 const detail = {...runDetail, steps: [runStep('a1', null, 'agent', 'entry', '08:25.940000', 3600000, 'cancelled')]};
 const view = R.structure(detail, 'light');
 const node = view.nodes.find(item => item.key === 'agent:entry');
 assert.equal(node.warnings, 1);
 assert.equal(node.failures, 0);
 assert.equal(node.cls, 'warn');
});
