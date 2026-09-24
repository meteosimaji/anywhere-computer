// Read a message through its own Copy action without replacing the OS clipboard.
// Callers serialize this with other browser interactions.
async function copySubchatUserText(document, conversationId, userId) {
  return copySubchatMessageText(document, conversationId, userId, 'user');
}

function subchatCopyTarget(document, userId, role) {
  const messages = [...document.querySelectorAll('main [data-message-author-role]')];
  if (messages.length) {
    const ids = messages.map(node => node.getAttribute('data-message-id'));
    if (ids.some(id => !id || id.trim() !== id) || new Set(ids).size !== ids.length)
      return null;
    const users = messages.filter(node =>
      node.getAttribute('data-message-author-role') === 'user' &&
      node.getAttribute('data-message-id') === userId);
    if (users.length !== 1) return null;
    let message = users[0];
    if (role === 'assistant') {
      const following = messages.slice(messages.indexOf(users[0]) + 1);
      const nextUser = following.findIndex(node =>
        node.getAttribute('data-message-author-role') === 'user');
      const answers = (nextUser < 0 ? following : following.slice(0, nextUser))
        .filter(node => node.getAttribute('data-message-author-role') === 'assistant');
      if (answers.length !== 1) return null;
      message = answers[0];
    }
    return {message, turn: message.closest('[data-turn-id-container]') || message};
  }
  const matches = [...document.querySelectorAll('main [data-turn-key]')]
    .filter(node => node.getAttribute('data-turn-key') === userId);
  if (matches.length !== 1 || matches[0].querySelector('[data-turn-key]')) return null;
  return {message: matches[0], turn: matches[0]};
}

async function copySubchatMessageText(document, conversationId, userId, role) {
  const unconfirmed = {state: 'message_unconfirmed'};
  if (document.location.href !== `https://chatgpt.com/c/${conversationId}` ||
      typeof userId !== 'string' || !userId || !['user', 'assistant'].includes(role))
    return unconfirmed;
  const target = subchatCopyTarget(document, userId, role);
  if (!target) return unconfirmed;
  const {turn} = target;
  if (role === 'user' &&
      turn.querySelectorAll('[data-user-message-bubble="true"]').length !== 1) return unconfirmed;
  let answerUnit = null;
  let controls = turn;
  const hasFinalControl = group => [...group.querySelectorAll('button')].some(button =>
    ['回答を再生成', 'Regenerate response'].includes(button.getAttribute('aria-label')) &&
      button.getClientRects().length && !button.disabled);
  if (role === 'assistant') {
    const answers = turn.querySelectorAll('[data-markdown-text-style="assistant-message"]');
    if (answers.length !== 1) return unconfirmed;
    answerUnit = answers[0].closest('[data-content-search-unit-key]');
    if (!answerUnit || !answerUnit.getAttribute('data-content-search-unit-key').endsWith(':assistant'))
      return unconfirmed;
    const groups = [...turn.querySelectorAll('.turn-action-controls')].filter(hasFinalControl);
    if (groups.length !== 1) return unconfirmed;
    controls = groups[0];
  }
  const answerReference = answerUnit?.getAttribute('data-content-search-unit-key');
  const generating = () => [...document.querySelectorAll('button')].some(button =>
    ['停止', 'Stop', 'Stop generating'].includes(button.getAttribute('aria-label')) &&
      button.getClientRects().length) || !!turn.querySelector('[aria-busy="true"]');
  if (role === 'assistant' && generating()) return unconfirmed;
  const labels = role === 'user' ? ['メッセージをコピーする', 'Copy message'] : ['コピーする', 'Copy'];
  const buttons = [...controls.querySelectorAll('button')].filter(button =>
    labels.includes(button.getAttribute('aria-label')));
  if (buttons.length !== 1 || buttons[0].disabled || !buttons[0].getClientRects().length)
    return unconfirmed;
  const clipboard = document.defaultView.navigator.clipboard;
  if (!clipboard) return unconfirmed;
  const original = Object.getOwnPropertyDescriptor(clipboard, 'writeText');
  const originalWrite = Object.getOwnPropertyDescriptor(clipboard, 'write');
  if ((original && !original.configurable) || (originalWrite && !originalWrite.configurable))
    return unconfirmed;
  const captured = [];
  let timeout;
  let completed;
  const written = new Promise(resolve => { completed = resolve; });
  try {
    Object.defineProperty(clipboard, 'writeText', {configurable: true,
      value: async text => { captured.push(text); completed(); }});
    Object.defineProperty(clipboard, 'write', {configurable: true,
      value: async items => {
        try {
          if (items.length !== 1 || !items[0].types.includes('text/plain')) return;
          captured.push(await (await items[0].getType('text/plain')).text());
        } finally { completed(); }
      }});
    buttons[0].click();
    await Promise.race([written, new Promise(resolve => {
      timeout = setTimeout(resolve, 1000);
    })]);
    const current = subchatCopyTarget(document, userId, role);
    if (!turn.isConnected || !current || current.turn !== turn ||
        current.message !== target.message ||
        !turn.contains(buttons[0]) ||
        document.location.href !== `https://chatgpt.com/c/${conversationId}` ||
        captured.length !== 1 || typeof captured[0] !== 'string') return unconfirmed;
    if (role === 'assistant') {
      if (generating() || !captured[0] || !hasFinalControl(controls) || !answerUnit.isConnected ||
          answerUnit.getAttribute('data-content-search-unit-key') !== answerReference)
        return unconfirmed;
      return {state: 'answer_text_observed', conversation_id: conversationId,
        user_message_id: userId, answer_reference: answerReference,
        completion_evidence: 'visible_response_controls', text: captured[0]};
    }
    return {state: 'user_text_observed', conversation_id: conversationId,
      user_message_id: userId, text: captured[0]};
  } finally {
    clearTimeout(timeout);
    if (original) Object.defineProperty(clipboard, 'writeText', original);
    else delete clipboard.writeText;
    if (originalWrite) Object.defineProperty(clipboard, 'write', originalWrite);
    else delete clipboard.write;
  }
}

// Exact original text, rather than a lossy reconstruction of rendered Markdown.
async function recoverSubchatSubmission(
    document, conversationId, prompt, previousIds, baselineIdentityKind = null) {
  const unconfirmed = {state: 'submission_unconfirmed'};
  if (typeof prompt !== 'string' || !prompt || !Array.isArray(previousIds) ||
      previousIds.some(id => typeof id !== 'string' || !id) ||
      new Set(previousIds).size !== previousIds.length) return unconfirmed;
  if (document.querySelectorAll('main').length !== 1) return unconfirmed;
  const hasMessageIds = !!document.querySelector('main [data-message-author-role]');
  // Saved pre-version baselines contain turn keys. They cannot establish which
  // current message IDs predate dispatch, even when the saved list is empty.
  if (baselineIdentityKind === 'empty') {
    if (previousIds.length) return unconfirmed;
  } else if (hasMessageIds ? baselineIdentityKind !== 'message_id' :
      ![null, 'legacy_turn_key'].includes(baselineIdentityKind)) return unconfirmed;
  const readMessages = () => {
    const messages = [...document.querySelectorAll('main [data-message-author-role]')];
    return (messages.length ? messages : [...document.querySelectorAll('main [data-turn-key]')])
      .map(node => ({id: node.getAttribute(messages.length ? 'data-message-id' : 'data-turn-key'),
        role: messages.length ? node.getAttribute('data-message-author-role') : 'user'}));
  };
  const snapshot = readMessages();
  const before = snapshot.map(message => message.id);
  if (before.some(id => !id || id.trim() !== id) ||
      new Set(before).size !== before.length) return unconfirmed;
  const pending = snapshot.filter(message =>
    message.role === 'user' && !previousIds.includes(message.id)).map(message => message.id);
  // Bound UI work; missing history never justifies a second submission.
  if (!pending.length || pending.length > 20) return unconfirmed;
  const matched = [];
  for (const id of pending) {
    const receipt = await copySubchatUserText(document, conversationId, id);
    if (receipt.state !== 'user_text_observed') return unconfirmed;
    if (receipt.text === prompt) matched.push(id);
  }
  if (matched.length !== 1 ||
      JSON.stringify(readMessages().map(message => message.id)) !== JSON.stringify(before))
    return unconfirmed;
  return {state: 'submission_observed', conversation_id: conversationId,
    user_message_id: matched[0]};
}
