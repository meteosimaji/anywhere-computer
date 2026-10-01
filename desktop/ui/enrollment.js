"use strict";
(() => {
  const methods = [
    "progress", "start", "open_page", "restart", "reauthorize", "poll", "retry_save", "register", "cancel", "cleanup",
  ];
  const element = id => document.getElementById(`enrollment-${id}`);
  let busy = false;
  let copying = false;
  let snapshot = null;
  const phases = {
    new: "認証を開始できます",
    starting: "認証を開始しています",
    waiting: "ブラウザーでの認証を待っています",
    requesting: "認証結果を確認しています",
    grant_saved: "認証情報を保存しました。端末名を入力して登録してください",
    denied: "認証が拒否されました",
    expired: "認証コードの期限が切れました",
    cancelled: "認証を中止しました",
    failed: "認証に失敗しました",
    uncertain: "認証結果が未確認です",
    credential_error: "認証情報を利用できません。期限や資格情報ストアの状態を確認してください",
  };

  function canInvoke(method, phase, saved) {
    switch (method) {
      case "progress": return true;
      case "cleanup": return snapshot?.can_cleanup === true;
      case "reauthorize": return snapshot?.can_reauthorize === true;
      case "start": return phase === "new" && !saved;
      case "restart": return ["denied", "expired", "cancelled", "failed"].includes(phase) && !saved;
      case "poll": return phase === "waiting";
      case "open_page": return phase === "waiting" && !!snapshot?.authorization.verification_uri;
      case "retry_save":
        return phase === "credential_error" && snapshot.authorization.can_retry_save === true;
      case "register":
        return !saved?.device && phase === "grant_saved" && element("name").value.trim().length > 0;
      case "cancel": return ["waiting", "credential_error"].includes(phase);
      default: return false;
    }
  }

  function controls() {
    const phase = snapshot?.authorization?.phase;
    const saved = snapshot?.registration;
    for (const method of methods) {
      element(method).disabled = busy || !canInvoke(method, phase, saved);
      // A blank name disables the current registration action without hiding it.
      // Busy controls remain in place while their original request is pending.
      element(method).hidden = method === "register"
        ? !!saved?.device || phase !== "grant_saved"
        : !canInvoke(method, phase, saved);
    }
    element("name").disabled = busy || !!saved;
    element("copy-code").disabled = busy || copying || phase !== "waiting"
      || !snapshot?.authorization.user_code;
  }

  function describeState(auth, saved) {
    if (saved?.device) {
      return saved.device.state === "registered"
        ? "端末登録を保存済み。現在の接続・登録の有効性は未確認です"
        : "端末登録は失効しています";
    }
    if (!saved) return phases[auth.phase];
    return auth.phase === "grant_saved"
      ? "登録要求を保存済み。結果を再確認するには同じ名前で登録してください"
      : `登録要求は保持されています。${phases[auth.phase]}`;
  }

  function render(value) {
    if (value.schema_version !== 1 || !value.authorization
        || !Object.hasOwn(phases, value.authorization.phase)) {
      throw new Error("Invalid enrollment snapshot");
    }
    snapshot = value;
    const auth = value.authorization;
    const saved = value.registration;
    element("state").textContent = describeState(auth, saved);
    element("recovery-help").hidden = auth.phase !== "credential_error";
    if (saved) element("name").value = saved.name;
    const showCode = auth.phase === "waiting" && !!auth.user_code && !!auth.verification_uri;
    element("code-area").hidden = !showCode;
    element("code").value = showCode ? auth.user_code : "";
    element("url").value = showCode ? auth.verification_uri : "";
  }
  async function invoke(method) {
    if (busy) return;
    busy = true;
    controls();
    element("result").textContent = "確認しています…";
    try {
      const args = {method, name: method === "register" ? element("name").value.trim() : null};
      const value = JSON.parse(await window.__TAURI__.core.invoke("management_enrollment", args));
      render(value);
      element("result").textContent = method === "open_page"
        ? "ブラウザーへ認証ページを開く要求を送りました。本人認証を終えたら、結果を確認してください。"
        : "";
    } catch (error) {
      // Do not imply the previous snapshot is current, or resend a mutation.
      snapshot = null;
      element("state").textContent = "現在の登録状態は未確認です";
      element("code-area").hidden = true;
      element("recovery-help").hidden = true;
      element("code").value = "";
      element("url").value = "";
      element("result").textContent = typeof error === "string"
        ? error : "登録処理に接続できません。状態を確認してください。";
    } finally {
      busy = false;
      controls();
    }
  }
  async function copyCode() {
    if (busy || copying || snapshot?.authorization.phase !== "waiting"
        || !snapshot.authorization.user_code) return;
    const selected = snapshot;
    copying = true;
    controls();
    try {
      await navigator.clipboard.writeText(selected.authorization.user_code);
      if (snapshot === selected) element("result").textContent = "コードをコピーしました。";
    } catch (_) {
      if (snapshot === selected) {
        element("code").focus();
        element("code").select();
        element("result").textContent = "コピーできませんでした。選択されたコードを手動でコピーしてください。";
      }
    } finally {
      copying = false;
      controls();
    }
  }
  for (const method of methods) element(method).addEventListener("click", () => invoke(method));
  element("copy-code").addEventListener("click", copyCode);
  element("name").addEventListener("input", controls);
  controls();
})();
