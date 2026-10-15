(() => {
  "use strict";
  // Agent / Tool カタログ画面の制御。表示切替(hash)・取得・描画・mermaid 図を
  // ここで持ち、app.js(共通部品)とは独立に動く。純粋ロジックは
  // catalog-model.js、HTTP 契約は api.js を使う。
  const M = CatalogModel, A = DashboardApi, $ = id => document.getElementById(id), esc = M.escapeHtml;
  const paths = new Map(Object.entries({
    catalog:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M17.5 14v7M14 17.5h7"/>',
    route:'<circle cx="5" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 5h9a4 4 0 0 1 0 8H8a3 3 0 0 0 0 6h9"/>',
    braces:'<path d="M8 3H6v6l-3 3 3 3v6h2M16 3h2v6l3 3-3 3v6h-2"/>',
    shield:'<path d="M12 3 4 6v5c0 5 3.4 8.3 8 10 4.6-1.7 8-5 8-10V6l-8-3Z"/>',
    source:'<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    search:'<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',
    refresh:'<path d="M20 10a8 8 0 1 0 0 5M20 3v7h-7"/>',
    down:'<path d="m6 9 6 6 6-6"/>', right:'<path d="m9 6 6 6-6 6"/>',
    plus:'<path d="M12 5v14M5 12h14"/>', minus:'<path d="M5 12h14"/>',
    reset:'<path d="M3 12a9 9 0 1 0 3-6.7M3 3v6h6"/>',
  }));
  const icon = (key, cls = "", size = 18) => `<svg class="tb-icon${cls ? " " + cls : ""}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths.get(key) || paths.get("source")}</svg>`;

  // ---- state ----------------------------------------------------------------
  let view = "", data = null, loaded = false, loading = false;
  let query = "", mode = "poster", scale = 1, fitScale = 1, unwiredOnly = false;
  const sections = new Set(["agents", "tools", "harnesses", "sources"]);
  const expanded = new Set();
  let mermaidLoading = null, diagramGeneration = 0;
  let animeLoading = null, animations = [], diagramVisible = false, selectedNode = null;
  let diagramSource = "", diagramEntries = [], motionAvailable = false;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let playing = !reducedMotion.matches, speed = 1;
  const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const theme = () => (darkQuery && darkQuery.matches ? "dark" : "light");

  // ---- data -----------------------------------------------------------------
  function safeMessage(error) { return error instanceof Error ? error.message : String(error); }

  async function load() {
    if (loading) return;
    loading = true;
    $("tb-cat-refresh").disabled = true;
    renderStatus();
    try {
      const catalog = await A.catalog();
      data = {agents: [], tools: [], harnesses: [], sources: [], ...catalog};
      loaded = true;
      const snapshotTime = data.as_of ? new Intl.DateTimeFormat("ja-JP", {dateStyle: "short", timeStyle: "medium"}).format(new Date(data.as_of)) : "未作成";
      $("tb-cat-update-time").textContent = (data.online ? "runtime 稼働中" : "runtime 更新待ち") + " · 保存 " + snapshotTime;
      $("tb-cat-update-time").title = data.code_version || "runtime のカタログ公開を待っています";
    } catch (error) {
      // 401 のときは api.js の onUnauthorized が接続設定を開いてくれる。
      $("tb-cat-error").textContent = "カタログを取得できませんでした: " + safeMessage(error);
      $("tb-cat-error").hidden = false;
    } finally {
      loading = false;
      $("tb-cat-refresh").disabled = false;
      renderAll();
    }
  }

  function ensureData() { if (!loaded && !loading) load(); }

  // ---- mermaid --------------------------------------------------------------
  function loadMermaid() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (!mermaidLoading) {
      mermaidLoading = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "static/js/vendor/mermaid.min.js";
        script.onload = () => resolve(window.mermaid);
        script.onerror = () => { mermaidLoading = null; reject(new Error("mermaid ライブラリーを読み込めませんでした")); };
        document.head.appendChild(script);
      });
    }
    return mermaidLoading;
  }

  function loadAnime() {
    if (window.anime) return Promise.resolve(window.anime);
    if (!animeLoading) animeLoading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "static/js/vendor/anime.umd.min.js";
      script.onload = () => resolve(window.anime);
      script.onerror = () => { animeLoading = null; script.remove(); reject(new Error("光点の動きを読み込めませんでした")); };
      document.head.appendChild(script);
    });
    return animeLoading;
  }

  function clearMotion() {
    for (const animation of animations) animation.revert();
    animations = [];
    $("tb-diagram-body").querySelectorAll(".tb-flow-dot").forEach(dot => dot.remove());
  }

  function syncMotion() {
    const active = motionAvailable && playing && !reducedMotion.matches && diagramVisible && view === "catalog" && !document.hidden;
    $("tb-diagram-body").dataset.motion = active ? "running" : "paused";
    $("tb-diagram-body").classList.toggle("tb-reduced-motion", reducedMotion.matches);
    for (const animation of animations) {
      animation.speed = speed;
      if (active) animation.resume(); else animation.pause();
    }
    $("tb-diagram-play").textContent = playing ? "一時停止" : "再生";
    $("tb-diagram-play").disabled = reducedMotion.matches || !motionAvailable;
    $("tb-diagram-play").setAttribute("aria-pressed", String(playing && !reducedMotion.matches));
    $("tb-diagram-speed").disabled = reducedMotion.matches || !motionAvailable;
    $("tb-diagram-motion-status").textContent = reducedMotion.matches ? "動きを減らす設定が有効です" : !motionAvailable ? "動きを準備中" : playing ? "流れる方向を表示中" : "一時停止中";
    $("tb-diagram-body").style.setProperty("--tb-flow-duration", (50 / speed) + "s");
  }

  function bindDiagramNodes() {
    for (const node of $("tb-diagram-body").querySelectorAll("g.node")) {
      const entry = diagramEntries.find(entry => node.id.startsWith("flowchart-" + entry.id + "-"));
      if (!entry) continue;
      node.dataset.catalogNode = entry.id;
      node.setAttribute("role", "button");
      node.setAttribute("tabindex", "0");
      node.setAttribute("aria-label", entry.name + "の詳細");
      node.setAttribute("aria-controls", "tb-diagram-detail");
    }
  }

  function selectNode(id) {
    selectedNode = id;
    const entry = diagramEntries.find(entry => entry.id === id);
    for (const node of $("tb-diagram-body").querySelectorAll("[data-catalog-node]")) {
      node.classList.toggle("tb-node-selected", node.dataset.catalogNode === id);
      node.setAttribute("aria-pressed", String(node.dataset.catalogNode === id));
    }
    for (const edge of $("tb-diagram-body").querySelectorAll(".poster-edge"))
      edge.classList.toggle("poster-edge-selected", edge.dataset.from === id || edge.dataset.to === id);
    const detail = $("tb-diagram-detail");
    if (!entry) { detail.innerHTML = '<h3>ノードから詳しく見る</h3><p>図のノードを選ぶと、役割・入出力・接続先を確認できます。キーボードでも選択できます。</p>'; return; }
    const field = (label, value) => value ? `<dt>${label}</dt><dd>${value}</dd>` : "";
    const names = list => list && list.length ? list.map(name => `<code>${esc(name)}</code>`).join("、") : "なし";
    detail.innerHTML = `<span class="tb-muted">${esc(entry.type)}</span><h3>${esc(entry.name)}</h3><p>${esc(entry.description || "説明は登録されていません。")}</p><dl>
      ${field("入力", entry.input && esc(entry.input))}${field("出力", entry.output && esc(entry.output))}
      ${entry.type === "Agent" ? field("入口", names(entry.entrances)) + field("子 Agent", names(entry.children)) + field("Tool", names(entry.tools)) + field("Pre / Post", names(entry.pre) + " / " + names(entry.post)) : ""}
      ${entry.type === "Tool" ? field("使用 Agent", names(entry.used_by)) : ""}
      ${entry.arguments ? field("引数", argumentsHtml(entry.arguments)) : ""}
      ${entry.module ? field("実装", provenance(entry)) : ""}</dl>`;
  }

  function attachMotion(anime) {
    // 大きな関係図でも同時に動かす光点は24個まで。連線は全件表示する。
    const edges = [...$("tb-diagram-body").querySelectorAll("path.flowchart-link")];
    for (const [index, path] of edges.slice(0, 24).entries()) {
      const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dot.setAttribute("r", "3.5");
      dot.setAttribute("class", "tb-flow-dot");
      dot.setAttribute("aria-hidden", "true");
      path.parentNode.appendChild(dot);
      const {translateX, translateY} = anime.svg.createMotionPath(path);
      animations.push(anime.animate(dot, {translateX, translateY, duration: Math.max(1800, path.getTotalLength() * 16), delay: index * 90, ease: "linear", loop: true, autoplay: false}));
    }
  }

  const mermaidInit = mermaid => mermaid.initialize({
    startOnLoad: false,
    theme: "base",
    securityLevel: "strict",
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans JP", sans-serif',
    themeVariables: theme() === "dark"
      ? {background: "#1a1e25", primaryColor: "#1a1e25", primaryBorderColor: "#363e4b", primaryTextColor: "#eef1f6", lineColor: "#abb5c5", tertiaryColor: "#242a34", clusterBkg: "#242a34", clusterBorder: "#363e4b", edgeLabelBackground: "#1a1e25"}
      : {background: "#ffffff", primaryColor: "#ffffff", primaryBorderColor: "#dce1e9", primaryTextColor: "#202733", lineColor: "#586373", tertiaryColor: "#eef0f4", clusterBkg: "#eef0f4", clusterBorder: "#dce1e9", edgeLabelBackground: "#ffffff"},
    flowchart: {htmlLabels: true, useMaxWidth: false, padding: 10},
  });

  async function renderDiagram() {
    const host = $("tb-diagram-body");
    if (!data || !host) return;
    const source = mode === "execution" || mode === "poster" ? M.executionMermaid(theme()) : mode === "tools" ? M.toolsMermaid(data, theme()) : M.delegationMermaid(data, theme());
    const signature = mode + "\n" + source;
    diagramEntries = M.diagramEntries(data, mode);
    if (signature === diagramSource) { selectNode(selectedNode); return; }
    diagramSource = signature;
    const current = ++diagramGeneration;
    clearMotion();
    motionAvailable = false;
    syncMotion();
    selectedNode = null;
    selectNode(null);
    host.closest("figure").dataset.mode = mode;
    $("tb-diagram-export").hidden = mode !== "poster";
    $("tb-diagram-export").disabled = true;
    $("tb-diagram-caption").textContent = mode === "poster"
      ? "上段は成功時の主経路、下段は必要に応じた呼出しです。ノードを選ぶと詳しい契約を確認できます。"
      : mode === "execution"
      ? "共通の実行契約を示します。分岐は条件付きで、すべてが毎回実行されるわけではありません。"
      : mode === "tools" ? "Agent に登録された Tool を示します。線は使用可能な関係で、実行履歴ではありません。"
      : "Source・Webhook と Agent の接続を示します。";
    $("tb-diagram-source-text").textContent = source;
    host.setAttribute("aria-busy", "true");
    host.textContent = "図を描画中…";
    try {
      let svg;
      if (mode === "poster") svg = CatalogPoster.render(theme());
      else {
        const mermaid = await loadMermaid();
        mermaidInit(mermaid);
        ({svg} = await mermaid.render("tb-diagram-svg-" + current, source));
      }
      if (current !== diagramGeneration) return;
      host.innerHTML = `<div class="tb-diagram-wrap">${svg}</div>`;
      fitToWidth();
      applyZoom();
      bindDiagramNodes();
      $("tb-diagram-export").disabled = false;
      if (mode === "poster") selectNode(M.nodeId("step:agent"));
      if (mode === "execution") {
        selectNode(diagramEntries[0].id);
        const first = host.querySelector("[data-catalog-node]");
        if (first) host.scrollLeft = first.getBoundingClientRect().left - host.getBoundingClientRect().left + first.getBoundingClientRect().width / 2 - host.clientWidth / 2;
      }
      motionAvailable = true;
      syncMotion();
      try {
        const anime = await loadAnime();
        if (current !== diagramGeneration) return;
        attachMotion(anime);
        syncMotion();
      } catch (error) {
        if (current === diagramGeneration) {
          clearMotion();
          $("tb-diagram-motion-status").textContent = safeMessage(error) + "。連線の表示は利用できます。";
        }
      }
    } catch (error) {
      if (current !== diagramGeneration) return;
      diagramSource = "";
      $("tb-diagram-motion-status").textContent = "図を描画できませんでした。更新で再試行できます。";
      // 図の描画に失敗したときも mermaid ソース自体は確認できるようにする。
      host.innerHTML = `<p class="tb-note">図の描画に失敗しました(${esc(safeMessage(error))})。生成した mermaid ソース:</p><pre class="tb-diagram-source">${esc(source)}</pre>`;
    } finally {
      if (current === diagramGeneration) host.removeAttribute("aria-busy");
    }
  }

  async function exportPoster() {
    const button = $("tb-diagram-export");
    button.disabled = true;
    button.textContent = "画像を作成中…";
    // 選択枠・移動中の光点を含めず、同じ SVG から2倍解像度の静止画を作る。
    const svg = CatalogPoster.render(theme()).replace("</style>", ".poster-edge{animation:none!important;stroke-dashoffset:0}</style>");
    const sourceUrl = URL.createObjectURL(new Blob([svg], {type: "image/svg+xml;charset=utf-8"}));
    try {
      const img = new Image();
      img.src = sourceUrl;
      await img.decode();
      const canvas = document.createElement("canvas");
      canvas.width = 2560; canvas.height = 1300;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("画像の描画を開始できませんでした");
      context.drawImage(img, 0, 0, canvas.width, canvas.height);
      const png = await new Promise((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("PNG を作成できませんでした")), "image/png"));
      const downloadUrl = URL.createObjectURL(png);
      const link = document.createElement("a");
      link.href = downloadUrl; link.download = "trackeverything-architecture.png";
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
      $("tb-diagram-motion-status").textContent = "PNG の保存を開始しました";
    } catch (error) {
      $("tb-diagram-motion-status").textContent = "保存できませんでした: " + safeMessage(error);
    } finally {
      URL.revokeObjectURL(sourceUrl);
      button.disabled = false;
      button.textContent = "PNG を保存";
    }
  }

  // 図は横長になりがちなので、既定で枠幅に収まる縮小率を 1 回だけ決める。
  // 自動縮小は0.8まで。狭い画面は図の中だけを横スクロールする。
  function fitToWidth() {
    const wrap = document.querySelector(".tb-diagram-wrap");
    const scroll = $("tb-diagram-body");
    const svg = wrap && wrap.querySelector("svg");
    const natural = svg ? svg.getBoundingClientRect().width : 0;
    if (wrap && natural) wrap.dataset.naturalWidth = String(natural);
    if (!natural || !scroll.clientWidth) { fitScale = 1; scale = fitScale; return; }
    const style = window.getComputedStyle(scroll);
    const available = scroll.clientWidth - parseFloat(style.paddingLeft || "0") - parseFloat(style.paddingRight || "0") - 4;
    fitScale = Math.min(1, Math.max(0.8, available / natural));
    scale = fitScale;
  }

  function applyZoom() {
    const wrap = document.querySelector(".tb-diagram-wrap");
    const svg = wrap && wrap.querySelector("svg");
    // 縮尺は svg 自体の width で表す(viewBox が縦横比を保つ)。transform は
    // 配置幅を変えないため、scroll と見た目が食い違う原因になる。
    if (svg) {
      const natural = Number(wrap.dataset.naturalWidth) || 0;
      if (natural) { svg.style.width = natural * scale + "px"; svg.style.height = "auto"; }
    }
    $("tb-diagram-zoom-label").textContent = Math.round(scale * 100) + "%";
  }

  // ---- rows -----------------------------------------------------------------
  const chip = (name, kind = "", search = name) => `<button type="button" class="tb-chip${kind ? " " + kind : ""}" data-chip-search="${esc(search)}" title="「${esc(search)}」で絞り込む">${esc(name)}</button>`;
  const badge = (label, kind) => `<span class="tb-badge${kind ? " " + kind : ""}">${esc(label)}</span>`;
  const provenance = entry => `<code>${esc(entry.module)}.${esc(entry.cls)}</code>`;

  function badgesFor(agent) {
    const items = [];
    for (const key of agent.entrances) items.push(badge(key === agent.name ? "入口" : "入口:" + key, "entrance"));
    if (data.webhook_entrances.some(key => agent.entrances.includes(key))) items.push(badge("Webhook 受け", "webhook"));
    if (!agent.entrances.length && agent.parents.length) items.push(badge("子 Agent", "child"));
    if (M.unwired(agent)) items.push(badge("未接続", "unwired"));
    return items.join("");
  }

  function fieldRow(symbol, label, valueHtml) {
    return `<dt><span role="img" aria-label="${esc(label)}" title="${esc(label)}" data-tip="${esc(label)}">${icon(symbol, "", 16)}</span></dt><dd>${valueHtml || `<span class="tb-muted">なし</span>`}</dd>`;
  }

  function argumentsHtml(schema) {
    const summary = M.argumentSummary(schema);
    if (!summary.length) return `<span class="tb-muted">引数なし</span>`;
    return summary.map(entry => `<span class="tb-arg"><code>${esc(entry.name)}</code><span class="tb-muted">${esc(entry.type)}${entry.required ? " · 必須" : ""}</span></span>`).join("");
  }

  function agentRow(agent) {
    const open = expanded.has("agent:" + agent.name);
    const meta = [`Tool ${agent.tools.length}`, `子 ${agent.children.length}`, agent.pre.length + agent.post.length ? `Gate ${agent.pre.length + agent.post.length}` : ""].filter(Boolean).join(" · ");
    const gates = [
      agent.pre.length ? `<div class="tb-chip-group"><span class="tb-chip-label">Pre</span>${agent.pre.map(name => chip(name)).join("")}</div>` : "",
      agent.post.length ? `<div class="tb-chip-group"><span class="tb-chip-label">Post</span>${agent.post.map(name => chip(name)).join("")}</div>` : "",
    ].join("");
    return `<div class="tb-cat-row" data-kind="agent" data-name="${esc(agent.name)}">
      <button type="button" class="tb-cat-head" data-toggle="agent:${esc(agent.name)}" aria-expanded="${open}">
        <span class="tb-cat-title"><code>${esc(agent.name)}</code>${badgesFor(agent)}</span>
        <span class="tb-cat-desc">${esc(agent.description)}</span>
        <span class="tb-cat-meta">${esc(meta)}</span>
        <span class="tb-cat-chevron">${icon(open ? "down" : "right", "", 16)}</span>
      </button>
      ${open ? `<div class="tb-cat-body"><dl class="tb-fields">
        ${fieldRow("route", "子 Agent", agent.children.map(name => chip(name)).join(""))}
        ${fieldRow("catalog", "親 Agent", agent.parents.map(name => chip(name)).join(""))}
        ${fieldRow("braces", "Tool", agent.tools.map(name => chip(name)).join(""))}
        ${fieldRow("shield", "Harness", gates || "")}
        ${fieldRow("search", "引数", argumentsHtml(agent.arguments))}
        ${fieldRow("source", "実装", provenance(agent))}
      </dl><p class="tb-cat-desc-full">${esc(agent.description)}</p></div>` : ""}
    </div>`;
  }

  function toolRow(tool) {
    const open = expanded.has("tool:" + tool.name);
    const meta = tool.used_by.length ? tool.used_by.length + " Agent が使用" : "未接続";
    return `<div class="tb-cat-row" data-kind="tool" data-name="${esc(tool.name)}">
      <button type="button" class="tb-cat-head" data-toggle="tool:${esc(tool.name)}" aria-expanded="${open}">
        <span class="tb-cat-title"><code>${esc(tool.name)}</code>${tool.used_by.length ? "" : badge("未接続", "unwired")}</span>
        <span class="tb-cat-desc">${esc(tool.description)}</span>
        <span class="tb-cat-meta">${esc(meta)}</span>
        <span class="tb-cat-chevron">${icon(open ? "down" : "right", "", 16)}</span>
      </button>
      ${open ? `<div class="tb-cat-body"><dl class="tb-fields">
        ${fieldRow("route", "使用 Agent", tool.used_by.map(name => chip(name)).join(""))}
        ${fieldRow("search", "引数", argumentsHtml(tool.arguments))}
        ${fieldRow("source", "実装", provenance(tool))}
      </dl><p class="tb-cat-desc-full">${esc(tool.description)}</p></div>` : ""}
    </div>`;
  }

  function harnessRow(harness) {
    const open = expanded.has("harness:" + harness.name);
    return `<div class="tb-cat-row" data-kind="harness" data-name="${esc(harness.name)}">
      <button type="button" class="tb-cat-head" data-toggle="harness:${esc(harness.name)}" aria-expanded="${open}">
        <span class="tb-cat-title"><code>${esc(harness.name)}</code>${badge(harness.phase === "pre" ? "Pre gate" : "Post gate", "webhook")}</span>
        <span class="tb-cat-desc">${esc(harness.description)}</span>
        <span class="tb-cat-meta">${esc(harness.guards.length ? harness.guards.length + " Agent を gate" : "未接続")}</span>
        <span class="tb-cat-chevron">${icon(open ? "down" : "right", "", 16)}</span>
      </button>
      ${open ? `<div class="tb-cat-body"><dl class="tb-fields">
        ${fieldRow("route", "守護対象", harness.guards.map(name => chip(name)).join(""))}
        ${fieldRow("source", "実装", provenance(harness))}
      </dl></div>` : ""}
    </div>`;
  }

  function sourceRow(source) {
    const open = expanded.has("source:" + source.key);
    return `<div class="tb-cat-row" data-kind="source" data-name="${esc(source.key)}">
      <button type="button" class="tb-cat-head" data-toggle="source:${esc(source.key)}" aria-expanded="${open}">
        <span class="tb-cat-title"><code>${esc(source.key)}</code></span>
        <span class="tb-cat-desc">${esc(source.description)}</span>
        <span class="tb-cat-chevron">${icon(open ? "down" : "right", "", 16)}</span>
      </button>
      ${open ? `<div class="tb-cat-body"><dl class="tb-fields">
        ${fieldRow("source", "実装", provenance(source))}
      </dl><p class="tb-cat-desc-full">${esc(source.description)}</p></div>` : ""}
    </div>`;
  }

  // ---- filtering & render ---------------------------------------------------
  const agentVisible = agent => M.matches(agent, query) && (!unwiredOnly || M.unwired(agent));
  const toolVisible = tool => M.matches(tool, query) && (!unwiredOnly || !tool.used_by.length);

  function renderStatus() {
    if (loaded) return;
    $("tb-catalog-rows").innerHTML = `<div class="tb-empty">${loading ? "読み込み中…" : "カタログを取得できませんでした。接続設定を確認して「更新」で再試行してください。"}</div>`;
  }

  function sectionHtml(key, title, iconKey, rows, visible, note) {
    if (!sections.has(key)) return "";
    if (!rows.length && (query || unwiredOnly)) return "";
    return `<section class="tb-cat-section" data-section="${key}"${visible ? "" : " hidden"}>
      <h2>${icon(iconKey)}<span>${esc(title)}</span><span class="tb-cat-count">${rows.length}</span></h2>
      ${note || ""}
      <div class="tb-cat-rows">${rows.join("") || `<div class="tb-empty">該当なし</div>`}</div>
    </section>`;
  }

  function renderAll() {
    if (!loaded || !data) { renderStatus(); return; }
    $("tb-cat-error").hidden = true;
    const agents = data.agents.filter(agentVisible);
    const tools = data.tools.filter(toolVisible);
    const harnesses = data.harnesses.filter(entry => M.matches(entry, query) && (!unwiredOnly || !entry.guards.length));
    const sources = data.sources.filter(entry => M.matches(entry, query));
    const counts = [
      ["tb-cat-count-agents", "Agent " + data.agents.length],
      ["tb-cat-count-tools", "Tool " + data.tools.length],
      ["tb-cat-count-unwired", "未接続 " + (data.agents.filter(M.unwired).length + data.tools.filter(tool => !tool.used_by.length).length)],
    ];
    for (const [id, label] of counts) $(id).textContent = label;
    const harnessNote = !data.harnesses.length ? `<p class="tb-note">宣言済みの Harness はまだありません(pre / post gate は <code>agent/how-to-create.md</code> を参照)。</p>` : "";
    $("tb-catalog-rows").innerHTML =
      sectionHtml("agents", "Agent", "route", agents.map(agentRow), true) +
      sectionHtml("tools", "Tool", "braces", tools.map(toolRow), true) +
      sectionHtml("harnesses", "Harness", "shield", harnesses.map(harnessRow), true, harnessNote) +
      sectionHtml("sources", "Source", "source", sources.map(sourceRow), true);
    if (query && !agents.length && !tools.length && !harnesses.length && !sources.length)
      $("tb-catalog-rows").innerHTML = `<div class="tb-empty">一致する項目はありません。</div>`;
    for (const button of document.querySelectorAll("#tb-cat-sections [data-section-chip]"))
      button.setAttribute("aria-pressed", String(sections.has(button.dataset.sectionChip)));
    $("tb-cat-unwired").setAttribute("aria-pressed", String(unwiredOnly));
    for (const button of document.querySelectorAll("#tb-diagram-mode [data-mode]"))
      button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    renderDiagram();
  }

  // ---- view switching -------------------------------------------------------
  // hash → 画面名。usage・runs 画面の取得は usage.js・runs.js が自分の hashchange で行うため、
  // ここは表示切替だけを担う(app.js / usage.js / runs.js とは独立に動く)。
  // 実行フローは #/runs と #/runs/<ID> の両方を受け、既定の画面でもある。
  const viewFromHash = () => (location.hash === "#/catalog" ? "catalog" : location.hash === "#/usage" ? "usage" : "runs");
  function setView(next) {
    if (next === view) return;
    view = next;
    $("tb-catalog").hidden = view !== "catalog";
    $("tb-usage").hidden = view !== "usage";
    $("tb-runs").hidden = view !== "runs";
    $("tb-nav-catalog").setAttribute("aria-current", view === "catalog" ? "page" : "false");
    $("tb-nav-usage").setAttribute("aria-current", view === "usage" ? "page" : "false");
    $("tb-nav-runs").setAttribute("aria-current", view === "runs" ? "page" : "false");
    if (view === "catalog") ensureData();
    syncMotion();
  }

  // ---- events ---------------------------------------------------------------
  const catalogRoot = $("tb-catalog");
  $("tb-diagram-body").addEventListener("click", event => {
    const node = event.target.closest("[data-catalog-node]");
    if (node) selectNode(node.dataset.catalogNode);
  });
  $("tb-diagram-body").addEventListener("keydown", event => {
    const node = event.target.closest("[data-catalog-node]");
    if (node && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); selectNode(node.dataset.catalogNode); }
  });
  $("tb-diagram-play").addEventListener("click", () => { playing = !playing; syncMotion(); });
  $("tb-diagram-export").addEventListener("click", exportPoster);
  $("tb-diagram-speed").addEventListener("change", event => { speed = Number(event.target.value); syncMotion(); });
  const diagramObserver = new IntersectionObserver(entries => {
    diagramVisible = entries[0].isIntersecting;
    syncMotion();
  });
  diagramObserver.observe($("tb-diagram-body"));
  document.addEventListener("visibilitychange", syncMotion);
  reducedMotion.addEventListener("change", () => { playing = !reducedMotion.matches; syncMotion(); });
  catalogRoot.addEventListener("click", event => {
    const toggle = event.target.closest("[data-toggle]");
    if (toggle) {
      const key = toggle.dataset.toggle;
      expanded.has(key) ? expanded.delete(key) : expanded.add(key);
      renderAll();
      return;
    }
    const chipButton = event.target.closest("[data-chip-search]");
    if (chipButton) { query = chipButton.dataset.chipSearch; $("tb-cat-search").value = query; renderAll(); $("tb-cat-search").focus(); }
  });
  $("tb-cat-search").addEventListener("input", event => { query = event.target.value; renderAll(); });
  $("tb-cat-refresh").addEventListener("click", load);
  $("tb-cat-unwired").addEventListener("click", () => { unwiredOnly = !unwiredOnly; renderAll(); });
  for (const button of document.querySelectorAll("#tb-cat-sections [data-section-chip]"))
    button.addEventListener("click", () => {
      const key = button.dataset.sectionChip;
      sections.has(key) ? sections.delete(key) : sections.add(key);
      renderAll();
    });
  for (const button of document.querySelectorAll("#tb-diagram-mode [data-mode]"))
    button.addEventListener("click", () => { if (mode !== button.dataset.mode) { mode = button.dataset.mode; renderAll(); } });
  for (const [id, delta] of [["tb-diagram-zoom-in", 0.15], ["tb-diagram-zoom-out", -0.15]])
    $(id).addEventListener("click", () => { scale = Math.min(1.6, Math.max(0.4, Math.round((scale + delta) * 20) / 20)); applyZoom(); });
  $("tb-diagram-zoom-reset").addEventListener("click", () => { scale = fitScale; applyZoom(); });
  $("tb-nav-catalog").addEventListener("click", () => setView("catalog"));
  $("tb-nav-usage").addEventListener("click", () => setView("usage"));
  $("tb-nav-runs").addEventListener("click", () => setView("runs"));
  window.addEventListener("hashchange", () => setView(viewFromHash()));
  $("token").addEventListener("change", () => { if (view === "catalog") load(); });
  if (darkQuery && darkQuery.addEventListener) darkQuery.addEventListener("change", () => { if (view === "catalog") renderDiagram(); });

  // decorate icon placeholders
  for (const [id, key, tip] of [["tb-cat-icon", "catalog", ""], ["tb-cat-search-icon", "search", ""], ["tb-cat-refresh", "refresh", "更新"], ["tb-diagram-zoom-in", "plus", "拡大"], ["tb-diagram-zoom-out", "minus", "縮小"], ["tb-diagram-zoom-reset", "reset", "枠幅に合わせる"]])
    if ($(id)) { $(id).innerHTML = icon(key); if (tip) { $(id).title = tip; $(id).dataset.tip = tip; } }
  setView(viewFromHash());
})();
