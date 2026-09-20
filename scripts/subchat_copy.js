// Read a message through its own Copy action without replacing the OS clipboard.
// Callers serialize this with other browser interactions.
async function copySubchatUserText(document, conversationId, userId) {
  const unconfirmed = {state: 'message_unconfirmed'};
  if (document.location.href !== `https://chatgpt.com/c/${conversationId}` ||
      typeof userId !== 'string' || !userId) return unconfirmed;
  const matches = [...document.querySelectorAll('main [data-turn-key]')]
    .filter(node => node.getAttribute('data-turn-key') === userId);
  if (matches.length !== 1) return unconfirmed;
  const turn = matches[0];
  if (turn.querySelector('[data-turn-key]') ||
      turn.querySelectorAll('[data-user-message-bubble="true"]').length !== 1)
    return unconfirmed;
  const buttons = [...turn.querySelectorAll('button')].filter(button =>
    ['メッセージをコピーする', 'Copy message'].includes(button.getAttribute('aria-label')));
  if (buttons.length !== 1 || buttons[0].disabled || !buttons[0].getClientRects().length)
    return unconfirmed;
  const clipboard = document.defaultView.navigator.clipboard;
  if (!clipboard) return unconfirmed;
  const original = Object.getOwnPropertyDescriptor(clipboard, 'writeText');
  if (original && !original.configurable) return unconfirmed;
  const captured = [];
  let timeout;
  let completed;
  const written = new Promise(resolve => { completed = resolve; });
  try {
    Object.defineProperty(clipboard, 'writeText', {configurable: true,
      value: async text => { captured.push(text); completed(); }});
    buttons[0].click();
    await Promise.race([written, new Promise(resolve => {
      timeout = setTimeout(resolve, 1000);
    })]);
    if (!turn.isConnected || turn.getAttribute('data-turn-key') !== userId ||
        !turn.contains(buttons[0]) ||
        document.location.href !== `https://chatgpt.com/c/${conversationId}` ||
        captured.length !== 1 || typeof captured[0] !== 'string') return unconfirmed;
    return {state: 'user_text_observed', conversation_id: conversationId,
      user_message_id: userId, text: captured[0]};
  } finally {
    clearTimeout(timeout);
    if (original) Object.defineProperty(clipboard, 'writeText', original);
    else delete clipboard.writeText;
  }
}

// Exact original text, rather than a lossy reconstruction of rendered Markdown.
async function recoverSubchatSubmission(document, conversationId, prompt, previousIds) {
  const unconfirmed = {state: 'submission_unconfirmed'};
  if (typeof prompt !== 'string' || !prompt || !Array.isArray(previousIds) ||
      previousIds.some(id => typeof id !== 'string' || !id) ||
      new Set(previousIds).size !== previousIds.length) return unconfirmed;
  if (document.querySelectorAll('main').length !== 1) return unconfirmed;
  const readIds = () => [...document.querySelectorAll('main [data-turn-key]')]
    .map(node => node.getAttribute('data-turn-key'));
  const before = readIds();
  if (before.some(id => !id || id.trim() !== id) ||
      new Set(before).size !== before.length) return unconfirmed;
  const pending = before.filter(id => !previousIds.includes(id));
  // Bound UI work; missing history never justifies a second submission.
  if (!pending.length || pending.length > 20) return unconfirmed;
  const matched = [];
  for (const id of pending) {
    const receipt = await copySubchatUserText(document, conversationId, id);
    if (receipt.state !== 'user_text_observed') return unconfirmed;
    if (receipt.text === prompt) matched.push(id);
  }
  if (matched.length !== 1 || JSON.stringify(readIds()) !== JSON.stringify(before))
    return unconfirmed;
  return {state: 'submission_observed', conversation_id: conversationId,
    user_message_id: matched[0]};
}
