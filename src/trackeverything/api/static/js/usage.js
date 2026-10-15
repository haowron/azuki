(() => {
  "use strict";
  // 実行統計画面の制御。/api/usage(logs の集計)と /api/catalog(登録一覧)を
  // 取得して UsageModel で合わせ、Agent・Tool ごとの呼び出し回数を並べる。
  // 画面切替(hash)は catalog.js が持ち、ここは #/usage への到着と token 変更で
  // 取得する。app.js / catalog.js とは独立に動く。
  const U = UsageModel, A = DashboardApi, $ = id => document.getElementById(id), esc = U.escapeHtml;
  const paths = new Map(Object.entries({
    chart:'<path d="M4 20V10M10 20V4M16 20v-6M22 20H2"/>',
    route:'<circle cx="5" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 5h9a4 4 0 0 1 0 8H8a3 3 0 0 0 0 6h9"/>',
    braces:'<path d="M8 3H6v6l-3 3 3 3v6h2M16 3h2v6l3 3-3 3v6h-2"/>',
    search:'<circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/>',
    refresh:'<path d="M20 10a8 8 0 1 0 0 5M20 3v7h-7"/>',
  }));
  const icon = (key, cls = "", size = 18) => `<svg class="tb-icon${cls ? " " + cls : ""}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths.get(key) || paths.get("chart")}</svg>`;

  // ---- state ----------------------------------------------------------------
  let data = null, loaded = false, loading = false, query = "";
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const fmt = new Intl.DateTimeFormat("ja-JP", {dateStyle: "short", timeStyle: "short", timeZone: zone});
  const time = value => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : fmt.format(date) + " · " + zone;
  };
  const dateOnly = value => {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("ja-JP", {dateStyle: "medium", timeZone: zone}).format(date);
  };

  // ---- data -----------------------------------------------------------------
  function safeMessage(error) { return error instanceof Error ? error.message : String(error); }

  async function load() {
    if (loading) return;
    loading = true;
    $("tb-usage-refresh").disabled = true;
    try {
      // 登録一覧は統計の補助情報。失敗しても集計だけは表示する。
      const [usage, catalog] = await Promise.all([A.usage(), A.catalog().catch(() => null)]);
      data = {
        agents: U.mergeRows(catalog && catalog.agents, usage.agents),
        tools: U.mergeRows(catalog && catalog.tools, usage.tools),
        agents_since: usage.agents_since ?? null,
        tools_since: usage.tools_since ?? null,
      };
      loaded = true;
      $("tb-usage-error").hidden = true;
      $("tb-usage-update-time").textContent = "最終更新 " + new Intl.DateTimeFormat("ja-JP", {timeStyle: "medium"}).format(new Date()) + " · " + zone;
    } catch (error) {
      // 401 のときは api.js の onUnauthorized が接続設定を開いてくれる。
      $("tb-usage-error").textContent = "統計を取得できませんでした: " + safeMessage(error);
      $("tb-usage-error").hidden = false;
    } finally {
      loading = false;
      $("tb-usage-refresh").disabled = false;
      render();
    }
  }

  // ---- render ---------------------------------------------------------------
  const total = rows => rows.reduce((sum, row) => sum + row.total, 0);

  function rowHtml(row) {
    const status = U.statusLine(row.by_status) || (row.total ? "" : "未実行");
    return `<tr>
      <th scope="row"><span class="tb-usage-name"><code>${esc(row.name)}</code>${row.registered === false ? '<span class="tb-badge unwired">現在未登録</span>' : ""}${row.description ? `<span class="tb-usage-desc">${esc(row.description)}</span>` : ""}</span></th>
      <td class="tb-usage-total">${row.total}</td>
      <td class="tb-usage-status">${esc(status)}</td>
      <td class="tb-usage-last">${esc(time(row.last_used_at))}</td>
    </tr>`;
  }

  function tableHtml(rows, iconKey, title) {
    const shown = rows.filter(row => U.matches(row, query));
    return `<section class="tb-cat-section"><h2>${icon(iconKey)}<span>${esc(title)}</span><span class="tb-cat-count">${shown.length}</span></h2>
      <div class="tb-cat-rows">${shown.length ? `<table class="tb-usage-table">
        <thead><tr><th>名前</th><th class="tb-usage-num">呼び出し</th><th>状態内訳</th><th>最終実行</th></tr></thead>
        <tbody>${shown.map(rowHtml).join("")}</tbody>
      </table>` : `<div class="tb-empty">該当なし</div>`}</div></section>`;
  }

  function scopeNote() {
    const agents = dateOnly(data.agents_since), tools = dateOnly(data.tools_since);
    const parts = [];
    parts.push(agents ? `Agent の集計範囲は ${agents} 以降` : "Agent の呼び出し記録はまだありません");
    parts.push(tools ? `Tool の記録は ${tools} 以降の実行から` : "Tool の呼び出し記録はまだありません(記録開始以降の実行のみを数えます)");
    return parts.join("。") + "。";
  }

  function render() {
    if (!loaded || !data) {
      $("tb-usage-agent-rows").innerHTML = `<div class="tb-empty">${loading ? "読み込み中…" : "統計を取得できませんでした。接続設定を確認して「更新」で再試行してください。"}</div>`;
      $("tb-usage-tool-rows").innerHTML = "";
      return;
    }
    $("tb-usage-agent-rows").innerHTML = tableHtml(data.agents, "route", "Agent");
    $("tb-usage-tool-rows").innerHTML = tableHtml(data.tools, "braces", "Tool");
    $("tb-usage-count-agents").textContent = "Agent 呼び出し 総計 " + total(data.agents).toLocaleString();
    $("tb-usage-count-tools").textContent = "Tool 呼び出し 総計 " + total(data.tools).toLocaleString();
    $("tb-usage-scope").textContent = scopeNote();
  }

  // ---- events ---------------------------------------------------------------
  $("tb-usage-refresh").addEventListener("click", load);
  $("tb-usage-search").addEventListener("input", event => { query = event.target.value; render(); });
  $("token").addEventListener("change", () => { if (location.hash === "#/usage") load(); });
  window.addEventListener("hashchange", () => { if (location.hash === "#/usage" && !loaded && !loading) load(); });

  // decorate icon placeholders
  for (const [id, key, tip] of [["tb-usage-icon", "chart", ""], ["tb-usage-search-icon", "search", ""], ["tb-usage-refresh", "refresh", "更新"]])
    if ($(id)) { $(id).innerHTML = icon(key); if (tip) { $(id).title = tip; $(id).dataset.tip = tip; } }
  if (location.hash === "#/usage") load();
})();
