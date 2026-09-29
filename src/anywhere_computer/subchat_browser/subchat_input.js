// Current ordinary-Chat editor contract. Insertion itself never sends.
const subchatComposerSelector =
  'form[data-type="unified-composer"], form[data-chatgpt-composer]';
const subchatEditorSelector =
  'form[data-type="unified-composer"] #prompt-textarea[role="textbox"], ' +
  'form[data-chatgpt-composer] [data-composer-markdown][role="textbox"]';

function subchatDraftMatches(editor, text) {
  if (editor.innerText === text &&
      !editor.querySelector('[data-rich-text-generated-autolink], a, p, ' +
                            'br.ProseMirror-trailingBreak')) return true;
  // Paragraphs and generated URL icons add layout-only newlines to innerText.
  // Read the observed paragraph/inline contract; never trim or collapse text.
  let autolink = false;
  let paragraphs = false;
  function read(node, root = false) {
    if (node.nodeType === 3) return node.nodeValue;
    if (node.nodeType !== 1) throw new Error('Unrecognized draft node');
    if (!root && node.matches('[data-rich-text-generated-autolink]')) {
      const href = node.getAttribute('text-link-href') ||
        node.getAttribute('data-rich-text-link-href');
      if (!href || !/^https?:\/\//.test(href)) throw new Error('Unrecognized URL');
      if (['text-link-href', 'data-rich-text-link-href'].some(
        key => node.hasAttribute(key) && node.getAttribute(key) !== href))
        throw new Error('Conflicting URL targets');
      const copy = node.cloneNode(true);
      for (const icon of copy.querySelectorAll(
        '[data-inline-url-icon][aria-hidden="true"][contenteditable="false"]')) icon.remove();
      if (copy.textContent !== href || copy.querySelector(':not(span)'))
        throw new Error('Changed URL label');
      autolink = true;
      return href;
    }
    if (!root && node.tagName === 'BR') {
      if (node.classList.contains('ProseMirror-trailingBreak')) {
        const paragraph = node.parentElement;
        if (node.attributes.length !== 1 ||
            node.getAttribute('class') !== 'ProseMirror-trailingBreak' ||
            paragraph?.tagName !== 'P' || paragraph.parentElement !== editor ||
            paragraph.getAttribute('data-empty-paragraph') !== 'true' ||
            paragraph.childNodes.length !== 1)
          throw new Error('Unrecognized trailing break');
        // This exact empty-paragraph filler only keeps the caret visible.
        // Its paragraph boundary, below, contributes the actual newline.
        return '';
      }
      return '\n';
    }
    if (!root && !['P', 'SPAN'].includes(node.tagName))
      throw new Error('Unrecognized draft markup');
    if (!root && (node.getAttribute('contenteditable') === 'false' ||
                  node.getAttribute('aria-hidden') === 'true'))
      throw new Error('Unrecognized draft widget');
    const children = [...node.childNodes];
    if (children.some(child => child.nodeType === 1 && child.tagName === 'P')) {
      if (!root || children.some(child => child.nodeType !== 1 || child.tagName !== 'P'))
        throw new Error('Unrecognized paragraphs');
      paragraphs = true;
      return children.map(child => read(child)).join('\n');
    }
    return children.map(child => read(child)).join('');
  }
  try { return read(editor, true) === text && (autolink || paragraphs); }
  catch { return false; }
}

async function insertSubchatDraft(document, text) {
  if (typeof text !== 'string' || text.length === 0 || text.includes('\r')) {
    return {state: 'invalid_input', input_dispatched: false};
  }
  const editors = document.querySelectorAll(subchatEditorSelector);
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
  const accepted = document.execCommand('insertText', false, text);
  // Flush the already queued editor MutationObserver once. Native insertion
  // creates temporary paragraph BRs before ProseMirror marks its view fillers.
  // No timer, new observer, retry, or second input is installed here.
  await Promise.resolve();
  // A rejected or changed edit must be inspected, never retried automatically.
  const current = document.querySelectorAll(subchatEditorSelector);
  if (!accepted || !editor.isConnected || current.length !== 1 || current[0] !== editor ||
      !subchatDraftMatches(editor, text)) {
    return {state: 'draft_unconfirmed', input_dispatched: true};
  }
  return {state: 'draft_observed', input_dispatched: true, submitted: false};
}

// Watch dispatch gestures before exposing text. A provider may acknowledge a
// manual send asynchronously, leaving all final DOM checks unchanged meanwhile.
// This records uncertainty; it never suppresses or claims a successful user send.
async function insertObservedSubchatDraft(document, text) {
  const controller = new AbortController();
  const guard = {intervened: false, stop: () => controller.abort()};
  const observe = event => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const button = target.closest('button');
    const label = button && (button.getAttribute('aria-label') || button.textContent.trim());
    if ((event.type === 'click' && button &&
         (button.matches('[data-testid="send-button"]') ||
          ['送信', 'Send', 'プロンプトを送信する'].includes(label))) ||
        (event.type === 'submit' && target.matches(subchatComposerSelector)) ||
        (event.type === 'keydown' && event.key === 'Enter' && !event.shiftKey &&
         !event.isComposing && target.closest(subchatEditorSelector)))
      guard.intervened = true;
  };
  for (const type of ['click', 'submit', 'keydown'])
    document.defaultView.addEventListener(type, observe,
                                         {capture: true, signal: controller.signal});
  const originalEditors = document.querySelectorAll(subchatEditorSelector);
  const originalEditor = originalEditors.length === 1 ? originalEditors[0] : null;
  try {
    guard.draft = await insertSubchatDraft(document, text);
    // Capture a refused insertion before the browser can run another user
    // event. A later replacement draft is not ours to discard or close.
    if (guard.draft.state !== 'draft_observed') {
      guard.editor = originalEditor;
      guard.draftHTML = guard.editor?.innerHTML;
    }
    return guard;
  } catch (error) {
    guard.stop();
    throw error;
  }
}

// Distinguish a local refusal from an observed manual dispatch gesture. Never
// normalize whitespace here: changed draft text must not pass as the input.
function submitObservedSubchatDraft(document, guard, ...args) {
  if (guard.intervened) return 'intervened';
  if (guard.draft.state !== 'draft_observed') {
    const editors = document.querySelectorAll(subchatEditorSelector);
    return guard.draft.input_dispatched === true && editors.length === 1 &&
      editors[0] === guard.editor && editors[0].innerHTML === guard.draftHTML
      ? 'draft_rejected' : 'submission_unconfirmed';
  }
  // A later change can be a user's replacement draft. Preserve that page and
  // the unknown outcome instead of treating it as our rejected insertion.
  return submitSubchatDraft(document, ...args) ? 'submitted' : 'submission_unconfirmed';
}

// Final comparison and click share one browser task. Do not overwrite a user
// edit or click again after a manual send changed the conversation or composer.
function submitSubchatDraft(document, expectedUrl, text, previousIds) {
  if (document.location.href.replace(/\/$/, '') !== expectedUrl.replace(/\/$/, '') ||
      document.querySelectorAll('main').length !== 1) return false;
  const editors = document.querySelectorAll(subchatEditorSelector);
  if (editors.length !== 1 || !subchatDraftMatches(editors[0], text) ||
      !editors[0].getClientRects().length) return false;
  const messages = [...document.querySelectorAll('main [data-message-author-role]')];
  const ids = (messages.length ? messages : [...document.querySelectorAll('main [data-turn-key]')])
    .map(node => node.getAttribute(messages.length ? 'data-message-id' : 'data-turn-key'));
  if (JSON.stringify(ids) !== JSON.stringify(previousIds)) return false;
  const buttons = [...document.querySelectorAll('button')]
    .filter(button => button.getClientRects().length);
  const label = button => button.getAttribute('aria-label') || button.textContent.trim();
  if (buttons.some(button => ['停止', 'Stop', 'Stop generating'].includes(label(button))))
    return false;
  const send = buttons.filter(button =>
    button.matches('form[data-type="unified-composer"] [data-testid="send-button"]') ||
    ['送信', 'Send', 'プロンプトを送信する'].includes(label(button)));
  if (send.length !== 1 || send[0].disabled || send[0].getAttribute('aria-disabled') === 'true')
    return false;
  send[0].click();
  return true;
}
