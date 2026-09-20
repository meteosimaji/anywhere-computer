// Read-only experimental DOM receipt. This does not certify response completion.
function observeSubchatSubmission(document, expectedPrompt, previousTurnIds, expectedConversationId) {
  if (typeof expectedPrompt !== 'string' || !expectedPrompt.length
      || !Array.isArray(previousTurnIds)
      || previousTurnIds.some(id => typeof id !== 'string' || !id.length)
      || new Set(previousTurnIds).size !== previousTurnIds.length)
    return { state: 'invalid_baseline' };
  const url = new URL(document.location.href);
  // Only the persisted ordinary-Chat route observed in acceptance is supported.
  const match = /^\/c\/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/.exec(url.pathname);
  if (url.origin !== 'https://chatgpt.com' || !match
      || match[1] !== expectedConversationId || url.search || url.hash)
    return { state: 'conversation_unconfirmed' };
  const mains = document.querySelectorAll('main');
  if (mains.length !== 1) return { state: 'transcript_unconfirmed' };
  const turns = Array.from(mains[0].querySelectorAll('[data-turn-key]'));
  const ids = turns.map(turn => turn.getAttribute('data-turn-key'));
  if (ids.some(id => !id || id.trim() !== id) || new Set(ids).size !== ids.length)
    return { state: 'ambiguous_turns' };
  const baseline = new Set(previousTurnIds);
  const matches = [];
  for (const turn of turns) {
    const id = turn.getAttribute('data-turn-key');
    if (baseline.has(id)) continue;
    if (turn.querySelector('[data-turn-key]')) return { state: 'unsupported_turn' };
    const bubbles = turn.querySelectorAll('[data-user-message-bubble="true"]');
    if (bubbles.length !== 1) return { state: 'unsupported_turn' };
    // Observe the user bubble, never a quoted prompt in an assistant response.
    if (bubbles[0].innerText === expectedPrompt) matches.push(id);
  }
  if (matches.length !== 1) return { state: 'submission_unconfirmed' };
  return { state: 'submission_observed', conversation_id: match[1],
    user_message_id: matches[0] };
}
