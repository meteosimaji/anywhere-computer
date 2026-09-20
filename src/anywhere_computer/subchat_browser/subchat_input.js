// Current ordinary-Chat editor contract. Insertion itself never sends.
function insertSubchatDraft(document, text) {
  if (typeof text !== 'string' || text.length === 0 || text.includes('\r')) {
    return {state: 'invalid_input', input_dispatched: false};
  }
  const editors = document.querySelectorAll(
    'form[data-chatgpt-composer] [data-composer-markdown][role="textbox"]');
  if (editors.length !== 1) return {state: 'editor_unconfirmed', input_dispatched: false};
  const editor = editors[0];
  if (!editor.isContentEditable || !editor.getClientRects().length ||
      editor.closest('[inert], [aria-hidden="true"]') ||
      editor.innerText.trim() !== '') {
    return {state: 'editor_unconfirmed', input_dispatched: false};
  }
  if (document.activeElement !== editor) {
    return {state: 'focus_unconfirmed', input_dispatched: false};
  }
  const selection = document.getSelection();
  if (!selection || !selection.isCollapsed || !editor.contains(selection.anchorNode)) {
    return {state: 'selection_unconfirmed', input_dispatched: false};
  }
  const literal = document.createElement('span');
  literal.setAttribute('data-prompt-literal-paste', '');
  const lines = text.split('\n');
  for (let index = 0; index < lines.length; index++) {
    if (index) literal.appendChild(document.createElement('br'));
    literal.appendChild(document.createTextNode(lines[index]));
  }
  const accepted = document.execCommand('insertHTML', false, literal.outerHTML);
  // A rejected or changed edit must be inspected, never retried automatically.
  if (!accepted || !editor.isConnected || editor.innerText !== text) {
    return {state: 'draft_unconfirmed', input_dispatched: true};
  }
  return {state: 'draft_observed', input_dispatched: true, submitted: false};
}

// Watch dispatch gestures before exposing text. A provider may acknowledge a
// manual send asynchronously, leaving all final DOM checks unchanged meanwhile.
// This records uncertainty; it never suppresses or claims a successful user send.
function insertObservedSubchatDraft(document, text) {
  const controller = new AbortController();
  const guard = {intervened: false, stop: () => controller.abort()};
  const observe = event => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest('button');
    const label = button && (button.getAttribute('aria-label') || button.textContent.trim());
    if ((event.type === 'click' && ['送信', 'Send'].includes(label)) ||
        (event.type === 'submit' && target.matches('form[data-chatgpt-composer]')) ||
        (event.type === 'keydown' && event.key === 'Enter' && !event.shiftKey &&
         !event.isComposing && target.closest(
           'form[data-chatgpt-composer] [data-composer-markdown][role="textbox"]')))
      guard.intervened = true;
  };
  for (const type of ['click', 'submit', 'keydown'])
    document.defaultView.addEventListener(type, observe,
                                         {capture: true, signal: controller.signal});
  try {
    guard.draft = insertSubchatDraft(document, text);
    return guard;
  } catch (error) {
    guard.stop();
    throw error;
  }
}

// Final comparison and click share one browser task. Do not overwrite a user
// edit or click again after a manual send changed the conversation or composer.
function submitSubchatDraft(document, expectedUrl, text, previousIds) {
  if (document.location.href.replace(/\/$/, '') !== expectedUrl.replace(/\/$/, '') ||
      document.querySelectorAll('main').length !== 1) return false;
  const editors = document.querySelectorAll(
    'form[data-chatgpt-composer] [data-composer-markdown][role="textbox"]');
  if (editors.length !== 1 || editors[0].innerText !== text ||
      !editors[0].getClientRects().length) return false;
  const ids = [...document.querySelectorAll('main [data-turn-key]')]
    .map(node => node.getAttribute('data-turn-key'));
  if (JSON.stringify(ids) !== JSON.stringify(previousIds)) return false;
  const buttons = [...document.querySelectorAll('button')]
    .filter(button => button.getClientRects().length);
  const label = button => button.getAttribute('aria-label') || button.textContent.trim();
  if (buttons.some(button => ['停止', 'Stop', 'Stop generating'].includes(label(button))))
    return false;
  const send = buttons.filter(button => ['送信', 'Send'].includes(label(button)));
  if (send.length !== 1 || send[0].disabled || send[0].getAttribute('aria-disabled') === 'true')
    return false;
  send[0].click();
  return true;
}
