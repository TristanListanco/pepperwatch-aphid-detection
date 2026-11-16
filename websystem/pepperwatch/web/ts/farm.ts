// The simple view's two cards: how the plants are, with what to do, and this
// week in words. Both come from /api/stats/weekly, whose advice is already in
// the chosen language; everything else here goes through words.js.

import type { WeeklyStats } from './api.js';
import { word } from './words.js';

const URGENCY_ICONS: Record<string, string> = {
  routine: 'i-leaf',
  monitor: 'i-scan-search',
  act_soon: 'i-triangle-alert',
  urgent: 'i-triangle-alert',
};
const TREND_ICONS: Record<string, string> = { rising: 'i-trending-up', falling: 'i-trending-down', stable: 'i-minus' };

export function renderFarm(root: ParentNode, stats: WeeklyStats): void {
  renderStatus(root, stats);
  renderWeek(root, stats);
}

function renderStatus(root: ParentNode, stats: WeeklyStats): void {
  const card = root.querySelector<HTMLElement>('[data-farm-status]');
  if (!card) return;
  // What recent photos typically show (the server's status), not the week's
  // worst single frame, which one false alarm could set.
  const status = stats.status || {};
  const noModel = stats.detector_synthetic === true;
  const photos = noModel ? 0 : status.photos ?? 0;
  const advice = status.advice || {};
  // With no photos, "no damage" would be a claim the camera has not made; with
  // no model, any advice would rest on made-up boxes.
  const band = photos ? status.severity || 'none' : 'empty';
  const setBand = () => ['none', 'low', 'moderate', 'high', 'empty'].forEach((name) => {
    card.classList.toggle(`is-${name}`, name === band);
  });
  // A change between readings fades; the first one after "no photos yet" (the
  // page's starting state) lands at once.
  if (card.classList.contains('is-empty')) withoutFade(card, setBand);
  else setBand();

  const urgency = noModel ? 'no_model' : photos ? advice.urgency || 'unknown' : null;
  const icon = card.querySelector('[data-farm-icon]');
  if (icon) icon.setAttribute('href', `#${noModel ? 'i-triangle-alert' : (urgency && URGENCY_ICONS[urgency]) || 'i-sprout'}`);
  text(card, '[data-farm-urgency]', urgency ? word(`urgency.${urgency}`) : '');
  let headline = photos ? advice.headline || '' : word('status.empty');
  let body = photos ? advice.body || '' : word('status.empty_note');
  if (noModel) {
    headline = word('status.no_model');
    body = word('status.no_model_note');
  }
  text(card, '[data-farm-headline]', headline);
  text(card, '[data-farm-body]', body);

  const todo = card.querySelector<HTMLElement>('[data-farm-todo]');
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

  const day = status.basis === '24h';
  const basis = day ? 'status.based_day' : 'status.based';
  text(card, '[data-farm-based]', photos
    ? word(photos === 1 ? `${basis}_one` : basis, { n: photos })
    : '');
  const draft = card.querySelector<HTMLElement>('[data-farm-draft]');
  if (draft) {
    draft.hidden = !photos || advice.validated !== false;
    draft.textContent = word('status.draft');
  }
}

function renderWeek(root: ParentNode, stats: WeeklyStats): void {
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
  if (icon) icon.setAttribute('href', `#${(trend && TREND_ICONS[trend]) || 'i-minus'}`);
  text(card, '[data-farm-trend-text]', word(trend ? `trend.${trend}` : 'trend.early'));
}

// Applies a colour change at once instead of fading it, so a placeholder
// colour (the health card's green before any reading) never shows on its way
// to the real one. Important, to beat the reduced-motion rule's own.
export function withoutFade(element: HTMLElement, apply: () => void): void {
  element.style.setProperty('transition', 'none', 'important');
  apply();
  void element.offsetWidth; // commit the new colours before the fade returns
  element.style.removeProperty('transition');
}

function text(root: ParentNode, selector: string, value: string): void {
  const element = root.querySelector(selector);
  if (element) element.textContent = value;
}
