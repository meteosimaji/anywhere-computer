"use strict";
(() => {
  const methods = ["progress", "start", "poll", "retry_save", "register", "cancel"];
  const element = id => document.getElementById(`enrollment-${id}`);
  let busy = false, snapshot = null;
  const phases = {new:"認証を開始できます",starting:"認証を開始しています",waiting:"ブラウザーでの認証を待っています",requesting:"認証結果を確認しています",grant_saved:"認証情報を保存しました。端末名を入力して登録してください",denied:"認証が拒否されました",expired:"認証コードの期限が切れました",cancelled:"認証を中止しました",failed:"認証に失敗しました",uncertain:"認証結果が未確認です",credential_error:"認証情報を保存できませんでした"};
  function controls() {
    const phase = snapshot?.authorization?.phase;
    const saved = snapshot?.registration;
    for (const method of methods) {
      const allowed = method === "progress"
        || method === "start" && phase === "new" && !saved
        || method === "poll" && phase === "waiting"
        || method === "retry_save" && phase === "credential_error"
        || method === "register" && !saved?.device && (phase === "grant_saved" || saved) && element("name").value.trim().length > 0
        || method === "cancel" && ["waiting","credential_error","uncertain"].includes(phase);
      element(method).disabled = busy || !allowed;
    }
    element("name").disabled = busy || !!saved;
  }
  function render(value) {
    if (value.schema_version !== 1 || !value.authorization || !(value.authorization.phase in phases)) throw new Error("Invalid enrollment snapshot");
    snapshot = value;
    const auth = value.authorization, saved = value.registration;
    element("state").textContent = saved?.device
      ? (saved.device.state === "registered" ? "端末登録を保存済み。現在の接続・登録の有効性は未確認です" : "端末登録は失効しています")
      : saved ? "登録要求を保存済み。結果を再確認するには同じ名前で登録してください" : phases[auth.phase];
    if (saved) element("name").value = saved.name;
    const showCode = auth.phase === "waiting" && !!auth.user_code && !!auth.verification_uri;
    element("code-area").hidden = !showCode;
    element("code").value = showCode ? auth.user_code : "";
    element("url").value = showCode ? auth.verification_uri : "";
  }
  async function invoke(method) {
    if (busy) return;
    busy = true; controls();
    element("result").textContent = "確認しています…";
    try {
      const args = {method, name: method === "register" ? element("name").value.trim() : null};
      const value = JSON.parse(await window.__TAURI__.core.invoke("management_enrollment", args));
      render(value);
      element("result").textContent = "";
    } catch (error) {
      // Do not imply the previous snapshot is current, or resend a mutation.
      snapshot = null;
      element("state").textContent = "現在の登録状態は未確認です";
      element("code-area").hidden = true;
      element("code").value = ""; element("url").value = "";
      element("result").textContent = typeof error === "string" ? error : "登録処理に接続できません。状態を確認してください。";
    } finally { busy = false; controls(); }
  }
  for (const method of methods) element(method).addEventListener("click", () => invoke(method));
  element("name").addEventListener("input", controls);
  controls();
})();
