(() => {
  "use strict";
  // 画面全体の共通部品。接続設定(API トークン)の保存・復元と api.js への注入、既定画面の選択を持つ。
  // 各画面の取得と描画は catalog.js・usage.js・runs.js、画面の切替は catalog.js が担う。
  const A=DashboardApi, $=id=>document.getElementById(id);
  const settingsIcon='<svg class="tb-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 3v7m0 4v7M12 3v3m0 4v11M20 3v11m0 4v3M1 10h6m2-4h6m2 8h6"/></svg>';
  function openSettings() {$("tb-settings").hidden=false;$("tb-settings-toggle").setAttribute("aria-expanded","true");}
  // 既定の画面は実行フロー。知らない hash(`#/` を含む)も実行フローへ送る。
  if (!/^#\/(catalog|usage|runs)(\/|$)/.test(location.hash||"")) location.hash="#/runs";
  $("tb-settings-toggle").setAttribute("aria-expanded","false");$("tb-settings-toggle").setAttribute("aria-controls","tb-settings");
  $("tb-settings-toggle").addEventListener("click",()=>{const open=$("tb-settings").hidden;$("tb-settings").hidden=!open;$("tb-settings-toggle").setAttribute("aria-expanded",String(open));});
  const toggle=$("tb-settings-toggle");toggle.innerHTML=settingsIcon;toggle.setAttribute("aria-label","接続設定");toggle.dataset.tip="接続設定";toggle.title="接続設定";
  A.configure({getToken:()=>$("token").value.trim(),onUnauthorized:openSettings});
  try{$("token").value=localStorage.getItem("te_token")||"";}catch{/* 現在の入力値で動作する。 */}
  // 各画面は自分の change listener で再取得する。ここは保存だけを行う。
  $("token").addEventListener("change",()=>{try{localStorage.setItem("te_token",$("token").value.trim());}catch{/* 現在の入力値を維持する。 */}});
})();
