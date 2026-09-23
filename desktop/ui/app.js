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
const engineActions = {ready:"",stopped:"起動ボタンで、このPCのエンジンを起動できます。",different_build:"稼働中の版と管理画面の版を確認してください。",stale_endpoint:"エンジンを起動してから、状態を更新してください。",unresponsive:"処理中の可能性があります。状態を再確認してください。",credential_unavailable:"このPCの認証情報を読み取れるか確認してください。"};
const resourceNames = {terminal_sessions:"端末セッション",plugin_sessions:"Pluginセッション",direct_mcp_sessions:"直接MCPセッション",searches:"検索",operations:"操作"};
const featureNames = {files:"ファイル",terminal:"端末",literal_search:"検索",office_text_read:"文書の読取",gui:"組込みGUI",skills:"Skills",audio_capture:"音声取得",gui_native:"macOS GUI",gui_mcp:"MCP GUI"};
const evidenceNames = {present:"実装あり",absent:"実装なし","true":"利用可能と報告","false":"非対応と報告",unknown:"未確認",not_observed:"未観測",not_checked:"未確認",not_verified:"未検証",authenticated_status_only:"状態照会の認証のみ",authorized:"認可済み",not_granted:"未認可",partial_grant:"一部のみ認可",not_required:"不要",verified_available:"署名済みhelperあり",unavailable:"helperなし",unsupported_platform:"非対応OS",verification_failed:"検証失敗"};
function render(snapshot) {
  field("engine",engineNames[snapshot.engine_state] ?? `要確認：${snapshot.engine_state}`);
  field("action",engineActions[snapshot.engine_state] ?? "接続の診断が必要です。");
  const configuration = snapshot.setup.configuration;
  field("connection",snapshot.setup.phase === "configured" ? `設定済み：${configuration.client}（接続は未確認）` : snapshot.setup.phase === "new" ? "接続設定はまだありません" : `設定の確認が必要です：${snapshot.setup.phase}`);
  const capabilityRows = Object.entries(snapshot.capability_diagnostics ?? {}).filter(([k]) => k in featureNames).map(([key, raw]) => {
    const value = raw && typeof raw === "object" ? raw : {};
    const fields = [["実装",value.running_implementation],["稼働版",value.runtime_available],["接続認可",value.connection_authorization],["helper",value.helper],["OS権限",value.os_permission],["受入",value.acceptance],["次の操作（helper/権限）",value.next_action],["認可の次の操作",value.authorization_next_action]];
    return [featureNames[key],fields.filter(([,item]) => item !== undefined).map(([label,item]) => `${label}：${evidenceNames[String(item)] ?? String(item)}`).join(" / ")];
  });
  if (capabilityRows.length) pairs("features",capabilityRows);
  else pairs("features",Object.entries(snapshot.capabilities).filter(([k]) => k in featureNames).map(([k,v]) => [featureNames[k],v?"エンジンが利用可能と報告":"未対応"]));
  const blockerRows = (snapshot.update_blocker_details ?? []).filter((item) => item && typeof item === "object").map((item) => [
    `更新を阻む：${resourceNames[item.resource] ?? item.resource ?? "セッション"}`,
    `${item.id ?? "ID未確認"}${item.session_id ? ` / session ${item.session_id}` : ""} / 状態 ${item.state ?? "未確認"}${item.reason ? ` / 理由 ${item.reason}` : ""} / ${item.stop_tool ? `${item.stop_tool}：停止${item.stop_available === true ? "可能" : "不可"}${item.stop_arguments ? ` ${JSON.stringify(item.stop_arguments)}` : ""}` : item.inspect_tool ? `${item.inspect_tool}で状態照会可能（停止操作なし）` : "停止操作未確認"}`,
  ]);
  pairs("work",[...Object.entries(snapshot.active_resources).map(([k,v]) => [resourceNames[k] ?? k,v]),...blockerRows]);
  if (!Object.keys(snapshot.active_resources).length) pairs("work",[["処理状態","未確認"]]);
  field("updates",snapshot.automatic_stable_updates ? "stableの自動更新：有効" : "手動更新（自動更新は無効）");
  field("observed",`確認時刻：${new Date(snapshot.observed_at).toLocaleString()}`);
  pairs("identity",[["管理画面側バージョン",snapshot.source_build?.version],["管理画面側 runtime ID",snapshot.source_build?.runtime_id],["稼働版バージョン",snapshot.version],["稼働版 runtime ID",snapshot.runtime_id],["version一致",snapshot.runtime_comparison?.version_matches === true ? "はい" : snapshot.runtime_comparison?.version_matches === false ? "いいえ" : "未確認"],["runtime ID一致",snapshot.runtime_comparison?.runtime_id_matches === true ? "はい" : snapshot.runtime_comparison?.runtime_id_matches === false ? "いいえ" : "未確認"],["instance ID",snapshot.instance_id]]);
  const devices = document.getElementById("devices"); devices.replaceChildren();
  if (snapshot.device_registry_state === "unavailable") devices.textContent = "登録情報を読み取れません。保存データは変更していません。";
  else if (!snapshot.devices.length) devices.textContent = "追加の端末は登録されていません。";
  for (const device of snapshot.devices) {
    const row = document.createElement("p"); row.className = "device";
    row.textContent = `${device.name} · 現在の接続は未確認。前回：${device.last_observed_state} / ${device.checked_at === null ? "観測なし" : new Date(device.checked_at * 1000).toLocaleString()}`;
    const check = document.createElement("button");
    check.type = "button"; check.textContent = "接続を確認";
    const observation = document.createElement("span");
    check.addEventListener("click", async () => {
      check.disabled = true; observation.textContent = " 接続を確認しています…";
      try {
        const result = JSON.parse(await window.__TAURI__.core.invoke("management_device_check", {deviceId: device.device_id}));
        if (result.schema_version !== 1 || result.device_id !== device.device_id) throw new Error("Invalid observation");
        const states = {ready: result.evidence === "authorized_catalog" ? "認証済みツール一覧を取得できました。実操作は未確認です。" : "端末エンジンの応答を確認しました。",unreachable:"接続できません。端末と接続先を確認してください。",not_ready:"接続の準備が整っていません。",authorization_required:"接続の認証が必要です。",credential_unavailable:"保存済みの認証情報を利用できません。"};
        observation.textContent = ` ${states[result.state] ?? "接続結果は未確認です。"}（${new Date(result.observed_at).toLocaleString()}）`;
      } catch (_) { observation.textContent = " 接続結果を確認できませんでした。自動再試行はしていません。"; }
      finally { check.disabled = false; }
    });
    row.append(document.createElement("br"), check, observation);
    devices.append(row);
  }
}
async function refresh(start = false) {
  const button = document.getElementById("refresh"), startButton = document.getElementById("start");
  button.disabled = true; startButton.disabled = true;
  field("status",start ? "エンジンの起動を確認しています…" : "状態を確認しています…");
  try {
    if (!window.__TAURI__?.core?.invoke) throw new Error("管理アプリから開いてください。このブラウザーではPCへ接続していません。");
    const result = JSON.parse(await window.__TAURI__.core.invoke(start ? "management_start" : "management_snapshot"));
    const snapshot = start ? result.snapshot : result;
    if (snapshot.schema_version !== 1) throw new Error("状態情報の版に対応していません。");
    render(snapshot);
    startButton.disabled = snapshot.engine_state !== "stopped";
    field("status",start ? (result.state === "ready" ? "このPCのエンジンの応答を確認しました。AIからの接続は未確認です。" : "起動結果を確認できませんでした。表示された診断を確認してください。") : "状態を更新しました。登録端末への接続試験は行っていません。");
  } catch (error) { field("status",`更新できませんでした。表示が残っている場合は前回の確認結果です。${String(error)}`); }
  finally { button.disabled = false; }
}
document.getElementById("refresh").addEventListener("click",() => refresh());
document.getElementById("start").addEventListener("click",() => refresh(true));
refresh();

let startupBusy = false;
function renderStartup(value) {
  const remote = value.mode === "remote";
  const names = {not_installed:"自動起動は未登録です",registered:"自動起動は登録されています",not_enabled:"登録されていますが無効です",unavailable:"自動起動の状態を確認できません"};
  field("startup-state", remote ? "既存のリモート接続用の自動起動です。変更にはCLIを使用してください。" : (names[value.state] ?? "未確認"));
  field("startup-observed", `確認時刻：${new Date(value.observed_at).toLocaleString()}。エンジンの接続状態は「このPC」で確認してください。`);
  document.getElementById("startup-enable").disabled = startupBusy || remote || !["not_installed","not_enabled"].includes(value.state);
  document.getElementById("startup-disable").disabled = startupBusy || remote || !["registered","not_enabled"].includes(value.state);
}
async function refreshStartup(action = "status") {
  if (startupBusy) return;
  startupBusy = true;
  document.getElementById("startup-enable").disabled = true;
  document.getElementById("startup-disable").disabled = true;
  field("startup-result", action === "status" ? "登録状態を確認しています…" : "登録の変更を確認しています…");
  try {
    const commands = {status:"management_startup_status",enable:"management_startup_enable",disable:"management_startup_disable"};
    const result = JSON.parse(await window.__TAURI__.core.invoke(commands[action]));
    const value = action === "status" ? result : result.startup;
    if (result.schema_version !== 1 || value.schema_version !== 1) throw new Error("対応していない状態情報です。");
    startupBusy = false;
    renderStartup(value);
    field("startup-result", action === "status" ? "" : result.state === "confirmed" ? "登録状態の変更を確認しました。" : "変更結果を確認できません。状態を更新し、診断を確認してください。");
  } catch (_) {
    field("startup-state", "現在の登録状態は未確認です");
    field("startup-result", "結果を確認できませんでした。自動で再実行はしません。状態を更新してください。");
  } finally { startupBusy = false; }
}
document.getElementById("startup-enable").addEventListener("click", () => refreshStartup("enable"));
document.getElementById("startup-disable").addEventListener("click", () => refreshStartup("disable"));
document.getElementById("refresh").addEventListener("click", () => refreshStartup());
refreshStartup();
