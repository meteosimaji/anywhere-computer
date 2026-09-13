"use strict";
const field = (id, value) => { document.getElementById(id).textContent = value; };
function pairs(id, entries) {
  const root = document.getElementById(id);
  root.replaceChildren();
  for (const [key, value] of entries) {
    const term = document.createElement("dt"), detail = document.createElement("dd");
    term.textContent = key; detail.textContent = String(value ?? "未確認");
    root.append(term, detail);
  }
}
const engineNames = {ready:"エンジンが応答しています",stopped:"エンジンは停止しています",different_build:"別のビルドが稼働しています",stale_endpoint:"記録されたエンジンは終了しています",unresponsive:"エンジンの応答を確認できません",credential_unavailable:"認証情報を利用できません"};
const engineActions = {ready:"",stopped:"このプレビューでは起動操作をまだ提供していません。",different_build:"稼働中の版と管理画面の版を確認してください。",stale_endpoint:"エンジンを起動してから、状態を更新してください。",unresponsive:"処理中の可能性があります。状態を再確認してください。",credential_unavailable:"このPCの認証情報を読み取れるか確認してください。"};
const resourceNames = {terminal_sessions:"端末セッション",plugin_sessions:"Pluginセッション",direct_mcp_sessions:"直接MCPセッション",searches:"検索",operations:"操作"};
const featureNames = {files:"ファイル",terminal:"端末",literal_search:"検索",office_text_read:"文書の読取",gui:"組込みGUI"};
function render(snapshot) {
  field("engine",engineNames[snapshot.engine_state] ?? `要確認：${snapshot.engine_state}`);
  field("action",engineActions[snapshot.engine_state] ?? "接続の診断が必要です。");
  const configuration = snapshot.setup.configuration;
  field("connection",snapshot.setup.phase === "configured" ? `設定済み：${configuration.client}（接続は未確認）` : snapshot.setup.phase === "new" ? "接続設定はまだありません" : `設定の確認が必要です：${snapshot.setup.phase}`);
  pairs("features",Object.entries(snapshot.capabilities).filter(([k]) => k in featureNames).map(([k,v]) => [featureNames[k],v?"エンジンが利用可能と報告":"未対応"]));
  pairs("work",Object.entries(snapshot.active_resources).map(([k,v]) => [resourceNames[k] ?? k,v]));
  if (!Object.keys(snapshot.active_resources).length) pairs("work",[["処理状態","未確認"]]);
  field("updates",snapshot.automatic_stable_updates ? "stableの自動更新：有効" : "手動更新（自動更新は無効）");
  field("observed",`確認時刻：${new Date(snapshot.observed_at).toLocaleString()}`);
  pairs("identity",[["バージョン",snapshot.version],["runtime ID",snapshot.runtime_id],["instance ID",snapshot.instance_id]]);
  const devices = document.getElementById("devices"); devices.replaceChildren();
  if (snapshot.device_registry_state === "unavailable") devices.textContent = "登録情報を読み取れません。保存データは変更していません。";
  else if (!snapshot.devices.length) devices.textContent = "追加の端末は登録されていません。";
  for (const device of snapshot.devices) {
    const row = document.createElement("p"); row.className = "device";
    row.textContent = `${device.name} · 現在の接続は未確認。前回：${device.last_observed_state} / ${device.checked_at === null ? "観測なし" : new Date(device.checked_at * 1000).toLocaleString()}`;
    devices.append(row);
  }
}
async function refresh() {
  const button = document.getElementById("refresh"); button.disabled = true;
  field("status","状態を確認しています…");
  try {
    if (!window.__TAURI__?.core?.invoke) throw new Error("管理アプリから開いてください。このブラウザーではPCへ接続していません。");
    const snapshot = JSON.parse(await window.__TAURI__.core.invoke("management_snapshot"));
    if (snapshot.schema_version !== 1) throw new Error("状態情報の版に対応していません。");
    render(snapshot); field("status","状態を更新しました。登録端末への接続試験は行っていません。");
  } catch (error) { field("status",`更新できませんでした。表示が残っている場合は前回の確認結果です。${String(error)}`); }
  finally { button.disabled = false; }
}
document.getElementById("refresh").addEventListener("click",refresh);
refresh();
