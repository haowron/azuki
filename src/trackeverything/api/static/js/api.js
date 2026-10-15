"use strict";
// 管理 API の契約を 1 か所に集める層。endpoint ごとに 1 関数で分類する。
// 認証 token の取得と 401 の通知は app.js が configure で注入する。
const DashboardApi = (() => {
  let getToken = () => "", onUnauthorized = () => {};
  function configure(options) {
    if (typeof options.getToken === "function") getToken = options.getToken;
    if (typeof options.onUnauthorized === "function") onUnauthorized = options.onUnauthorized;
  }
  async function request(path, method = "GET", body, signal) {
    const token = getToken();
    const headers = token ? {"X-Webhook-Token": token} : {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path,{method,signal,headers,body:body!==undefined?JSON.stringify(body):undefined});
    const parsed = await response.json().catch(()=>({}));
    if (!response.ok) {
      if(response.status===401)onUnauthorized();
      // status を残し、呼出し側が 404(未登録)とその他の失敗を区別できるようにする。
      const error=new Error(response.status===401?"401 — 接続設定の API トークンを確認してください。":typeof parsed.detail==="string"?parsed.detail:"HTTP "+response.status);
      error.status=response.status;throw error;
    }
    return parsed;
  }
  // カタログ(Agent / Tool / Harness / Source の稼働中グラフ)。
  const catalog = () => request("api/catalog","GET");
  // 実行統計(logs の agent / tool イベントの呼び出し回数集計)。
  const usage = () => request("api/usage","GET");
  // 実行フロー。ID は path の残り全体として送る(`/` や `:` を含む run / session ID)。
  const runs = (limit = 20) => request("api/runs?limit="+limit,"GET");
  const run = id => request("api/runs/"+encodeURIComponent(id),"GET");
  const session = id => request("api/sessions/"+encodeURIComponent(id),"GET");
  return {configure,request,catalog,usage,runs,run,session};
})();
if (typeof module !== "undefined") module.exports = DashboardApi;
