// The simple view's two cards: how the plants are, with what to do, and this
// week in words. Both come from /api/stats/weekly, whose advice is already in
// the chosen language; everything else here goes through words.js.

import { word } from './words.js';

const URGENCY_ICONS = {
  routine: 'i-leaf',
  monitor: 'i-scan-search',
  act_soon: 'i-triangle-alert',
  urgent: 'i-triangle-alert',
};
const TREND_ICONS = { rising: 'i-trending-up', falling: 'i-trending-down', stable: 'i-minus' };

export function renderFarm(root, stats) {
  renderStatus(root, stats);
  renderWeek(root, stats);
}

function renderStatus(root, stats) {
  const card = root.querySelector('[data-farm-status]');
  if (!card) return;
  const photos = stats.events ?? 0;
  const advice = stats.advice || {};
  // With no photos, "no damage" would be a claim the camera has not made.
  const band = photos ? stats.severity || 'none' : 'empty';
  ['none', 'low', 'moderate', 'high', 'empty'].forEach((name) => {
    card.classList.toggle(`is-${name}`, name === band);
  });

  const urgency = photos ? advice.urgency || 'unknown' : null;
  const icon = card.querySelector('[data-farm-icon]');
  if (icon) icon.setAttribute('href', `#${URGENCY_ICONS[urgency] || 'i-sprout'}`);
  text(card, '[data-farm-urgency]', urgency ? word(`urgency.${urgency}`) : '');
  text(card, '[data-farm-headline]', photos ? advice.headline || '' : word('status.empty'));
  text(card, '[data-farm-body]', photos ? advice.body || '' : word('status.empty_note'));

  const todo = card.querySelector('[data-farm-todo]');
  const list = card.querySelector('[data-farm-actions]');
  const actions = photos ? advice.actions || [] : [];
  if (todo && list) {
    todo.hidden = !actions.length;
    list.replaceChildren(...actions.map((action) => {
      const item = document.createElement('li');
      item.textContent = action;
      return item;
    }));
  }

  text(card, '[data-farm-based]', photos
    ? word(photos === 1 ? 'status.based_one' : 'status.based', { n: photos })
    : '');
  const draft = card.querySelector('[data-farm-draft]');
  if (draft) {
    draft.hidden = !photos || advice.validated !== false;
    draft.textContent = word('status.draft');
  }
}

function renderWeek(root, stats) {
  const card = root.querySelector('[data-farm-week]');
  if (!card) return;
  const distribution = stats.distribution || {};
  const healthy = distribution.healthy || 0;
  const total = healthy + (distribution.aphid_damage || 0);
  text(card, '[data-farm-healthy]', total
    ? word(total === 1 ? 'week.healthy_one' : 'week.healthy', { healthy, total })
    : word('week.none'));

  // No photos the week before: nothing to compare against.
  const trend = stats.events_previous ? stats.trend || 'stable' : null;
  const line = card.querySelector('[data-farm-trend]');
  if (line) {
    line.classList.toggle('is-up', trend === 'rising');
    line.classList.toggle('is-down', trend === 'falling');
  }
  const icon = card.querySelector('[data-farm-trend-icon]');
  if (icon) icon.setAttribute('href', `#${TREND_ICONS[trend] || 'i-minus'}`);
  text(card, '[data-farm-trend-text]', word(trend ? `trend.${trend}` : 'trend.early'));
}

function text(root, selector, value) {
  const element = root.querySelector(selector);
  if (element) element.textContent = value;
}
