"use strict";
// 実行統計画面(#/usage)の純粋ロジック。/api/usage の集計(logs の agent / tool
// イベント)と /api/catalog の登録一覧を名前で突き合わせ、未実行の登録成分も
// 0 回として並べる。DOM も fetch も持たない(Node の標準ライブラリーだけで検証する)。
const UsageModel = (() => {
  function escapeHtml(value) {
    return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  // registered は /api/catalog の登録一覧(agents / tools)、counts は /api/usage の
  // 集計行。registered === null のときは登録情報なしと扱い、集計だけを並べる
  // (registered フラグは null になり、画面は「現在未登録」を出さない)。
  // 並びは呼び出し回数の降順、同数は名前順。
  function mergeRows(registered, counts) {
    const byName = new Map();
    for (const entry of Array.isArray(registered) ? registered : []) {
      if (!entry || typeof entry.name !== "string") continue;
      byName.set(entry.name, {name: entry.name, description: String(entry.description ?? ""), registered: true, total: 0, by_status: {}, last_used_at: null});
    }
    for (const entry of Array.isArray(counts) ? counts : []) {
      if (!entry || typeof entry.name !== "string") continue;
      // 登録一覧があるのに載っていない名前は false(現在未登録)、一覧自体が
      // 取れないときは null(判定できないので画面は何も出さない)。
      const fallback = Array.isArray(registered) ? false : null;
      const row = byName.get(entry.name) ?? {name: entry.name, description: "", registered: fallback, total: 0, by_status: {}, last_used_at: null};
      row.total = Number(entry.total) || 0;
      row.by_status = entry.by_status && typeof entry.by_status === "object" ? {...entry.by_status} : {};
      row.last_used_at = typeof entry.last_used_at === "string" && entry.last_used_at ? entry.last_used_at : null;
      byName.set(entry.name, row);
    }
    return [...byName.values()].sort((a, b) => b.total - a.total || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  }

  function matches(row, query) {
    const q = String(query ?? "").trim().toLowerCase();
    if (!q) return true;
    return [row.name, row.description].some(field => String(field ?? "").toLowerCase().includes(q));
  }

  // 状態内訳の表示列。ok を先頭に固定し、残りは件数の多い順、同数は名前順。
  function statusLine(by_status) {
    const entries = Object.entries(by_status ?? {}).filter(([, count]) => Number(count) > 0);
    entries.sort(([aName, aCount], [bName, bCount]) =>
      aName === "ok" ? -1 : bName === "ok" ? 1 : (bCount - aCount) || (aName < bName ? -1 : aName > bName ? 1 : 0));
    return entries.map(([status, count]) => status + " " + count).join(" · ");
  }

  return {escapeHtml, mergeRows, matches, statusLine};
})();
if (typeof module !== "undefined") module.exports = UsageModel;
