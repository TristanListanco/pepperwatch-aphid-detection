// The Actionable Output Panel (Table 3.4).
//
// Table 3.4 asks this panel to turn "raw mathematical classification outputs"
// into plain-text alerts and treatment recommendations. The text itself lives
// in config/recommendations.json and is served by the API, not hard-coded here,
// for two reasons: it has to be translatable (Northern Mindanao is
// Cebuano-speaking, and "localized" will be read as translated), and pesticide
// guidance reaching farmers needs to be reviewable by an agriculturist without
// editing JavaScript.
//
// Until that review happens the file is marked as a draft and this panel says
// so. A dashboard that presents unreviewed treatment advice as settled fact is
// worse than one that admits it is provisional.

export function renderAdvisory(root, advice) {
  if (!advice) return;

  const headline = root.querySelector('[data-advice-headline]');
  if (headline) headline.textContent = advice.headline || '';

  const urgency = root.querySelector('[data-advice-urgency]');
  if (urgency) {
    urgency.textContent = (advice.urgency || 'routine').replace('_', ' ');
    urgency.className = `advisory-urgency is-${advice.urgency || 'routine'}`;
  }

  const body = root.querySelector('[data-advice-body]');
  if (body) body.textContent = advice.body || '';

  const actions = root.querySelector('[data-advice-actions]');
  if (actions) {
    actions.innerHTML = '';
    (advice.actions || []).forEach((action) => {
      const li = document.createElement('li');
      li.textContent = action;
      actions.appendChild(li);
    });
  }

  const draft = root.querySelector('[data-advice-draft]');
  if (draft) draft.hidden = advice.validated === true;
}
