// Current ordinary-Chat editor contract. This edits a draft; it never sends.
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
