/* Experimental, read-only extraction from an already opened model menu.
 * Labels are UI observations, never provider IDs or quota guarantees.
 */
function observeSubchatModelMenu(document) {
  const available = element => !element.closest('[hidden], [inert], [aria-hidden="true"]')
    && element.getClientRects().length > 0
    && document.defaultView.getComputedStyle(element).visibility === 'visible';
  const menus = Array.from(document.querySelectorAll('[role="menu"]'))
    .filter(available);
  if (menus.length !== 1) return { state: 'menu_unconfirmed' };
  const rows = Array.from(menus[0].querySelectorAll('[role="menuitemradio"]'))
    .filter(available);
  if (!rows.length) return { state: 'model_list_not_visible' };
  const models = [];
  for (const row of rows) {
    // Current menu puts the label and optional notice in separate leaf spans.
    // Reject changed markup rather than guessing model IDs from version strings.
    const parts = Array.from(row.querySelectorAll('span'))
      .filter(part => available(part) && part.children.length === 0)
      .map(part => part.textContent.trim()).filter(Boolean);
    const checked = row.getAttribute('aria-checked');
    if (!parts.length || !['true', 'false'].includes(checked))
      return { state: 'unsupported_menu' };
    models.push({ label: parts[0], notices: parts.slice(1),
      selected: checked === 'true',
      disabled: !!row.closest('[aria-disabled="true"], [disabled]') });
  }
  if (new Set(models.map(model => model.label)).size !== models.length
      || models.filter(model => model.selected).length !== 1)
    return { state: 'ambiguous_menu' };
  return { state: 'models_observed', models };
}
