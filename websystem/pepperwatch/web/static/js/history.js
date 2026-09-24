// The Historical Detection Log (Table 3.4): a searchable gallery of past scans.
//
// Thumbnails are annotated server-side and stored that way, so a gallery tile
// is self-contained -- it does not need the live overlay stream to make sense,
// which matters because these are the evidence record.

import { fetchJson, postJson } from './bus.js';
import { dateTime, num, pct, relative, severityLabel } from './fmt.js';

export class History {
  constructor(root, { pageSize = 12 } = {}) {
    this.root = root;
    this.gallery = root.querySelector('[data-gallery]');
    this.pager = root.querySelector('[data-pager]');
    this.dialog = document.querySelector('[data-scan-dialog]');
    this.pageSize = pageSize;
    this.offset = 0;
    this.total = 0;
    this.filters = {};
    this.archivedView = root.dataset.archived === 'true';

    this.bindFilters();
    this.bindDialog();
  }

  bindFilters() {
    const form = this.root.querySelector('[data-filters]');
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

  applyFilters(form) {
    const data = new FormData(form);
    this.filters = {};
    for (const [key, value] of data.entries()) {
      if (value) this.filters[key] = value;
    }
    this.offset = 0;
    this.load();
  }

  bindDialog() {
    if (!this.dialog) return;
    const close = this.dialog.querySelector('[data-dialog-close]');
    if (close) close.addEventListener('click', () => this.dialog.close());
    // Clicking the backdrop closes it, which is what a thumb reaches for first.
    this.dialog.addEventListener('click', (event) => {
      if (event.target === this.dialog) this.dialog.close();
    });
  }

  query() {
    const params = new URLSearchParams({
      limit: String(this.pageSize),
      offset: String(this.offset),
      archived: this.archivedView ? 'true' : 'false',
      ...this.filters,
    });
    return `/api/events?${params.toString()}`;
  }

  async load() {
    try {
      const page = await fetchJson(this.query());
      if (!page) return;
      this.total = page.total;
      this.render(page.items || []);
      this.renderPager();
    } catch (error) {
      this.gallery.innerHTML = `<p class="empty">Could not load history: ${escapeHtml(error.message)}</p>`;
    }
  }

  render(items) {
    if (!items.length) {
      this.gallery.innerHTML = `<p class="empty">${
        this.archivedView ? 'Nothing archived yet.' : 'No scans match these filters.'
      }</p>`;
      return;
    }

    // Rebuilt wholesale each page rather than diffed. The listener count stays
    // bounded because every node is replaced together -- an incremental
    // rebuild that forgot to detach handlers is the exact leak §3.5.1 asks the
    // Selenium suite to watch for.
    this.gallery.innerHTML = '';
    items.forEach((item) => this.gallery.appendChild(this.tile(item)));
  }

  tile(item) {
    const button = document.createElement('button');
    button.className = 'scan';
    button.type = 'button';
    button.dataset.eventId = String(item.id);
    button.setAttribute(
      'aria-label',
      `Scan at ${dateTime(item.captured_at)}, severity ${severityLabel(item.severity)}`,
    );

    const thumb = document.createElement('img');
    thumb.className = 'scan-thumb';
    thumb.loading = 'lazy';
    thumb.decoding = 'async';
    thumb.alt = '';
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
    severity.className = `sev-${item.severity}`;
    severity.textContent = severityLabel(item.severity);
    detail.appendChild(severity);
    detail.appendChild(
      document.createTextNode(
        `${item.detection_count} box${item.detection_count === 1 ? '' : 'es'}`,
      ),
    );
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

  renderPager() {
    if (!this.pager) return;
    const from = this.total === 0 ? 0 : this.offset + 1;
    const to = Math.min(this.offset + this.pageSize, this.total);

    this.pager.innerHTML = '';
    const previous = pagerButton('Previous', this.offset === 0, () => {
      this.offset = Math.max(0, this.offset - this.pageSize);
      this.load();
    });
    const label = document.createElement('span');
    label.textContent = `${from}–${to} of ${this.total}`;
    const next = pagerButton('Next', to >= this.total, () => {
      this.offset += this.pageSize;
      this.load();
    });

    this.pager.appendChild(previous);
    this.pager.appendChild(label);
    this.pager.appendChild(next);
  }

  async open(eventId) {
    if (!this.dialog) return;
    try {
      const event = await fetchJson(`/api/events/${eventId}`);
      if (!event) return;

      const image = this.dialog.querySelector('[data-dialog-image]');
      if (image) {
        image.src = event.image_path ? `/media/${event.image_path}` : '';
        image.alt = `Scan from ${dateTime(event.captured_at)}`;
      }

      const meta = this.dialog.querySelector('[data-dialog-meta]');
      if (meta) {
        meta.innerHTML = `
          <div><strong>${escapeHtml(dateTime(event.captured_at))}</strong>
            <span class="muted">${escapeHtml(relative(event.captured_at))}</span></div>
          <div class="sev-${escapeHtml(event.severity)}">Severity: ${escapeHtml(
            severityLabel(event.severity),
          )}</div>
          <div class="muted">${event.detection_count} detections${
            event.max_confidence ? `, best ${pct(event.max_confidence, 0)}` : ''
          }${
            event.damaged_area ? `, ${pct(event.damaged_area, 1)} of frame affected` : ''
          }</div>
          <div class="muted mono">${escapeHtml(event.model_version)} · conf ≥ ${num(
            event.conf_threshold,
            2,
          )} · ${event.inference_ms ? `${num(event.inference_ms, 0)} ms` : '—'}</div>
          ${event.synthetic ? '<div class="sev-moderate">Synthetic — not a field measurement</div>' : ''}
          ${event.advice ? `<div>${escapeHtml(event.advice.headline)}</div>` : ''}`;
      }

      const archive = this.dialog.querySelector('[data-dialog-archive]');
      if (archive) {
        archive.textContent = event.archived ? 'Restore' : 'Archive';
        archive.onclick = async () => {
          await postJson(`/api/events/${eventId}/archive`, { archived: !event.archived });
          this.dialog.close();
          this.load();
        };
      }

      this.dialog.showModal();
    } catch (error) {
      console.error('[pw] could not open scan', error);
    }
  }
}

function pagerButton(label, disabled, onClick) {
  const button = document.createElement('button');
  button.className = 'btn is-quiet';
  button.type = 'button';
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener('click', onClick);
  return button;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = String(text ?? '');
  return div.innerHTML;
}
