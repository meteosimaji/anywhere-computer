/* Experimental, read-only extraction from an already opened model menu.
 * Labels are UI observations, never provider IDs or quota guarantees.
 */
function subchatMenuElementVisible(document, element) {
  return !element.closest('[hidden], [inert], [aria-hidden="true"]')
    && Array.from(element.getClientRects()).some(rect => rect.width > 0 && rect.height > 0)
    && document.defaultView.getComputedStyle(element).visibility === 'visible';
}

function observeSubchatModelMenu(document) {
  const available = element => subchatMenuElementVisible(document, element);
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
    let parts = Array.from(row.querySelectorAll('span'))
      .filter(part => available(part) && part.children.length === 0)
      .map(part => part.textContent.trim()).filter(Boolean);
    if (!parts.length) {
      const labels = Array.from(row.querySelectorAll('div.truncate'))
        .filter(part => available(part) && part.children.length === 0)
        .map(part => part.textContent.trim()).filter(Boolean);
      if (labels.length === 1) {
        const lines = row.innerText.split('\n').map(part => part.trim()).filter(Boolean);
        if (lines[0] === labels[0]) parts = lines;
      }
    }
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


function observeSubchatEffort(document) {
  const menus = Array.from(document.querySelectorAll('[role="menu"]'))
    .filter(element => subchatMenuElementVisible(document, element));
  if (menus.length !== 1) return { state: 'menu_unconfirmed' };
  const controls = Array.from(menus[0].querySelectorAll(
    '[data-model-reasoning-effort-slider], [data-reasoning-slider="true"]'))
    .filter(element => subchatMenuElementVisible(document, element));
  if (controls.length !== 1) return { state: 'effort_control_unconfirmed' };
  const control = controls[0];
  // The visible keyboard control owns an aria-hidden thumb in the current UI.
  // Only read that thumb inside the confirmed control; do not operate it.
  const thumbs = control.querySelectorAll('[role="slider"]');
  if (thumbs.length !== 1) return { state: 'unsupported_effort_control' };
  const values = ['aria-valuemin', 'aria-valuemax', 'aria-valuenow'].map(
    name => thumbs[0].getAttribute(name));
  if (values.some(value => value === null || !/^-?\d+$/.test(value)))
    return { state: 'unsupported_effort_control' };
  const [minimum, maximum, index] = values.map(Number);
  if (![minimum, maximum, index].every(Number.isSafeInteger)
      || minimum > maximum || index < minimum || index > maximum)
    return { state: 'unsupported_effort_control' };
  const descriptionOwner = control.closest('[role="menuitem"][aria-label="パワー"]') || control;
  const descriptions = (descriptionOwner.getAttribute('aria-describedby') || '').split(/\s+/)
    .filter(Boolean).map(id => document.getElementById(id))
    .filter(element => element && menus[0].contains(element)
      && subchatMenuElementVisible(document, element));
  const modernDescription = descriptions.length === 2
    && descriptions.every(element => element.tagName === 'SPAN'
      && element.textContent.trim())
    && [null, 'status'].includes(descriptions[0].getAttribute('role'));
  const legacyDescription = descriptions.length === 1
    && descriptions[0].getAttribute('role') === 'status';
  if ((!modernDescription && !legacyDescription) || !descriptions[0].textContent.trim())
    return { state: 'effort_description_unconfirmed' };
  return { state: 'effort_observed', minimum, maximum, index,
    description: descriptions[0].textContent.trim(),
    disabled: !!control.closest('[aria-disabled="true"], [disabled]') };
}
