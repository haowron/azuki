"use strict";
// 実行フロー画面(#/runs)の純粋ロジック。DOM も fetch も持たない(Node の標準ライブラリーだけで検証する)。
// /api/runs/{id} の step 一覧から木を組み、Mermaid の構成図(4 列)・順序図・gantt と、再生用の時間変換を作る。
// runs.js は描画・操作だけを担う。仕様は docs/components/DASHBOARD_SPEC.md。
const RunsModel = (() => {
  // 色は画面の design token と同じ対(light / dark)。状態の色は文字と組み合わせて使う。
  const THEMES = {
    light: {
      ok: {fill: "#e8f5ed", stroke: "#5c8a6e", color: "#186141"},
      error: {fill: "#fcebec", stroke: "#b9838c", color: "#8f2934"},
      warn: {fill: "#fff2d4", stroke: "#b8913c", color: "#795210"},
      running: {fill: "#edf3ff", stroke: "#2256bd", color: "#224c9d"},
      neutral: {fill: "#ffffff", stroke: "#dce1e9", color: "#202733"},
      terminal: {fill: "#eef0f4", stroke: "#586373", color: "#202733"},
      missing: {fill: "#f5f6f8", stroke: "#abb5c5", color: "#586373"},
      group: {fill: "#f7f9fc", stroke: "#dce1e9"},
      lane: {fill: "#fbfcfe", stroke: "#dce1e9"},
    },
    dark: {
      ok: {fill: "#1d382c", stroke: "#67b287", color: "#a0e4bc"},
      error: {fill: "#3d2327", stroke: "#ffc3ca", color: "#ffc3ca"},
      warn: {fill: "#43351b", stroke: "#f5d697", color: "#f5d697"},
      running: {fill: "#20314d", stroke: "#a2bdff", color: "#c4d6ff"},
      neutral: {fill: "#1a1e25", stroke: "#363e4b", color: "#eef1f6"},
      terminal: {fill: "#242a34", stroke: "#abb5c5", color: "#eef1f6"},
      missing: {fill: "#101216", stroke: "#586373", color: "#abb5c5"},
      group: {fill: "#151920", stroke: "#363e4b"},
      lane: {fill: "#13171d", stroke: "#363e4b"},
    },
  };
  const STATUS = {ok: "成功", error: "失敗", max_steps: "上限到達", cancelled: "中断", interrupted: "停止で未完了", running: "実行中", incomplete: "未完了"};
  // 注意を促す終了状態(上限到達・スキップ・期限による中断)。
  const WARNINGS = new Set(["max_steps", "cancelled", "interrupted"]);
  const MARKS = {ok: "✓", error: "✗", warn: "!", running: "…", neutral: "·"};
  const KINDS = {
    agent: {mark: "◆", label: "Agent"}, llm: {mark: "✦", label: "LLM"}, tool: {mark: "⚙", label: "Tool"},
    harness: {mark: "⛨", label: "Harness"}, cli: {mark: "⌘", label: "CLI"}, source: {mark: "⇣", label: "Source"},
    skill: {mark: "❖", label: "Skill"},
  };
  // 構成表示の列。左からこの順に固定する。
  const LANES = [{key: "agent", title: "Agent"}, {key: "llm", title: "LLM"}, {key: "tool", title: "Tool"}, {key: "skill", title: "Skill"}];
  // Skill の自前関数は `skill__<Skill 名>__<関数名>` の Tool 名で記録される(add-agent-skills の設計)。
  const SKILL_FUNCTION = /^skill__(.+?)__(.+)$/;
  // 1× の総再生時間。step ごとの目安を掛け、下限・上限で挟む。
  const REPLAY_PER_STEP = 750, REPLAY_MIN = 4000, REPLAY_MAX = 45000;

  function escapeHtml(value) {
    return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  // mermaid の label は "…" 引用 + htmlLabels。実体参照で埋め、改行は <br/> だけ許す。
  const mermaidLabel = lines => '"' + lines.filter(line => line !== "" && line != null).map(line => escapeHtml(String(line).replace(/\s+/g, " "))).join("<br/>") + '"';
  const clipText = (text, limit) => (text.length > limit ? text.slice(0, limit - 1) + "…" : text);

  const statusLabel = status => STATUS[status] ?? String(status ?? "不明");
  const statusClass = status => (status === "ok" ? "ok" : status === "error" ? "error" : WARNINGS.has(status) ? "warn" : status === "running" ? "running" : "neutral");
  const statusMark = status => MARKS[statusClass(status)];
  const kindInfo = kind => KINDS[kind] ?? {mark: "▪", label: String(kind ?? "event")};

  // 1 秒未満は ms、60 秒未満は秒(小数 1 桁)、それ以上は分・秒、1 時間以上は時間・分。
  function duration(ms) {
    const value = Number(ms);
    if (ms === null || ms === undefined || !Number.isFinite(value)) return "—";
    const total = Math.max(0, Math.round(value));
    if (total < 1000) return total + "ms";
    if (total < 60000) return (total / 1000).toFixed(1) + "秒";
    if (total < 3600000) return Math.floor(total / 60000) + "分" + Math.floor((total % 60000) / 1000) + "秒";
    return Math.floor(total / 3600000) + "時間" + Math.floor((total % 3600000) / 60000) + "分";
  }
  // 再生位置の経過表示(m:ss / h:mm:ss)。
  function clock(ms) {
    const seconds = Math.max(0, Math.floor(Number(ms) / 1000) || 0);
    const pad = value => String(value).padStart(2, "0");
    const hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60);
    return (hours ? hours + ":" + pad(minutes) : String(minutes)) + ":" + pad(seconds % 60);
  }
  const instant = value => {
    const parsed = Date.parse(value ?? "");
    return Number.isNaN(parsed) ? Infinity : parsed;
  };
  const shortId = id => clipText(String(id ?? ""), 9);
  function shortTime(value) {
    const at = instant(value);
    if (!Number.isFinite(at)) return String(value ?? "");
    return new Intl.DateTimeFormat("ja-JP", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit"}).format(new Date(at));
  }

  // ---- 木 ----------------------------------------------------------------------
  // 根の列: 親のない step と、親が run 内に記録されていない step を親 ID ごとにまとめた枠。
  function tree(steps) {
    const list = (Array.isArray(steps) ? steps : []).map((step, index) => ({...step, index, at: instant(step.started_at)}));
    list.sort((a, b) => (a.at - b.at) || (a.index - b.index));
    const known = new Set(list.map(step => step.id));
    const children = new Map(), missing = new Map(), root = [];
    for (const step of list) {
      if (step.parent === null || step.parent === undefined || step.parent === step.id) root.push(step);
      else if (known.has(step.parent)) {
        if (!children.has(step.parent)) children.set(step.parent, []);
        children.get(step.parent).push(step);
      } else {
        let group = missing.get(step.parent);
        if (!group) {
          group = {kind: "missing", key: "missing:" + step.parent, parent: step.parent, children: []};
          missing.set(step.parent, group);
          root.push(group);
        }
        group.children.push(step);
      }
    }
    return {list, root, childrenOf: item => (item.kind === "missing" ? item.children : children.get(item.id) ?? [])};
  }
  const keyOf = item => (item.kind === "missing" ? item.key : item.id);
  const isBox = item => item.kind === "missing" || item.kind === "agent";

  const classDefs = theme => Object.entries(THEMES[theme] ?? THEMES.light).filter(([name]) => name !== "group" && name !== "lane").map(([name, palette]) => `  classDef ${name} fill:${palette.fill},stroke:${palette.stroke},color:${palette.color};`);

  function stepLabel(step) {
    const kind = kindInfo(step.kind);
    const lines = [kind.mark + " " + step.name, statusMark(step.status) + " " + (step.kind === "agent" ? statusLabel(step.status) + " · " : "") + duration(step.duration_ms)];
    const preview = typeof step.preview === "string" ? step.preview : "";
    if (step.kind === "llm") lines.push(preview.startsWith("呼び出し: ") ? "→ " + clipText(preview.slice("呼び出し: ".length), 36) : preview ? "最終応答" : "");
    if (step.status === "error" && Array.isArray(step.errors) && step.errors.length) lines.push(clipText(step.errors[0], 40));
    return lines;
  }
  function stepShape(step, label) {
    if (step.kind === "agent") return `[[${label}]]`;
    if (step.kind === "llm") return `(${label})`;
    if (step.kind === "tool") return `[${label}]`;
    return `[/${label}/]`;
  }

  // run の flowchart。nodes / edges / groups は描画後の SVG 要素との対応と再生に使う。
  function flow(detail, theme = "light") {
    const run = detail && detail.run ? detail.run : {};
    const {root, childrenOf} = tree(detail && detail.steps);
    const palette = THEMES[theme] ?? THEMES.light;
    const nodes = [], edges = [], groups = [], body = [], styles = [];
    const ids = new Map();
    const idFor = key => {
      if (!ids.has(key)) ids.set(key, "s" + ids.size);
      return ids.get(key);
    };
    const node = (key, text, cls, group, kind, step) => {
      nodes.push({key, id: idFor(key), cls, group, kind, step: step ?? null});
      return `${idFor(key)}${text}:::${cls};`;
    };
    const edge = (from, to) => edges.push({id: "e" + edges.length, from, to});
    const indent = depth => "  ".repeat(depth + 1);

    const runEnd = (detail && detail.steps || []).reduce((end, step) => Math.max(end, instant(step.started_at) + (Number(step.duration_ms) || 0)), -Infinity);
    body.push(indent(0) + node("start", `([${mermaidLabel(["▶ 開始", run.agent || "", shortTime(run.started_at)])}])`, "terminal", null, "start"));

    function emit(item, group, depth) {
      if (!isBox(item)) {
        body.push(indent(depth) + node(item.id, stepShape(item, mermaidLabel(stepLabel(item))), statusClass(item.status), group, item.kind, item));
        return item.id;
      }
      const key = keyOf(item), boxId = "g" + idFor(key);
      groups.push({key, parent: group});
      styles.push(`  style ${boxId} fill:${palette.group.fill},stroke:${palette.group.stroke},stroke-dasharray:${item.kind === "missing" ? "4 3" : "0"};`);
      body.push(`${indent(depth)}subgraph ${boxId}[" "]`);
      body.push(`${indent(depth + 1)}direction TB`);
      if (item.kind === "missing") body.push(indent(depth + 1) + node(key, `[/${mermaidLabel(["未記録の実行", "親 " + shortId(item.parent)])}/]`, "missing", key, "missing"));
      else body.push(indent(depth + 1) + node(key, stepShape(item, mermaidLabel(stepLabel(item))), statusClass(item.status), key, "agent", item));
      let tail = key;
      for (const child of childrenOf(item)) {
        edge(tail, keyOf(child));
        tail = emit(child, key, depth + 1);
      }
      body.push(`${indent(depth)}end`);
      return tail;
    }
    let previous = "start";
    for (const item of root) {
      edge(previous, keyOf(item));
      previous = emit(item, null, 0);
    }
    const total = Number.isFinite(run.duration_ms) ? run.duration_ms : Number.isFinite(runEnd) && Number.isFinite(instant(run.started_at)) ? runEnd - instant(run.started_at) : null;
    body.push(indent(0) + node("end", `([${mermaidLabel(["■ 結果: " + statusLabel(run.status), "全体 " + duration(total)])}])`, statusClass(run.status), null, "end"));
    edge(previous, "end");
    // 枠の所属は最初に現れた位置で決まるため、連線はすべての枠の後に置く。
    const links = edges.map(({id, from, to}) => `  ${idFor(from)} ${id}@--> ${idFor(to)};`);
    const source = ["flowchart TD", ...body, ...links, ...styles, ...classDefs(theme)].join("\n");
    return {source, nodes, edges, groups};
  }

  // 実時刻のタイムライン。Agent ごとの section に step の棒を並べ、失敗を crit にする。
  function gantt(detail) {
    const {list} = tree(detail && detail.steps);
    const known = new Set(list.map(step => step.id));
    const sections = new Map();
    const names = new Map(list.filter(step => step.kind === "agent").map(step => [step.id, step.name]));
    list.forEach((step, index) => {
      const key = step.kind === "agent" ? step.id : step.parent && known.has(step.parent) ? step.parent : step.parent ? "missing:" + step.parent : "root";
      if (!sections.has(key)) sections.set(key, []);
      const start = Number.isFinite(step.at) ? Math.round(step.at) : 0;
      const end = start + Math.max(1, Math.round(Number(step.duration_ms) || 0));
      const tag = step.status === "error" ? "crit" : step.status === "ok" ? "done" : "active";
      const name = (kindInfo(step.kind).mark + " " + step.name + " " + duration(step.duration_ms)).replace(/[:;#\n]/g, " ");
      sections.get(key).push(`    ${name} :${tag}, s${index}, ${start}, ${end}`);
    });
    const title = key => (key === "root" ? "実行" : key.startsWith("missing:") ? "未記録の実行" : names.get(key) ?? key);
    const lines = ["gantt", "  dateFormat x", "  axisFormat %H:%M:%S", "  todayMarker off"];
    for (const [key, tasks] of sections) lines.push("  section " + title(key).replace(/[:;#\n]/g, " "), ...tasks);
    return lines.join("\n");
  }

  // session の run を開始順に並べた左→右の flowchart。
  function sessionFlow(session, theme = "light") {
    const runs = Array.isArray(session && session.runs) ? session.runs : [];
    const nodes = [], edges = [], lines = ["flowchart LR"];
    runs.forEach((run, index) => {
      const id = "r" + index, cls = statusClass(run.status);
      nodes.push({key: run.run_id, id, cls, run});
      const label = mermaidLabel(["◆ " + (run.agent || "不明"), statusMark(run.status) + " " + statusLabel(run.status) + " · " + duration(run.duration_ms), shortTime(run.started_at) + " · " + shortId(run.run_id)]);
      lines.push(`  ${id}[${label}]:::${cls};`);
      if (index) edges.push({id: "e" + (index - 1), from: runs[index - 1].run_id, to: run.run_id});
    });
    for (const [index, item] of edges.entries()) lines.push(`  r${index} ${item.id}@--> r${index + 1};`);
    return {source: [...lines, ...classDefs(theme)].join("\n"), nodes, edges};
  }

  // ---- 構成(Agent・LLM・Tool・Skill の 4 列) ------------------------------------------
  function skillNameOf(preview) {
    const match = /"name"\s*:\s*"((?:[^"\\]|\\.)*)"/.exec(typeof preview === "string" ? preview : "");
    if (!match) return "不明";
    try { return JSON.parse('"' + match[1] + '"'); } catch { return match[1]; }
  }
  // step が属する block。同じ Agent 名・LLM model・Tool 名・Skill 関数は同じ block になる。
  function blockOf(step) {
    if (step.kind === "agent") return {key: "agent:" + step.name, lane: "agent", kind: "agent", name: step.name};
    if (step.kind === "llm") return {key: "llm:" + step.name, lane: "llm", kind: "llm", name: step.name};
    if (step.kind === "tool") {
      if (step.name === "read_skill") {
        const skill = skillNameOf(step.preview);
        return {key: "skill:" + skill + ":read", lane: "skill", kind: "skill", name: "本文の読込", group: {key: "skill:" + skill, title: skill}};
      }
      const match = SKILL_FUNCTION.exec(step.name);
      if (match) return {key: "skill:" + match[1] + ":fn:" + match[2], lane: "skill", kind: "skill", name: match[2], group: {key: "skill:" + match[1], title: match[1]}};
      return {key: "tool:" + step.name, lane: "tool", kind: "tool", name: step.name};
    }
    return {key: String(step.kind) + ":" + step.name, lane: "tool", kind: step.kind, name: step.name};
  }

  function structure(detail, theme = "light") {
    const run = detail && detail.run ? detail.run : {};
    const {list} = tree(detail && detail.steps);
    const byId = new Map(list.map(step => [step.id, step]));
    const palette = THEMES[theme] ?? THEMES.light;
    const nodes = [], index = new Map(), edges = [], groups = [];
    const ensure = info => {
      if (!index.has(info.key)) {
        const node = {key: info.key, id: "b" + nodes.length, lane: info.lane, kind: info.kind, name: info.name, group: info.group ? info.group.key : null, parent: info.parent ?? null, members: [], outgoing: [], calls: 0, failures: 0, warnings: 0, running: 0, total_ms: 0, cost: null};
        if (info.group && !groups.some(group => group.key === info.group.key)) groups.push({key: info.group.key, title: info.group.title});
        nodes.push(node); index.set(info.key, node);
      }
      return index.get(info.key);
    };
    ensure({key: "start", lane: "agent", kind: "start", name: "開始"});
    list.forEach((step, position) => {
      const node = ensure(blockOf(step));
      node.members.push(step.id); node.calls += 1; node.total_ms += Number(step.duration_ms) || 0;
      if (step.status === "error") node.failures += 1;
      else if (WARNINGS.has(step.status)) node.warnings += 1;
      else if (step.status === "running") node.running += 1;
      if (typeof step.cost_usd === "number" && Number.isFinite(step.cost_usd)) node.cost = (node.cost ?? 0) + step.cost_usd;
      const caller = step.parent === null || step.parent === undefined || step.parent === step.id ? index.get("start")
        : byId.has(step.parent) ? ensure(blockOf(byId.get(step.parent)))
        : ensure({key: "missing:" + step.parent, lane: "agent", kind: "missing", name: "未記録の実行", parent: step.parent});
      caller.outgoing.push(step.id);
      edges.push({id: "e" + position, from: caller.key, to: node.key, step: step.id, label: (step.status === "error" ? "✗" : "") + (position + 1)});
    });
    for (const node of nodes) node.cls = node.kind === "start" ? "terminal" : node.kind === "missing" ? "missing" : node.failures ? "error" : node.warnings ? "warn" : node.running ? "running" : "ok";

    const label = node => {
      if (node.kind === "start") return `(["${["▶ 開始", run.agent ? escapeHtml(run.agent) : "", escapeHtml(shortTime(run.started_at))].filter(Boolean).join("<br/>")}"])`;
      if (node.kind === "missing") return `[/${mermaidLabel(["未記録の実行", "親 " + shortId(node.parent)])}/]`;
      const text = mermaidLabel([kindInfo(node.kind).mark + " " + node.name, node.calls + "回 · 計 " + duration(node.total_ms) + (node.failures ? " · ✗" + node.failures : ""), node.cost !== null ? "$" + node.cost.toFixed(3) : ""]);
      return node.kind === "agent" ? `[[${text}]]` : node.kind === "llm" ? `(${text})` : `[${text}]`;
    };
    const lines = ["flowchart LR"], links = [], styles = [];
    const firsts = [];
    for (const lane of LANES) {
      const own = nodes.filter(node => node.lane === lane.key);
      lines.push(`  subgraph lane_${lane.key}["${lane.title}"]`, "    direction TB");
      styles.push(`  style lane_${lane.key} fill:${palette.lane.fill},stroke:${palette.lane.stroke};`);
      if (!own.length) {
        lines.push(`    empty_${lane.key}["なし"]:::missing;`);
        firsts.push("empty_" + lane.key);
      } else firsts.push(own[0].id);
      for (const node of own.filter(item => !item.group)) lines.push(`    ${node.id}${label(node)}:::${node.cls};`);
      for (const [position, group] of groups.entries()) {
        const members = own.filter(node => node.group === group.key);
        if (!members.length) continue;
        lines.push(`    subgraph sk${position}[${mermaidLabel([group.title])}]`, "      direction TB");
        for (const node of members) lines.push(`      ${node.id}${label(node)}:::${node.cls};`);
        lines.push("    end");
        styles.push(`  style sk${position} fill:${palette.group.fill},stroke:${palette.group.stroke};`);
      }
      lines.push("  end");
    }
    // 列を左から右へ並べるため、隣り合う列の先頭どうしを見えない線でつなぐ。
    for (let position = 1; position < firsts.length; position++) links.push(`  ${firsts[position - 1]} ~~~ ${firsts[position]};`);
    for (const edge of edges) links.push(`  ${index.get(edge.from).id} ${edge.id}@-->|"${edge.label}"| ${index.get(edge.to).id};`);
    return {source: [...lines, ...links, ...styles, ...classDefs(theme)].join("\n"), lanes: LANES, nodes, edges, groups};
  }

  // ---- 再生 ----------------------------------------------------------------------
  // 実時刻 → 再生時刻の単調な区分線形変換。境界(開始・終了)の間隔を対数で圧縮するため、
  // 順序と入れ子を保ちつつ、長い step ほど長く見える。
  function plan(items) {
    const valid = items.filter(item => Number.isFinite(item.start));
    const origin = valid.length ? Math.min(...valid.map(item => item.start)) : 0;
    const spansReal = valid.map(item => [item.key, item.start - origin, Math.max(item.start, Number.isFinite(item.end) ? item.end : item.start) - origin]);
    const points = [...new Set([0, ...spansReal.flatMap(([, start, end]) => [start, end])])].sort((a, b) => a - b);
    const total = Math.min(REPLAY_MAX, Math.max(REPLAY_MIN, REPLAY_PER_STEP * valid.length));
    const weights = points.slice(1).map((point, index) => {
      const gap = point - points[index];
      return gap > 0 ? Math.max(0.15, Math.log1p(gap / 50)) : 0;
    });
    const sum = weights.reduce((acc, weight) => acc + weight, 0);
    const mapped = [0];
    for (const weight of weights) mapped.push(mapped[mapped.length - 1] + (sum ? (weight / sum) * total : 0));
    if (mapped.length > 1) mapped[mapped.length - 1] = total;
    const realTotal = points[points.length - 1];
    const segment = (values, value) => {
      let low = 0, high = values.length - 1;
      while (low < high - 1) {
        const middle = (low + high) >> 1;
        if (values[middle] <= value) low = middle; else high = middle;
      }
      return low;
    };
    const warp = real => {
      if (points.length < 2 || real <= 0) return 0;
      if (real >= realTotal) return total;
      const index = segment(points, real), span = points[index + 1] - points[index];
      return mapped[index] + (span ? (real - points[index]) / span : 0) * (mapped[index + 1] - mapped[index]);
    };
    const elapsedAt = t => {
      if (points.length < 2 || t <= 0) return 0;
      if (t >= total) return realTotal;
      const index = segment(mapped, t), span = mapped[index + 1] - mapped[index];
      return points[index] + (span ? (t - mapped[index]) / span : 0) * (points[index + 1] - points[index]);
    };
    const spans = new Map(spansReal.map(([key, start, end]) => [key, {start: warp(start), end: warp(end)}]));
    return {total, realTotal, spans, elapsedAt};
  }

  function runPlan(detail) {
    const steps = Array.isArray(detail && detail.steps) ? detail.steps : [];
    const items = steps.map(step => ({key: step.id, start: instant(step.started_at), end: instant(step.started_at) + (Number(step.duration_ms) || 0)}));
    const finite = items.filter(item => Number.isFinite(item.start));
    const first = finite.length ? Math.min(...finite.map(item => item.start)) : 0;
    const last = finite.length ? Math.max(...finite.map(item => item.end)) : 0;
    items.push({key: "start", start: first, end: first}, {key: "end", start: last, end: last});
    // 「未記録の実行」枠の見出しは自分の時間を持たず、配下の step の状態から決める。
    const groups = new Map(tree(steps).root.filter(item => item.kind === "missing").map(group => [group.key, group.children.map(child => child.id)]));
    return {...plan(items), groups};
  }

  // 構成表示の再生。step の時間変換に、block ごとの配下(呼出し先の step、未記録の実行は呼び出した step)を添える。
  function structurePlan(detail) {
    const steps = Array.isArray(detail && detail.steps) ? detail.steps : [];
    const items = steps.map(step => ({key: step.id, start: instant(step.started_at), end: instant(step.started_at) + (Number(step.duration_ms) || 0)}));
    const finite = items.filter(item => Number.isFinite(item.start));
    const first = finite.length ? Math.min(...finite.map(item => item.start)) : 0;
    items.push({key: "start", start: first, end: first});
    const view = structure(detail);
    const blocks = new Map(view.nodes.filter(node => node.kind !== "start").map(node => [node.key, node.kind === "missing" ? node.outgoing : node.members]));
    return {...plan(items), blocks};
  }

  function sessionPlan(session) {
    const runs = Array.isArray(session && session.runs) ? session.runs : [];
    return plan(runs.map(run => ({key: run.run_id, start: instant(run.started_at), end: instant(run.started_at) + (Number(run.duration_ms) || 0)})));
  }

  // 再生時刻 t の各 node の状態。
  function frame(replay, t) {
    const states = new Map();
    for (const [key, span] of replay.spans) states.set(key, t < span.start ? "pending" : t >= span.end ? "done" : "running");
    for (const [key, members] of replay.groups ?? []) {
      const own = members.map(member => states.get(member));
      states.set(key, own.every(state => state === "pending") ? "pending" : own.every(state => state === "done") ? "done" : "running");
    }
    // 構成表示の block は配下の呼出しが実行中のときだけ実行中で、呼出しの合間は完了に戻る。
    for (const [key, members] of replay.blocks ?? []) {
      const own = members.map(member => states.get(member));
      states.set(key, own.some(state => state === "running") ? "running" : own.every(state => state === "pending") ? "pending" : "done");
    }
    return states;
  }

  return {THEMES, LANES, escapeHtml, mermaidLabel, statusLabel, statusClass, statusMark, kindInfo, duration, clock, shortId, shortTime, tree, flow, gantt, sessionFlow, structure, plan, runPlan, structurePlan, sessionPlan, frame};
})();
if (typeof module !== "undefined") module.exports = RunsModel;
