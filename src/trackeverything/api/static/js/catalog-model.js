"use strict";
// Agent / Tool カタログの純粋ロジック。DOM にも fetch も持たない
// (Node の標準ライブラリーだけで検証する)。mermaid 図の組立はここで行い、
// catalog.js は描画だけを担う。色は画面の design token と同じ対(light/dark)。
const CatalogModel = (() => {
  const THEMES = {
    light: {
      entrance: {fill: "#edf3ff", stroke: "#2256bd", color: "#224c9d"},
      inner: {fill: "#ffffff", stroke: "#dce1e9", color: "#202733"},
      unwired: {fill: "#fcebec", stroke: "#b9838c", color: "#8f2934"},
      source: {fill: "#e8f5ed", stroke: "#5c8a6e", color: "#186141"},
      tool: {fill: "#e6f4f7", stroke: "#3d8ea3", color: "#0e7490"},
      webhook: {fill: "#f1eafd", stroke: "#8a63d9", color: "#57319f"},
    },
    dark: {
      entrance: {fill: "#20314d", stroke: "#a2bdff", color: "#c4d6ff"},
      inner: {fill: "#1a1e25", stroke: "#363e4b", color: "#eef1f6"},
      unwired: {fill: "#3d2327", stroke: "#ffc3ca", color: "#ffc3ca"},
      source: {fill: "#1d382c", stroke: "#67b287", color: "#a0e4bc"},
      tool: {fill: "#17333d", stroke: "#4fb3c9", color: "#67e8f9"},
      webhook: {fill: "#2b2340", stroke: "#c4b5fd", color: "#c4b5fd"},
    },
  };

  function escapeHtml(value) {
    return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  // mermaid の label は "…" 引用 + htmlLabels。実体参照で埋め、改行は <br/> だけ許す。
  const mermaidLabel = lines => '"' + lines.filter(line => line !== "").map(escapeHtml).join("<br/>") + '"';
  // 記号類は code point 表記に置換し、識別子として衝突しない node id を作る。
  const nodeId = key => "n_" + String(key).replace(/[^A-Za-z0-9_]/g, ch => "u" + ch.codePointAt(0) + "_");

  function matches(entry, query) {
    const q = String(query ?? "").trim().toLowerCase();
    if (!q) return true;
    return [entry.name, entry.description, entry.module, entry.cls].some(field => String(field ?? "").toLowerCase().includes(q));
  }

  function unwired(agent) {
    return !agent.entrances.length && !agent.parents.length;
  }

  function argumentSummary(schema) {
    const properties = schema && typeof schema === "object" && typeof schema.properties === "object" ? schema.properties : {};
    const required = Array.isArray(schema && schema.required) ? schema.required : [];
    return Object.entries(properties).map(([name, prop]) => ({
      name,
      type: prop && typeof prop === "object" && typeof prop.type === "string" ? prop.type : "any",
      required: required.includes(name),
    }));
  }

  const agentBadgeLine = (agent, webhookEntrances) =>
    [
      webhookEntrances && webhookEntrances.some(key => agent.entrances.includes(key)) ? "Webhook 受け" : "",
      agent.tools.length ? "Tool " + agent.tools.length : "",
      agent.children.length ? "子 " + agent.children.length : "",
    ]
      .filter(Boolean)
      .join(" · ");
  const classDefs = theme => Object.entries(THEMES[theme] ?? THEMES.light).map(([name, palette]) => `  classDef ${name} fill:${palette.fill},stroke:${palette.stroke},color:${palette.color};`).join("\n");
  const nodeClass = agent => (agent.entrances.length ? "entrance" : unwired(agent) ? "unwired" : "inner");
  // Edge ID は Mermaid のアニメーションと SVG 側の選択に使う。
  function flowEdge(lines, from, to, label, dotted = false) {
    const id = "edge_" + lines.length;
    lines.push(`  ${from} ${id}@${dotted ? "-.->" : "-->"}|${mermaidLabel([label])}| ${to};`);
    lines.push(`  ${id}@{ animation: slow };`);
  }

  // 共通契約の説明。実行イベントや観測した処理時間ではない。
  const EXECUTION = [
    {key: "input", name: "Source / Webhook", kind: "source", description: "Source は入口の fetch_source が実行ごとに一度取得し、内容と identity を返します。取得の定義は dispatch Agent が組み立てます。Webhook は受信した payload を原形のまま投入します。", input: "Source の定義（source / source_kwargs）または Webhook payload", output: "内容・identity・受信情報"},
    {key: "invocation", name: "Queue", kind: "entrance", description: "受信・CLI・SDK・自動開発・保守の実行依頼を、一種類の EventRequest として Redis Streams に保存します。Agent は Source 自体を受け取りません。", input: "EventRequest", output: "メッセージ（stream id と実行依頼）"},
    {key: "execution", name: "Runtime", kind: "entrance", description: "版をリースし、全てのメッセージで実行単位の Context を作って dispatch Agent の run(call) を呼びます。Session の関連付けと記録を担当し、ACK は dispatch Agent が行います。", input: "メッセージ", output: "dispatch Agent への AgentCall（Context.data.event）"},
    {key: "pre", name: "Pre Harness", kind: "webhook", description: "宣言順に評価します。拒否・例外なら fail-closed で実行を止め、error を返します。未登録なら通過します。", input: "AgentCall", output: "通過、または error"},
    {key: "agent", name: "Agent / _execute()", kind: "entrance", description: "Agent が制御を持ちます。標準ループでは共有 step 予算の範囲で LLM を呼び、Tool・子 Agent・終了を判断します。具象 Agent は _execute() を変更できます。", input: "request と共有 Context.data", output: "AgentResult（status / response）"},
    {key: "llm", name: "LLM", kind: "inner", description: "メッセージと呼出し候補を受け取り、呼出し要求または最終回答を返します。Tool や子 Agent の実行は Agent が行います。", input: "messages と callable 定義", output: "Call または FinalAnswer"},
    {key: "tool", name: "Tool", kind: "tool", description: "call_tool() を経由して実行します。保存・通知などの副作用を担い、失敗や例外は ToolResult に変換します。Context は JSON 引数に含めません。", input: "JSON 引数と別経路の Context", output: "ToolResult（ok / output / error）"},
    {key: "child", name: "子 Agent", kind: "inner", description: "親は宣言した子の名前で call_agent(call, name, request) を呼びます。親と同じ Context.data と LLM step 予算を共有し、自分の pre / post も評価します。", input: "子の request と共有 Context", output: "子の AgentResult"},
    {key: "post", name: "Post Harness", kind: "webhook", description: "status=ok の結果に対して宣言順に評価します。拒否・例外は error に変換します。ok 以外の結果では評価しません。", input: "AgentCall と成功結果", output: "承認された結果、または error"},
    {key: "result", name: "AgentResult", kind: "source", description: "status と response を呼出し元へ返します。予算到達は max_steps、事前・事後の拒否は error、実行期限の超過は cancelled です。実行中の未処理例外はログを残して送出します。", input: "実行結果または Harness の判定", output: "呼出し元への結果"},
  ];

  function executionMermaid(theme = "light") {
    const lines = ["flowchart TB"];
    for (const node of EXECUTION) lines.push(`  ${nodeId("step:" + node.key)}[${mermaidLabel([node.name])}]:::${node.kind};`);
    for (const [from, to, label, dotted] of [
      ["input", "invocation", "投入"], ["invocation", "execution", "1 件を読む"], ["execution", "pre", "run(call)"],
      ["pre", "agent", "通過"], ["pre", "result", "拒否 → error", true],
      ["agent", "llm", "判断要求"], ["llm", "agent", "応答"],
      ["agent", "tool", "Tool 呼出し"], ["tool", "agent", "結果"],
      ["agent", "child", "委譲"], ["child", "agent", "結果"],
      ["agent", "post", "status = ok"], ["agent", "result", "ok 以外", true],
      ["post", "result", "承認 / 拒否 → error"],
    ]) flowEdge(lines, nodeId("step:" + from), nodeId("step:" + to), label, dotted);
    lines.push(classDefs(theme));
    return lines.join("\n") + "\n";
  }

  function diagramEntries(catalog, mode) {
    if (mode === "execution" || mode === "poster") return EXECUTION.map(entry => ({...entry, id: nodeId("step:" + entry.key), type: "実行ステップ"}));
    const agents = catalog.agents.map(entry => ({...entry, id: nodeId(entry.name), type: "Agent"}));
    if (mode === "tools") return agents.concat(catalog.tools.map(entry => ({...entry, id: nodeId("tool:" + entry.name), type: "Tool"})));
    return agents.concat(catalog.sources.map(entry => ({...entry, name: entry.key, id: nodeId("src:" + entry.key), type: "Source"})),
      [{id: nodeId("webhook"), name: "Webhook", type: "入力", description: "受信した payload を queue へ投入し、dispatch Agent が入口 Agent へ渡します。"}]);
  }

  // 委任グラフ: Source/ Webhook → 入口 Agent → 子 Agent。
  function delegationMermaid(catalog, theme = "light") {
    const lines = ["flowchart TB"];
    const declared = catalog.sources.map(source => `  ${nodeId("src:" + source.key)}[${mermaidLabel([source.key])}]:::source;`).join("\n");
    lines.push(`  subgraph ${nodeId("sources")}["Source（実行内で取得する入力）"]\n${declared}\n  end`);
    const webhooks = catalog.webhook_entrances.length ? [`  ${nodeId("webhook")}(("Webhook<br/>受信")):::webhook;`] : [];
    lines.push(...webhooks);
    for (const agent of catalog.agents) {
      lines.push(`  ${nodeId(agent.name)}[${mermaidLabel([agent.name, agentBadgeLine(agent, catalog.webhook_entrances)])}]:::${nodeClass(agent)};`);
    }
    for (const agent of catalog.agents) {
      for (const child of agent.children) flowEdge(lines, nodeId(agent.name), nodeId(child), "委譲可能");
    }
    for (const entrance of catalog.webhook_entrances) {
      const agent = catalog.agents.find(entry => entry.entrances.includes(entrance));
      flowEdge(lines, nodeId("webhook"), nodeId(agent ? agent.name : entrance), "受信", true);
    }
    lines.push(classDefs(theme));
    return lines.join("\n") + "\n";
  }

  // Tool 利用グラフ: Agent → Tool の二部図。未接続 Tool は枠でまとめる。
  function toolsMermaid(catalog, theme = "light") {
    const lines = ["flowchart LR"];
    for (const agent of catalog.agents) {
      lines.push(`  ${nodeId(agent.name)}[${mermaidLabel([agent.name])}]:::${nodeClass(agent)};`);
    }
    const lonely = catalog.tools.filter(tool => !tool.used_by.length).map(tool => `    ${nodeId("tool:" + tool.name)}[${mermaidLabel([tool.name])}]:::unwired;`).join("\n");
    if (lonely) lines.push(`  subgraph ${nodeId("lonely")}["未接続（どの Agent からも使われない）"]\n${lonely}\n  end`);
    for (const tool of catalog.tools) {
      if (tool.used_by.length) lines.push(`  ${nodeId("tool:" + tool.name)}[${mermaidLabel([tool.name])}]:::tool;`);
      for (const agent of tool.used_by) flowEdge(lines, nodeId(agent), nodeId("tool:" + tool.name), "使用可能");
    }
    lines.push(classDefs(theme));
    return lines.join("\n") + "\n";
  }

  return {THEMES, escapeHtml, mermaidLabel, nodeId, matches, unwired, argumentSummary, delegationMermaid, toolsMermaid, executionMermaid, diagramEntries};
})();
if (typeof module !== "undefined") module.exports = CatalogModel;
