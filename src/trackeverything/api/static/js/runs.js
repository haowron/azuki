(() => {
  "use strict";
  // 実行フロー画面(#/runs)の制御。ID の解決(run → session)・最近の実行・Mermaid 描画・
  // 再生・step の詳細を持つ。画面切替(hash → main の表示)は catalog.js が持ち、
  // ここは #/runs への到着と hash の ID で取得する。純粋ロジックは runs-model.js、
  // HTTP 契約は api.js を使う。仕様は docs/components/DASHBOARD_SPEC.md。
  const R = RunsModel, A = DashboardApi, $ = id => document.getElementById(id), esc = R.escapeHtml;
  const paths = new Map(Object.entries({
    flow:'<circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M7 6h10M6 8l5 8m7-8-5 8"/>',
    search:'<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',
    refresh:'<path d="M20 10a8 8 0 1 0 0 5M20 3v7h-7"/>',
    plus:'<path d="M12 5v14M5 12h14"/>', minus:'<path d="M5 12h14"/>',
    reset:'<path d="M3 12a9 9 0 1 0 3-6.7M3 3v6h6"/>',
  }));
  const icon = (key, size = 18) => `<svg class="tb-icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths.get(key) || paths.get("flow")}</svg>`;
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const fullTime = value => {
    const at = Date.parse(value ?? "");
    if (Number.isNaN(at)) return value ? String(value) : "—";
    return new Intl.DateTimeFormat("ja-JP", {dateStyle: "short", timeStyle: "medium", timeZone: zone}).format(new Date(at)) + "." + String(new Date(at).getMilliseconds()).padStart(3, "0") + " · " + zone;
  };
  const shortTime = value => {
    const at = Date.parse(value ?? "");
    return Number.isNaN(at) ? "—" : new Intl.DateTimeFormat("ja-JP", {dateStyle: "short", timeStyle: "short", timeZone: zone}).format(new Date(at));
  };
  const money = value => (typeof value === "number" && Number.isFinite(value) ? "$" + (value < 0.01 ? value.toFixed(4) : value.toFixed(3)) : "—");
  const media = query => (window.matchMedia ? window.matchMedia(query) : {matches: false, addEventListener() {}});
  const reducedMotion = media("(prefers-reduced-motion: reduce)");
  const darkQuery = media("(prefers-color-scheme: dark)");
  const theme = () => (darkQuery.matches ? "dark" : "light");
  const hashPath = id => "#/runs/" + encodeURIComponent(id);

  // ---- state ----------------------------------------------------------------
  let shownId, generation = 0, renderCount = 0;
  let recent = null, recentError = "", recentLoading = false;
  // mode: structure(構成・既定)/ flow(順序)/ gantt(タイムライン)。diagram は表示中の図のモデル。
  let session = null, run = null, selectedRun = null, mode = "structure", runDiagram = null, sessionDiagram = null, selectedNode = null, diagram = null;
  let scale = 1;

  function safeMessage(error) { return error instanceof Error ? error.message : String(error); }
  function showError(message) { $("tb-runs-error").textContent = message; $("tb-runs-error").hidden = !message; }

  // ---- mermaid --------------------------------------------------------------
  let mermaidLoading = null;
  function loadMermaid() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (!mermaidLoading) mermaidLoading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "static/js/vendor/mermaid.min.js";
      script.onload = () => resolve(window.mermaid);
      script.onerror = () => { mermaidLoading = null; script.remove(); reject(new Error("mermaid ライブラリーを読み込めませんでした")); };
      document.head.appendChild(script);
    });
    return mermaidLoading;
  }
  const mermaidInit = mermaid => mermaid.initialize({
    startOnLoad: false, theme: "base", securityLevel: "strict",
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans JP", sans-serif',
    themeVariables: theme() === "dark"
      ? {background: "#1a1e25", primaryColor: "#1a1e25", primaryBorderColor: "#363e4b", primaryTextColor: "#eef1f6", lineColor: "#abb5c5", tertiaryColor: "#242a34", clusterBkg: "#151920", clusterBorder: "#363e4b", edgeLabelBackground: "#1a1e25", sectionBkgColor: "#242a34", altSectionBkgColor: "#1a1e25", taskBkgColor: "#20314d", taskBorderColor: "#a2bdff", taskTextColor: "#eef1f6", taskTextDarkColor: "#eef1f6", taskTextOutsideColor: "#eef1f6", doneTaskBkgColor: "#1d382c", doneTaskBorderColor: "#67b287", critBkgColor: "#3d2327", critBorderColor: "#ffc3ca", activeTaskBkgColor: "#43351b", activeTaskBorderColor: "#f5d697", gridColor: "#363e4b", titleColor: "#eef1f6"}
      : {background: "#ffffff", primaryColor: "#ffffff", primaryBorderColor: "#dce1e9", primaryTextColor: "#202733", lineColor: "#586373", tertiaryColor: "#eef0f4", clusterBkg: "#f7f9fc", clusterBorder: "#dce1e9", edgeLabelBackground: "#ffffff", sectionBkgColor: "#eef0f4", altSectionBkgColor: "#ffffff", taskBkgColor: "#edf3ff", taskBorderColor: "#2256bd", taskTextColor: "#202733", taskTextDarkColor: "#202733", taskTextOutsideColor: "#202733", doneTaskBkgColor: "#e8f5ed", doneTaskBorderColor: "#5c8a6e", critBkgColor: "#fcebec", critBorderColor: "#b9838c", activeTaskBkgColor: "#fff2d4", activeTaskBorderColor: "#b8913c", gridColor: "#dce1e9", titleColor: "#202733"},
    flowchart: {htmlLabels: true, useMaxWidth: false, padding: 8, nodeSpacing: 28, rankSpacing: 34},
    gantt: {useMaxWidth: false, barHeight: 18, barGap: 4, topPadding: 40, leftPadding: 150, fontSize: 11, sectionFontSize: 12},
    maxEdges: 2000, maxTextSize: 200000,
  });
  async function draw(host, source) {
    const mermaid = await loadMermaid();
    mermaidInit(mermaid);
    const {svg} = await mermaid.render("tb-runs-svg-" + (++renderCount), source);
    host.innerHTML = `<div class="tb-diagram-wrap">${svg}</div>`;
    return host.querySelector ? host.querySelector("svg") : null;
  }
  // Mermaid の SVG から node(key → <g>)と連線(連線 id → <path>)を引き当てる。
  function bindNodes(host, nodes) {
    const byId = new Map(nodes.map(node => [node.id, node]));
    const found = new Map();
    for (const element of host.querySelectorAll ? host.querySelectorAll("g.node") : []) {
      const match = /^flowchart-(.+)-\d+$/.exec(element.id || "");
      const node = match && byId.get(match[1]);
      if (!node) continue;
      element.dataset.runNode = node.key;
      element.setAttribute("role", "button");
      element.setAttribute("tabindex", "0");
      found.set(node.key, element);
    }
    return found;
  }
  // 連線を再生の key(順序図は連線の先の step、構成図は連線が表す step)で引き当てる。
  function bindEdges(host, edges, keyOf = edge => edge.to) {
    const found = new Map();
    for (const edge of edges) {
      const path = host.querySelector ? host.querySelector(`path[data-id="${edge.id}"]`) || host.querySelector(`path[id$="${edge.id}"]`) : null;
      if (path) found.set(keyOf(edge), path);
    }
    return found;
  }

  // ---- 再生 -------------------------------------------------------------------
  // requestAnimationFrame で再生時刻 t を進め、各 node の data-replay と連線の光点を塗る。
  // シーク・速度変更と表示が常に一致するよう、状態は t だけから決める(R.frame)。
  const DOT_MS = 700;
  function createReplay(controls, statusLabelFor, host) {
    let replay = null, nodes = new Map(), edges = new Map(), dots = new Map(), followed = null, focusOf = key => key;
    // autoplay: 描画後の自動再生が未実行。resume: 非表示で止めた再生を表示時に再開する。
    let t = 0, playing = false, speed = 1, raf = 0, last = null, active = true, available = false, autoplay = false, resume = false;
    controls.innerHTML = `<button type="button" class="tb-button" data-replay="toggle" disabled>再生</button><button type="button" class="tb-button tb-quiet" data-replay="restart" disabled>最初から</button><label class="tb-motion-speed">速度 <select data-replay="speed" disabled><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option><option value="4">4×</option></select></label><input type="range" class="tb-runs-seek" data-replay="seek" min="0" max="1000" value="0" aria-label="再生位置" disabled><span class="tb-muted tb-runs-elapsed" role="status">再生を準備中</span>`;
    const part = key => (controls.querySelector ? controls.querySelector(`[data-replay="${key}"]`) : null);
    const elapsed = () => (controls.querySelector ? controls.querySelector(".tb-runs-elapsed") : null);
    function syncControls(message) {
      const usable = available && !reducedMotion.matches;
      for (const key of ["toggle", "restart", "speed", "seek"]) { const element = part(key); if (element) element.disabled = !usable; }
      const toggle = part("toggle");
      if (toggle) { toggle.textContent = playing ? "一時停止" : t >= (replay ? replay.total : 0) && t > 0 ? "もう一度" : "再生"; toggle.setAttribute("aria-pressed", String(playing)); }
      const status = elapsed();
      if (!status) return;
      if (message !== undefined) status.textContent = message;
      else if (reducedMotion.matches) status.textContent = "動きを減らす設定のため、最終状態を表示しています";
      else if (replay) status.textContent = "経過 " + R.clock(replay.elapsedAt(t)) + " / " + R.clock(replay.realTotal) + (playing ? "" : " · " + statusLabelFor(t >= replay.total));
    }
    function dotFor(path) {
      let dot = dots.get(path);
      if (!dot && path.parentNode && document.createElementNS) {
        dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        dot.setAttribute("r", "4");
        dot.setAttribute("class", "tb-flow-dot");
        dot.setAttribute("aria-hidden", "true");
        path.parentNode.appendChild(dot);
        dots.set(path, dot);
      }
      return dot;
    }
    function paint() {
      if (!replay) return;
      const states = R.frame(replay, t);
      for (const [key, element] of nodes) element.dataset.replay = states.get(key) ?? "done";
      for (const [to, path] of edges) {
        const state = states.get(to) ?? "done";
        path.dataset.replay = state;
        const span = replay.spans.get(to);
        const travelling = span && t >= span.start - DOT_MS && t < span.start && typeof path.getTotalLength === "function";
        const dot = travelling || dots.has(path) ? dotFor(path) : null;
        if (!dot) continue;
        if (!travelling) { dot.style.display = "none"; continue; }
        const point = path.getPointAtLength(path.getTotalLength() * (1 - (span.start - t) / DOT_MS));
        dot.setAttribute("cx", String(point.x)); dot.setAttribute("cy", String(point.y)); dot.style.display = "";
      }
      const seek = part("seek");
      if (seek) seek.value = String(Math.round((t / replay.total) * 1000));
      if (playing) follow(states);
      syncControls();
    }
    // 再生中は最後に始まった step が見えるように図をスクロールする(縦に長い run 向け)。
    function follow(states) {
      let latest = null, latestStart = -1;
      for (const [key, state] of states) {
        const span = replay.spans.get(key), target = focusOf(key);
        if (state !== "pending" && span && span.start >= latestStart && nodes.has(target)) { latest = target; latestStart = span.start; }
      }
      if (!latest || latest === followed || !host || typeof host.getBoundingClientRect !== "function") return;
      followed = latest;
      const box = nodes.get(latest).getBoundingClientRect(), frame = host.getBoundingClientRect();
      const outside = box.top < frame.top + 24 || box.bottom > frame.bottom - 24 || box.left < frame.left || box.right > frame.right;
      if (outside && typeof host.scrollTo === "function")
        host.scrollTo({top: host.scrollTop + box.top - frame.top - host.clientHeight / 3, left: host.scrollLeft + box.left - frame.left - host.clientWidth / 2 + box.width / 2, behavior: "smooth"});
    }
    function tick(now) {
      if (!playing || !replay) return;
      if (last === null) last = now;
      t = Math.min(replay.total, t + Math.min(100, now - last) * speed);
      last = now;
      paint();
      if (t >= replay.total) { playing = false; syncControls(); return; }
      raf = window.requestAnimationFrame(tick);
    }
    function play() {
      if (!replay || !available || reducedMotion.matches || !active) return;
      if (t >= replay.total) t = 0;
      playing = true; last = null; autoplay = false; resume = false;
      window.cancelAnimationFrame(raf);
      raf = window.requestAnimationFrame(tick);
      syncControls();
    }
    function pause() { playing = false; window.cancelAnimationFrame(raf); syncControls(); }
    controls.addEventListener("click", event => {
      const button = event.target.closest ? event.target.closest("[data-replay]") : null;
      if (!button || !replay) return;
      if (button.dataset.replay === "toggle") { if (playing) pause(); else play(); }
      if (button.dataset.replay === "restart") { t = 0; paint(); play(); }
    });
    controls.addEventListener("change", event => {
      if (event.target.dataset && event.target.dataset.replay === "speed") speed = Number(event.target.value) || 1;
    });
    controls.addEventListener("input", event => {
      if (!replay || !event.target.dataset || event.target.dataset.replay !== "seek") return;
      pause();
      t = (Number(event.target.value) / 1000) * replay.total;
      paint();
    });
    return {
      // 図を描き直すたびに最初から準備し、自動再生は 1 回だけ行う。
      // focus は step の key → 追従する node の key(構成図では呼出し先の block)。
      load(nextReplay, nodeElements, edgeElements, focus = key => key) {
        pause();
        for (const dot of dots.values()) dot.remove();
        dots = new Map();
        replay = nextReplay; nodes = nodeElements; edges = edgeElements; available = true; followed = null; focusOf = focus;
        t = reducedMotion.matches ? replay.total : 0;
        autoplay = !reducedMotion.matches; resume = false;
        paint();
        play();
      },
      disable(message) { pause(); replay = null; available = false; autoplay = false; resume = false; nodes = new Map(); edges = new Map(); syncControls(message); },
      setActive(value) {
        if (value === active) return;
        active = value;
        if (!value) { if (playing) resume = true; pause(); }
        else if (autoplay || resume) play();
      },
      reduceMotion() { if (!replay) return; pause(); t = reducedMotion.matches ? replay.total : t; paint(); },
    };
  }
  const runReplay = createReplay($("tb-runs-controls"), done => (done ? "再生終了" : "一時停止中"), $("tb-runs-body"));
  const sessionReplay = createReplay($("tb-runs-session-controls"), done => (done ? "再生終了" : "一時停止中"), $("tb-runs-session-body"));

  // ---- 詳細 -------------------------------------------------------------------
  function field(label, value) { return value === null || value === undefined || value === "" ? "" : `<dt>${esc(label)}</dt><dd>${value}</dd>`; }
  function runFields(summary) {
    return field("run ID", `<code>${esc(summary.run_id)}</code>`)
      + field("session", summary.session_id ? `<a href="${esc(hashPath(summary.session_id))}"><code>${esc(summary.session_id)}</code></a>` : "なし")
      + field("入口 Agent", summary.agent ? `<code>${esc(summary.agent)}</code>` : "不明")
      + field("状態", esc(R.statusLabel(summary.status)) + (summary.complete ? "" : "（根 Agent の記録なし）"))
      + field("開始", esc(fullTime(summary.started_at)))
      + field("全体の所要時間", esc(R.duration(summary.duration_ms)))
      + field("step 数", esc(String(summary.step_count)) + (run && run.truncated ? "（先頭 400 件を表示）" : ""))
      + field("LLM 費用", esc(money(summary.cost_usd)))
      + field("失敗 step", esc(String(summary.error_count)));
  }
  function showDetail(key) {
    selectedNode = key;
    for (const [nodeKey, element] of runDiagram ? runDiagram.elements : []) element.classList.toggle("tb-node-selected", nodeKey === key);
    const detail = $("tb-runs-detail");
    if (!run) return;
    if (key === "start" || key === "end" || key === null) {
      detail.innerHTML = `<span class="tb-muted">${key === "end" ? "結果" : "run"}</span><h3>${esc(run.run.agent || "run")}</h3><dl>${runFields(run.run)}</dl>`;
      return;
    }
    if (String(key).startsWith("missing:")) {
      const parent = String(key).slice("missing:".length);
      detail.innerHTML = `<span class="tb-muted">未記録の実行</span><h3>親 Agent の記録なし</h3><p>この枠の step は親の実行 ID を持ちますが、親 Agent の記録がありません。実行中・異常終了・旧形式の記録で起こります。</p><dl>${field("親の実行 ID", `<code>${esc(parent)}</code>`)}</dl>`;
      return;
    }
    const block = mode === "structure" && diagram && diagram.nodes ? diagram.nodes.find(node => node.key === key) : null;
    if (block) { detail.innerHTML = blockDetail(block); return; }
    const step = run.steps.find(item => item.id === key);
    if (!step) return;
    const tokens = step.tokens ? ["入力 " + (step.tokens.input ?? "—"), "出力 " + (step.tokens.output ?? "—"), "合計 " + (step.tokens.total ?? "—")].join(" · ") : "";
    const errors = step.errors && step.errors.length ? `<ul class="tb-runs-errors">${step.errors.map(error => `<li>${esc(error)}</li>`).join("")}</ul>` : "";
    detail.innerHTML = `<span class="tb-muted">${esc(R.kindInfo(step.kind).label)}</span><h3>${esc(step.name)}</h3><dl>
      ${field("状態", `<span class="tb-runs-status is-${esc(R.statusClass(step.status))}">${esc(R.statusMark(step.status) + " " + R.statusLabel(step.status))}</span>`)}
      ${field("開始", esc(fullTime(step.started_at)))}
      ${field("所要時間", esc(R.duration(step.duration_ms)))}
      ${field("token", tokens && esc(tokens))}
      ${field("費用", step.cost_usd === null || step.cost_usd === undefined ? "" : esc(money(step.cost_usd)))}
      ${field("エラー", errors)}
      ${field("抜粋", step.preview ? `<pre class="tb-runs-preview">${esc(step.preview)}</pre>` : "")}
      ${field("実行 ID", `<code>${esc(step.id)}</code>`)}
      ${field("親の実行 ID", step.parent ? `<code>${esc(step.parent)}</code>` : "")}</dl>`;
  }

  // 構成図の block: 回数・合計と、配下の呼出しを開始順(図の番号)に一覧する。
  function blockDetail(block) {
    const order = new Map((diagram.edges || []).map(edge => [edge.step, edge.label.replace("✗", "")]));
    const time = value => {
      const at = Date.parse(value ?? "");
      return Number.isNaN(at) ? "—" : new Intl.DateTimeFormat("ja-JP", {timeStyle: "medium", timeZone: zone}).format(new Date(at)) + "." + String(new Date(at).getMilliseconds()).padStart(3, "0");
    };
    const calls = block.members.map(id => run.steps.find(step => step.id === id)).filter(Boolean);
    const rows = calls.map(step => `<li><div class="tb-runs-call-head"><strong>#${esc(order.get(step.id) ?? "?")}</strong><span>${esc(time(step.started_at))}</span><span>${esc(R.duration(step.duration_ms))}</span><span class="tb-runs-status is-${esc(R.statusClass(step.status))}">${esc(R.statusMark(step.status) + " " + R.statusLabel(step.status))}</span>${step.tokens && step.tokens.total !== null && step.tokens.total !== undefined ? `<span class="tb-muted">${esc(step.tokens.total + " tok")}</span>` : ""}</div>${step.errors && step.errors.length ? `<ul class="tb-runs-errors">${step.errors.map(error => `<li>${esc(error)}</li>`).join("")}</ul>` : ""}${step.preview ? `<pre class="tb-runs-preview">${esc(step.preview)}</pre>` : ""}</li>`).join("");
    const lane = (R.LANES.find(item => item.key === block.lane) || {title: block.lane}).title;
    return `<span class="tb-muted">${esc(lane + (block.group ? " · " + block.group.slice("skill:".length) : ""))}</span><h3>${esc(block.name)}</h3><dl>
      ${field("呼出し", esc(block.calls + "回"))}${field("合計時間", esc(R.duration(block.total_ms)))}${field("失敗", esc(String(block.failures)))}
      ${field("費用", block.cost === null ? "" : esc(money(block.cost)))}</dl><ol class="tb-runs-calls">${rows}</ol>`;
  }

  // ---- 描画 -------------------------------------------------------------------
  function summaryChips(summary) {
    const cls = R.statusClass(summary.status);
    const chip = (text, extra = "") => `<span class="tb-metric tb-cat-metric${extra}">${esc(text)}</span>`;
    return chip(R.statusMark(summary.status) + " " + R.statusLabel(summary.status), " tb-runs-status is-" + cls)
      + chip("入口 " + (summary.agent || "不明"))
      + chip("開始 " + shortTime(summary.started_at))
      + chip("全体 " + R.duration(summary.duration_ms))
      + chip(summary.step_count + " step" + (run && run.truncated ? "（400 件まで表示）" : ""))
      + (summary.cost_usd !== null && summary.cost_usd !== undefined ? chip("LLM " + money(summary.cost_usd)) : "")
      + (summary.error_count ? chip("失敗 " + summary.error_count, " tb-runs-status is-error") : "");
  }
  function renderCrumbs() {
    const parts = [`<a href="#/runs">← 最近の実行</a>`];
    if (session) parts.push(`<span>session <code>${esc(session.session_id)}</code></span>`);
    else if (run && run.run.session_id) parts.push(`<a href="${esc(hashPath(run.run.session_id))}">この run の session <code>${esc(run.run.session_id)}</code> を表示</a>`);
    $("tb-runs-crumbs").innerHTML = parts.join("");
    $("tb-runs-crumbs").hidden = shownId === null;
  }
  function applyZoom() {
    const svg = $("tb-runs-body").querySelector ? $("tb-runs-body").querySelector("svg") : null;
    if (svg) {
      const natural = Number(svg.dataset.naturalWidth) || svg.getBoundingClientRect().width;
      svg.dataset.naturalWidth = String(natural);
      if (natural) { svg.style.width = natural * scale + "px"; svg.style.height = "auto"; svg.style.maxWidth = "none"; }
    }
    $("tb-runs-zoom-label").textContent = Math.round(scale * 100) + "%";
  }
  function fitScale() {
    const host = $("tb-runs-body"), svg = host.querySelector ? host.querySelector("svg") : null;
    if (!svg || !host.clientWidth) return 1;
    const natural = Number(svg.dataset.naturalWidth) || svg.getBoundingClientRect().width;
    svg.dataset.naturalWidth = String(natural);
    // 文字が読める 80% を下限にし、それより広い図は横スクロールで見る。
    return natural ? Math.min(1, Math.max(0.8, (host.clientWidth - 32) / natural)) : 1;
  }

  async function renderRun() {
    const figure = $("tb-runs-run");
    figure.hidden = !run;
    if (!run) return;
    const current = ++generation;
    $("tb-runs-run-title").textContent = "run " + R.shortId(run.run.run_id);
    $("tb-runs-run-title").title = run.run.run_id;
    $("tb-runs-summary").innerHTML = summaryChips(run.run);
    for (const button of document.querySelectorAll("#tb-runs-mode [data-mode]")) button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    renderCrumbs();
    const host = $("tb-runs-body");
    diagram = mode === "gantt" ? {source: R.gantt(run), nodes: [], edges: []} : mode === "structure" ? R.structure(run, theme()) : R.flow(run, theme());
    const shown = diagram;
    $("tb-runs-source").textContent = shown.source;
    $("tb-runs-caption").textContent = mode === "gantt"
      ? "実時刻の時間軸に step を並べます。赤は失敗です。タイムラインは再生しません。"
      : mode === "structure"
      ? "左から Agent・LLM・Tool・Skill の列です。同じ相手への呼出しは同じ block への矢印を足し、番号は呼出し順（✗ は失敗）です。block を選ぶと呼出しの一覧を表示します。"
      : "上から実行順に並びます。枠は Agent の範囲、色は結果（緑=成功・赤=失敗・黄=注意）です。node を選ぶと詳細を表示します。";
    host.setAttribute("aria-busy", "true");
    host.textContent = "図を描画中…";
    runReplay.disable("再生を準備中");
    try {
      await draw(host, shown.source);
      if (current !== generation) return;
      const elements = bindNodes(host, shown.nodes);
      runDiagram = {elements};
      scale = fitScale();
      applyZoom();
      if (mode === "gantt") runReplay.disable("タイムラインは再生しません");
      else if (mode === "structure") {
        const target = new Map(shown.edges.map(edge => [edge.step, edge.to]));
        runReplay.load(R.structurePlan(run), elements, bindEdges(host, shown.edges, edge => edge.step), key => target.get(key) ?? key);
      } else runReplay.load(R.runPlan(run), elements, bindEdges(host, shown.edges));
      const keys = new Set(shown.nodes.map(node => node.key));
      showDetail(selectedNode && keys.has(selectedNode) ? selectedNode : mode === "structure" ? "start" : "end");
    } catch (error) {
      if (current !== generation) return;
      runReplay.disable("図を描画できませんでした");
      host.innerHTML = `<p class="tb-note">図の描画に失敗しました(${esc(safeMessage(error))})。生成した Mermaid ソース:</p><pre class="tb-diagram-source">${esc(shown.source)}</pre>`;
    } finally {
      host.setAttribute("aria-busy", "false");
    }
  }

  async function renderSession() {
    const figure = $("tb-runs-session");
    figure.hidden = !session;
    if (!session) return;
    const shown = session.runs.length, hidden = session.total_runs - shown;
    $("tb-runs-session-title").textContent = "session " + R.shortId(session.session_id);
    $("tb-runs-session-title").title = session.session_id;
    const counts = {};
    for (const item of session.runs) counts[item.status] = (counts[item.status] ?? 0) + 1;
    $("tb-runs-session-summary").innerHTML = [`run ${session.total_runs} 件`, ...Object.entries(counts).map(([status, count]) => R.statusLabel(status) + " " + count)]
      .map(text => `<span class="tb-metric tb-cat-metric">${esc(text)}</span>`).join("")
      + `<span class="tb-muted tb-runs-session-note">${esc("作成 " + shortTime(session.created_at) + (session.parent_session_id ? " · 親 session " + session.parent_session_id : "") + (hidden > 0 ? " · 古い " + hidden + " 件は省略（最新 " + shown + " 件）" : ""))}</span>`;
    const diagram = R.sessionFlow(session, theme());
    $("tb-runs-session-source").textContent = diagram.source;
    const host = $("tb-runs-session-body");
    if (!shown) { host.innerHTML = `<div class="tb-empty">この session に run の記録はありません。</div>`; sessionReplay.disable("run がありません"); return; }
    host.textContent = "図を描画中…";
    sessionReplay.disable("再生を準備中");
    const drawing = session;
    try {
      await draw(host, diagram.source);
      if (session !== drawing) return;
      const elements = bindNodes(host, diagram.nodes);
      sessionDiagram = {elements};
      markSelectedRun();
      sessionReplay.load(R.sessionPlan(session), elements, bindEdges(host, diagram.edges));
    } catch (error) {
      if (session !== drawing) return;
      sessionReplay.disable("図を描画できませんでした");
      host.innerHTML = `<p class="tb-note">図の描画に失敗しました(${esc(safeMessage(error))})。</p><pre class="tb-diagram-source">${esc(diagram.source)}</pre>`;
    }
  }
  function markSelectedRun() {
    for (const [key, element] of sessionDiagram ? sessionDiagram.elements : []) {
      element.classList.toggle("tb-node-selected", key === selectedRun);
      element.setAttribute("aria-pressed", String(key === selectedRun));
      element.setAttribute("aria-label", "run " + key + " の流れを表示");
    }
  }

  function renderRecent() {
    const host = $("tb-runs-recent");
    host.hidden = shownId !== null;
    if (shownId !== null) return;
    if (!recent) { host.innerHTML = `<div class="tb-empty">${recentLoading ? "読み込み中…" : esc(recentError || "最近の実行を取得できませんでした。接続設定を確認して「更新」で再試行してください。")}</div>`; return; }
    const rows = recent.map(item => `<tr>
      <th scope="row"><button type="button" class="tb-runs-pick" data-run-pick="${esc(item.run_id)}" title="${esc(item.run_id)}"><code>${esc(R.shortId(item.run_id))}</code></button></th>
      <td><span class="tb-runs-status is-${esc(R.statusClass(item.status))}">${esc(R.statusMark(item.status) + " " + R.statusLabel(item.status))}</span></td>
      <td><code>${esc(item.agent || "不明")}</code></td>
      <td class="tb-usage-last">${esc(shortTime(item.started_at))}</td>
      <td class="tb-usage-num">${esc(R.duration(item.duration_ms))}</td>
      <td class="tb-usage-num">${esc(String(item.step_count))}</td>
      <td>${item.session_id ? `<button type="button" class="tb-runs-pick" data-run-pick="${esc(item.session_id)}" title="${esc(item.session_id)}"><code>${esc(item.session_id)}</code></button>` : `<span class="tb-muted">—</span>`}</td>
    </tr>`).join("");
    host.innerHTML = `<section class="tb-cat-section"><h2>${icon("flow")}<span>最近の実行</span><span class="tb-cat-count">${recent.length}</span></h2>
      <div class="tb-cat-rows">${recent.length ? `<table class="tb-usage-table tb-runs-table"><thead><tr><th>run</th><th>状態</th><th>入口 Agent</th><th>開始</th><th class="tb-usage-num">所要時間</th><th class="tb-usage-num">step</th><th>session</th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="tb-empty">実行の記録はまだありません。</div>`}</div></section>`;
  }

  // ---- データ -----------------------------------------------------------------
  async function loadRecent() {
    if (recentLoading) return;
    recentLoading = true; recentError = ""; renderRecent();
    try {
      const body = await A.runs(20);
      recent = Array.isArray(body.items) ? body.items : [];
    } catch (error) {
      recent = null; recentError = "最近の実行を取得できませんでした: " + safeMessage(error);
    } finally {
      recentLoading = false; renderRecent();
    }
  }
  async function loadRun(runId) {
    const ticket = ++generation;
    selectedRun = runId; markSelectedRun();
    try {
      const body = await A.run(runId);
      if (ticket !== generation) return;
      run = body; selectedNode = null;
      await renderRun();
    } catch (error) {
      if (ticket === generation) showError("run を取得できませんでした: " + safeMessage(error));
    }
  }
  async function show(id, preferredRun = null) {
    const ticket = ++generation;
    showError(""); session = null; run = null; selectedRun = null; selectedNode = null;
    $("tb-runs-input").value = id;
    $("tb-runs-session").hidden = true; $("tb-runs-run").hidden = true;
    runReplay.disable(""); sessionReplay.disable("");
    renderRecent(); renderCrumbs();
    try {
      const body = await A.run(id);
      if (ticket !== generation) return;
      run = body;
      await renderRun();
      return;
    } catch (error) {
      if (ticket !== generation) return;
      if (error.status !== 404) { showError("run を取得できませんでした: " + safeMessage(error)); return; }
    }
    try {
      const body = await A.session(id);
      if (ticket !== generation) return;
      session = body;
      renderCrumbs();
      // 初期表示は最新の run。更新では選択中の run が残っていればそれを保つ。
      const keep = preferredRun && session.runs.some(item => item.run_id === preferredRun) ? preferredRun : null;
      const target = keep ?? (session.runs.length ? session.runs[session.runs.length - 1].run_id : null);
      await Promise.all([renderSession(), target ? loadRun(target) : Promise.resolve()]);
    } catch (error) {
      if (ticket !== generation) return;
      showError(error.status === 404 ? "run と session のどちらにも見つかりません: " + id : "session を取得できませんでした: " + safeMessage(error));
    }
  }
  function idFromHash() {
    const match = /^#\/runs(?:\/(.*))?$/.exec(location.hash || "");
    if (!match) return undefined;
    if (!match[1]) return null;
    try { return decodeURIComponent(match[1]); } catch { return match[1]; }
  }
  function route(force = false, preferredRun = null) {
    const id = idFromHash();
    const visible = id !== undefined;
    runReplay.setActive(visible && !document.hidden);
    sessionReplay.setActive(visible && !document.hidden);
    if (!visible || (!force && id === shownId)) return;
    shownId = id;
    if (id === null) {
      generation++; session = null; run = null; showError("");
      $("tb-runs-session").hidden = true; $("tb-runs-run").hidden = true;
      runReplay.disable(""); sessionReplay.disable("");
      renderCrumbs();
      if (!recent || force) loadRecent(); else renderRecent();
    } else show(id, preferredRun);
  }
  function go(id) {
    const target = hashPath(id);
    if (location.hash !== target) location.hash = target;
    route();
  }

  // ---- events ---------------------------------------------------------------
  $("tb-runs-form").addEventListener("submit", event => {
    event.preventDefault();
    const id = String($("tb-runs-input").value ?? "").trim();
    if (id) go(id);
  });
  $("tb-runs-recent").addEventListener("click", event => {
    const pick = event.target.closest ? event.target.closest("[data-run-pick]") : null;
    if (pick) go(pick.dataset.runPick);
  });
  const nodeActivate = (event, handler) => {
    const node = event.target.closest ? event.target.closest("[data-run-node]") : null;
    if (!node) return;
    if (event.type === "keydown") { if (event.key !== "Enter" && event.key !== " ") return; event.preventDefault(); }
    handler(node.dataset.runNode);
  };
  for (const type of ["click", "keydown"]) {
    $("tb-runs-body").addEventListener(type, event => nodeActivate(event, showDetail));
    $("tb-runs-session-body").addEventListener(type, event => nodeActivate(event, key => { if (key !== selectedRun) loadRun(key); }));
  }
  for (const button of document.querySelectorAll("#tb-runs-mode [data-mode]"))
    button.addEventListener("click", () => { if (mode !== button.dataset.mode) { mode = button.dataset.mode; renderRun(); } });
  for (const [id, delta] of [["tb-runs-zoom-in", 0.15], ["tb-runs-zoom-out", -0.15]])
    $(id).addEventListener("click", () => { scale = Math.min(2, Math.max(0.3, Math.round((scale + delta) * 20) / 20)); applyZoom(); });
  $("tb-runs-zoom-reset").addEventListener("click", () => { scale = fitScale(); applyZoom(); });
  $("tb-runs-refresh").addEventListener("click", () => {
    route(true, selectedRun);
    $("tb-runs-update-time").textContent = "最終更新 " + new Intl.DateTimeFormat("ja-JP", {timeStyle: "medium"}).format(new Date());
  });
  window.addEventListener("hashchange", () => route());
  $("token").addEventListener && $("token").addEventListener("change", () => { if (idFromHash() !== undefined) route(true); });
  document.addEventListener("visibilitychange", () => route());
  reducedMotion.addEventListener("change", () => { runReplay.reduceMotion(); sessionReplay.reduceMotion(); });
  darkQuery.addEventListener("change", () => { if (idFromHash() === undefined) return; if (run) renderRun(); if (session) renderSession(); });
  if (typeof IntersectionObserver === "function") {
    for (const [id, replay] of [["tb-runs-body", runReplay], ["tb-runs-session-body", sessionReplay]])
      new IntersectionObserver(entries => replay.setActive(entries[entries.length - 1].isIntersecting && idFromHash() !== undefined && !document.hidden)).observe($(id));
  }

  for (const [id, key, tip] of [["tb-runs-icon", "flow", ""], ["tb-runs-search-icon", "search", ""], ["tb-runs-refresh", "refresh", "更新"], ["tb-runs-zoom-in", "plus", "拡大"], ["tb-runs-zoom-out", "minus", "縮小"], ["tb-runs-zoom-reset", "reset", "枠幅に合わせる"]])
    if ($(id)) { $(id).innerHTML = icon(key); if (tip) { $(id).title = tip; $(id).dataset.tip = tip; $(id).setAttribute("aria-label", tip); } }
  route();
})();
