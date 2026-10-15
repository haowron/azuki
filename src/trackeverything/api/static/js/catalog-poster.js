"use strict";
// 実行契約の図解。固定座標の SVG はブラウザー表示と PNG 保存で共用する。
// 登録済み構成は catalog-model.js / API が所有し、ここでは複製しない。
const CatalogPoster = (() => {
  function render(theme = "light") {
    const M = CatalogModel, esc = M.escapeHtml, palette = M.THEMES[theme] || M.THEMES.light;
    const dark = theme === "dark";
    const bg = palette.inner.fill, text = palette.inner.color;
    const muted = dark ? "#abb5c5" : "#586373", line = palette.inner.stroke, accent = palette.entrance.stroke;
    const entries = new Map(M.diagramEntries({}, "execution").map(entry => [entry.key, entry]));
    const id = key => M.nodeId("step:" + key);
    const node = (key, x, y, width, title, subtitle, gate = false) => {
      const entry = entries.get(key), color = palette[entry.kind];
      const focus = key === "agent", fill = focus ? accent : color.fill;
      const ink = focus ? (dark ? "#15213b" : "#ffffff") : color.color;
      const stroke = entry.kind === "inner" ? muted : color.stroke;
      const shape = gate
        ? `<path class="poster-node-shape" d="M16 0H${width - 16}L${width} 43L${width - 16} 86H16L0 43Z"/>`
        : `<rect class="poster-node-shape" width="${width}" height="86" rx="10"/>`;
      return `<g class="node poster-node" id="flowchart-${id(key)}-poster" transform="translate(${x} ${y})" fill="${fill}" stroke="${stroke}" style="color:${ink}">
        ${shape}<text x="${width / 2}" y="35" text-anchor="middle" fill="${ink}" stroke="none" class="poster-node-title">${esc(title)}</text>
        <text x="${width / 2}" y="60" text-anchor="middle" fill="${ink}" stroke="none" class="poster-node-note">${esc(subtitle)}</text></g>`;
    };
    const edge = (from, to, d, both = false) => `<path class="flowchart-link poster-edge edge-animation-slow${both ? " poster-branch" : ""}" data-from="${id(from)}" data-to="${id(to)}" d="${d}" marker-end="url(#poster-arrow${both ? "-branch" : ""})"${both ? ' marker-start="url(#poster-arrow-branch)"' : ""}/>`;
    const main = [
      ["input", "入力を取得", "Source / Webhook", "内容 + identity"],
      ["invocation", "依頼を保存", "Queue", "Redis Streams"],
      ["execution", "実行を準備", "Runtime", "版 + Context"],
      ["pre", "事前に検証", "Pre Harness", "拒否なら停止"],
      ["agent", "判断して実行", "Agent", "_execute()"],
      ["post", "成功を検証", "Post Harness", "ok の結果を検証"],
      ["result", "結果を返す", "AgentResult", "status + response"],
    ];
    const arrows = ["投入", "読む", "開始", "通過", "ok", "承認"];
    return `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="650" viewBox="0 0 1280 650" role="group" aria-labelledby="poster-title poster-desc" class="tb-poster-svg">
      <title id="poster-title">入力から判断、結果まで。</title>
      <desc id="poster-desc">上段は成功時の主経路。Agent は必要に応じて LLM、Tool、子 Agent を呼び、結果を受け取ります。各ノードを選ぶと詳細が表示されます。</desc>
      <style>
        .tb-poster-svg {font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans JP",sans-serif;color:${text};background:${bg}}
        .tb-poster-svg text {fill:${text}}
        .tb-poster-svg .poster-title {font-size:34px;font-weight:600;letter-spacing:-.6px}
        .tb-poster-svg .poster-subtitle {font-size:16px;fill:${muted}}
        .tb-poster-svg .poster-stage {font-size:15px;font-weight:600}
        .tb-poster-svg .poster-number {fill:${accent}}
        .tb-poster-svg .poster-node-title {font-size:16px;font-weight:600;fill:inherit}
        .tb-poster-svg .poster-node-note {font-size:13px;fill:inherit}
        .tb-poster-svg .poster-node text {fill:currentColor}
        .tb-poster-svg .poster-node-shape {stroke-width:1.5}
        .tb-poster-svg .poster-edge {fill:none;stroke:${accent};stroke-width:2;stroke-dasharray:7 5;stroke-dashoffset:900;animation:poster-flow 50s linear infinite}
        .tb-poster-svg .poster-branch {stroke:${muted};stroke-width:1.5}
        .tb-poster-svg .poster-edge-label {font-size:13px;fill:${muted};paint-order:stroke;stroke:${bg};stroke-width:6;stroke-linejoin:round}
        .tb-poster-svg .poster-note-title {font-size:23px;font-weight:600}
        .tb-poster-svg .poster-note {font-size:15px;fill:${muted}}
        .tb-poster-svg .poster-code {font-size:17px;font-family:ui-monospace,monospace;fill:${accent}}
        .tb-poster-svg .poster-footer {font-size:13px;fill:${muted}}
        .tb-poster-svg .poster-edge-selected {stroke:${accent};stroke-width:3}
        .tb-poster-svg .tb-node-selected .poster-node-shape {stroke:${accent};stroke-width:3}
        @keyframes poster-flow {to {stroke-dashoffset:0}}
        @media(prefers-reduced-motion:reduce) {.tb-poster-svg .poster-edge {animation:none}}
      </style>
      <defs>
        <marker id="poster-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="${accent}"/></marker>
        <marker id="poster-arrow-branch" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="${muted}"/></marker>
      </defs>
      <rect width="1280" height="650" fill="${bg}"/>
      <text x="40" y="57" class="poster-title">入力から判断、結果まで。</text>
      <text x="40" y="91" class="poster-subtitle">Agent が制御を持ち、必要な処理を呼び出す。上段は成功時の主経路です。</text>
      <text x="1240" y="55" text-anchor="end" class="poster-footer">TrackEverything</text>
      <path d="M40 116H1240" stroke="${line}"/>
      ${main.map(([key, label], i) => `<text x="${40 + i * 176}" y="156" class="poster-stage"><tspan class="poster-number">${i + 1}.</tspan> ${label}</text>`).join("")}
      ${main.slice(0, -1).map(([key], i) => edge(key, main[i + 1][0], `M${184 + i * 176} 225H${216 + i * 176}`) + `<text x="${200 + i * 176}" y="210" text-anchor="middle" class="poster-edge-label">${arrows[i]}</text>`).join("")}
      ${edge("agent", "llm", "M790 268V318H624V416", true)}
      ${edge("agent", "tool", "M816 268V380H850V416", true)}
      ${edge("agent", "child", "M842 268V318H1076V416", true)}
      <text x="610" y="370" text-anchor="end" class="poster-edge-label">判断 / 応答</text>
      <text x="836" y="361" class="poster-edge-label">呼出し / 結果</text>
      <text x="1090" y="370" class="poster-edge-label">委譲 / 結果</text>
      ${main.map(([key, , title, subtitle], i) => node(key, 40 + i * 176, 182, 144, title, subtitle, key === "pre" || key === "post")).join("")}
      ${node("llm", 538, 416, 172, "LLM", "Call / FinalAnswer")}
      ${node("tool", 764, 416, 172, "Tool", "ToolResult を返す")}
      ${node("child", 990, 416, 172, "子 Agent", "AgentResult を返す")}
      <text x="40" y="361" class="poster-note-title">親子で共有するもの</text>
      <text x="40" y="405" class="poster-code">Context.data</text>
      <text x="40" y="436" class="poster-code">LLM step 予算</text>
      <text x="40" y="476" class="poster-note">子への委譲でも、予算は増えません。</text>
      <text x="40" y="502" class="poster-note">保存や通知は Tool が担います。</text>
      <path d="M40 548H1240" stroke="${line}"/>
      <text x="40" y="582" class="poster-note">Pre が拒否 → error</text>
      <text x="430" y="582" class="poster-note">Post が拒否 → error</text>
      <text x="820" y="582" class="poster-note">共有予算が尽きる → max_steps</text>
      <text x="40" y="621" class="poster-footer">共通契約の図解 · 詳細はノードを選択</text>
      <text x="1240" y="621" text-anchor="end" class="poster-footer">動きは方向の模式表示です。処理時間・実行履歴は表しません。</text>
    </svg>`;
  }
  return {render};
})();
if (typeof module !== "undefined") module.exports = CatalogPoster;
