// The Historical Detection Log (Table 3.4): a searchable gallery of past scans.
//
// Thumbnails are annotated server-side and stored that way, so a gallery tile
// is self-contained -- it does not need the live overlay stream to make sense,
// which matters because these are the evidence record.

import type { CaptureCamera, EventsPage, ScanEvent, Severity } from './api.js';
import { fetchJson } from './bus.js';
import { dateTime, errorText, num, pct, relative, severityLabel } from './fmt.js';
import { isSimple, locale, word } from './words.js';

// The simple view names a severity in plain words, in the chosen language.
const severityText = (severity: string): string => (isSimple() ? word(`sev.${severity}`) : severityLabel(severity));

// The detector found no leaves in this photo. "Healthy" would claim what it
// never checked, so the simple view says what happened instead.
type Reading = Pick<ScanEvent, 'severity' | 'detection_count'>;
const noLeaves = (event: Reading): boolean => isSimple() && event.severity === 'none' && !event.detection_count;
const photoText = (event: Reading): string => (noLeaves(event) ? word('photos.no_leaves') : severityText(event.severity));
const photoClass = (event: Reading): string => (noLeaves(event) ? 'muted' : `sev-${event.severity}`);

// A photo of several cameras is as bad as its worst camera, and holds the
// leaves all of them found.
const RANK: Severity[] = ['none', 'low', 'moderate', 'high'];
function overall(event: ScanEvent): Reading {
  const cameras = event.cameras || [];
  if (cameras.length < 2) return event;
  return {
    severity: cameras.reduce<Severity>((worst, c) => (RANK.indexOf(c.severity) > RANK.indexOf(worst) ? c.severity : worst), 'none'),
    detection_count: cameras.reduce((sum, c) => sum + (c.detection_count || 0), 0),
  };
}
const cameraName = (c: CaptureCamera): string => c.camera_name || word('camera.n', { n: c.camera + 1 });

export class History {
  private readonly root: HTMLElement;
  private readonly gallery: HTMLElement;
  private readonly pager: HTMLElement | null;
  private readonly dialog: HTMLDialogElement | null;
  private readonly pageSize: number;
  private offset = 0;
  private total = 0;
  private filters: Record<string, string> = {};
  // Name the camera only when this page has scans from more than one.
  private multiCamera = false;

  constructor(root: HTMLElement, { pageSize = 12 }: { pageSize?: number } = {}) {
    this.root = root;
    this.gallery = root.querySelector<HTMLElement>('[data-gallery]')!;
    this.pager = root.querySelector<HTMLElement>('[data-pager]');
    this.dialog = document.querySelector<HTMLDialogElement>('[data-scan-dialog]');
    this.pageSize = pageSize;

    this.bindFilters();
    this.bindDialog();
  }

  private bindFilters(): void {
    const form = this.root.querySelector<HTMLFormElement>('[data-filters]');
    if (!form) return;

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      this.applyFilters(form);
    });
    form.addEventListener('change', () => this.applyFilters(form));

    const reset = form.querySelector('[data-filter-reset]');
    if (reset) {
      reset.addEventListener('click', () => {
        form.reset();
        this.filters = {};
        this.offset = 0;
        this.load();
      });
    }
  }

  private applyFilters(form: HTMLFormElement): void {
    const data = new FormData(form);
    this.filters = {};
    for (const [key, value] of data.entries()) {
      if (typeof value !== 'string' || !value) continue;
      // Dates are the phone's local days, through the end of the "to" day;
      // scans are stored in UTC.
      if (key === 'from') this.filters.from = new Date(`${value}T00:00:00`).toISOString();
      else if (key === 'to') this.filters.to = new Date(`${value}T23:59:59.999`).toISOString();
      else this.filters[key] = value;
    }
    this.offset = 0;
    this.load();
  }

  private bindDialog(): void {
    const dialog = this.dialog;
    if (!dialog) return;
    const close = dialog.querySelector('[data-dialog-close]');
    if (close) close.addEventListener('click', () => dialog.close());
    // Clicking the backdrop closes it, which is what a thumb reaches for first.
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog) dialog.close();
    });
  }

  private query(): string {
    const params = new URLSearchParams({
      limit: String(this.pageSize),
      offset: String(this.offset),
      // There is no archive view any more, so scans archived earlier show too.
      archived: 'all',
      ...this.filters,
    });
    return `/api/events?${params.toString()}`;
  }

  async load(): Promise<void> {
    try {
      const page = await fetchJson<EventsPage>(this.query());
      if (!page) return;
      this.total = page.total;
      this.render(page.items || []);
      this.renderPager();
    } catch (error) {
      this.gallery.innerHTML = `<p class="empty">Could not load history: ${escapeHtml(errorText(error))}</p>`;
    }
  }

  private render(items: ScanEvent[]): void {
    if (!items.length) {
      const message = isSimple() ? word('photos.empty') : 'No scans match these filters.';
      this.gallery.innerHTML = `<p class="empty">${escapeHtml(message)}</p>`;
      return;
    }

    // Rebuilt wholesale each page rather than diffed. The listener count stays
    // bounded because every node is replaced together -- an incremental
    // rebuild that forgot to detach handlers is the exact leak §3.5.1 asks the
    // Selenium suite to watch for.
    this.gallery.innerHTML = '';
    this.multiCamera = new Set(items.map((item) => item.camera || 0)).size > 1;
    items.forEach((item) => this.gallery.appendChild(this.tile(item)));
  }

  private tile(item: ScanEvent): HTMLButtonElement {
    const button = document.createElement('button');
    button.className = 'scan';
    button.type = 'button';
    button.dataset.eventId = String(item.id);
    const shown = overall(item);
    const cameras = item.cameras || [];
    button.setAttribute(
      'aria-label',
      isSimple()
        ? `${word('photos.at', { time: dateTime(item.captured_at) })}, ${photoText(shown)}`
        : `Scan at ${dateTime(item.captured_at)}, severity ${severityLabel(shown.severity)}`,
    );

    const thumb = document.createElement('img');
    thumb.className = 'scan-thumb';
    thumb.loading = 'lazy';
    thumb.decoding = 'async';
    thumb.alt = '';
    thumb.classList.toggle('is-pair', cameras.length === 2); // side by side: twice as wide
    if (item.thumb_path) thumb.src = `/media/${item.thumb_path}`;
    else if (item.image_path) thumb.src = `/media/${item.image_path}`;

    const meta = document.createElement('div');
    meta.className = 'scan-meta';

    const time = document.createElement('div');
    time.className = 'scan-time';
    time.textContent = dateTime(item.captured_at);

    const detail = document.createElement('div');
    detail.className = 'scan-detail';
    const severity = document.createElement('span');
    severity.className = photoClass(shown);
    severity.textContent = photoText(shown);
    detail.appendChild(severity);
    // Each its own element: adjacent text would run together ("24 boxes2 cameras").
    const piece = (text: string) => {
      const span = document.createElement('span');
      span.textContent = text;
      detail.appendChild(span);
    };
    if (!isSimple()) piece(`${shown.detection_count} box${shown.detection_count === 1 ? '' : 'es'}`);
    if (cameras.length > 1) {
      // The photo names each camera itself.
      if (!isSimple()) piece(`${cameras.length} cameras`);
    } else if (this.multiCamera) {
      const camera = (item.camera || 0) + 1;
      piece(isSimple() ? word('camera.n', { n: camera }) : `Cam ${camera}`);
    }
    if (item.synthetic) {
      const flag = document.createElement('span');
      flag.className = 'sev-moderate';
      flag.title = 'Produced by the stub detector, not a trained model';
      flag.textContent = 'synthetic';
      detail.appendChild(flag);
    }

    meta.appendChild(time);
    meta.appendChild(detail);
    button.appendChild(thumb);
    button.appendChild(meta);
    button.addEventListener('click', () => this.open(item.id));
    return button;
  }

  private renderPager(): void {
    if (!this.pager) return;
    const from = this.total === 0 ? 0 : this.offset + 1;
    const to = Math.min(this.offset + this.pageSize, this.total);

    this.pager.innerHTML = '';
    const previous = pagerButton(word('pager.previous'), this.offset === 0, () => {
      this.offset = Math.max(0, this.offset - this.pageSize);
      this.load();
    });
    const label = document.createElement('span');
    label.textContent = word('pager.range', { from, to, total: this.total });
    const next = pagerButton(word('pager.next'), to >= this.total, () => {
      this.offset += this.pageSize;
      this.load();
    });

    this.pager.appendChild(previous);
    this.pager.appendChild(label);
    this.pager.appendChild(next);
  }

  private async open(eventId: number): Promise<void> {
    if (!this.dialog) return;
    try {
      // The advice headline comes back in the chosen language.
      const event = await fetchJson<ScanEvent>(`/api/events/${eventId}?locale=${locale()}`);
      if (!event) return;

      const image = this.dialog.querySelector<HTMLImageElement>('[data-dialog-image]');
      if (image) {
        // No saved image: hide it rather than show a broken-image box.
        image.hidden = !event.image_path;
        if (event.image_path) image.src = `/media/${event.image_path}`;
        else image.removeAttribute('src');
        image.alt = `Scan from ${dateTime(event.captured_at)}`;
      }

      const meta = this.dialog.querySelector('[data-dialog-meta]');
      const shown = overall(event);
      const cameras = (event.cameras || []).length > 1 ? event.cameras || [] : [];
      if (meta && isSimple()) {
        meta.innerHTML = `
          <div><strong>${escapeHtml(dateTime(event.captured_at))}</strong>
            <span class="muted">${escapeHtml(relative(event.captured_at))}${
              this.multiCamera && !cameras.length ? ` · ${escapeHtml(word('camera.n', { n: (event.camera || 0) + 1 }))}` : ''
            }</span></div>
          <div class="${escapeHtml(photoClass(shown))}">${escapeHtml(photoText(shown))}</div>
          ${cameras.map((c) => `<div class="${escapeHtml(photoClass(c))}">${escapeHtml(cameraName(c))}: ${escapeHtml(photoText(c))}</div>`).join('')}
          ${event.advice && !noLeaves(shown) ? `<div>${escapeHtml(event.advice.headline)}</div>` : ''}`;
      } else if (meta) {
        meta.innerHTML = `
          <div><strong>${escapeHtml(dateTime(event.captured_at))}</strong>
            <span class="muted">${escapeHtml(relative(event.captured_at))}${
              this.multiCamera && !cameras.length ? ` · Camera ${(event.camera || 0) + 1}` : ''
            }</span></div>
          ${cameras.length ? cameras.map((c) => `<div class="sev-${escapeHtml(c.severity)}">${escapeHtml(cameraName(c))}: ${escapeHtml(
            severityLabel(c.severity),
          )}, ${c.detection_count} detections${c.max_confidence ? `, best ${pct(c.max_confidence, 0)}` : ''}</div>`).join('') : `
          <div class="sev-${escapeHtml(event.severity)}">Severity: ${escapeHtml(
            severityLabel(event.severity),
          )}</div>
          <div class="muted">${event.detection_count} detections${
            event.max_confidence ? `, best ${pct(event.max_confidence, 0)}` : ''
          }${
            event.damaged_area ? `, ${pct(event.damaged_area, 1)} of frame affected` : ''
          }</div>`}
          <div class="muted mono">${escapeHtml(event.model_version)} · conf ≥ ${num(
            event.conf_threshold,
            2,
          )} · ${event.inference_ms ? `${num(event.inference_ms, 0)} ms` : '—'}</div>
          ${event.synthetic ? '<div class="sev-moderate">Synthetic — not a field measurement</div>' : ''}
          ${event.advice ? `<div>${escapeHtml(event.advice.headline)}</div>` : ''}`;
      }

      this.dialog.showModal();
    } catch (error) {
      console.error('[pw] could not open scan', error);
    }
  }
}

function pagerButton(label: string, disabled: boolean, onClick: () => void): HTMLButtonElement {
  const button = document.createElement('button');
  button.className = 'btn is-quiet';
  button.type = 'button';
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener('click', onClick);
  return button;
}

function escapeHtml(text: unknown): string {
  const div = document.createElement('div');
  div.textContent = String(text ?? '');
  return div.innerHTML;
}
