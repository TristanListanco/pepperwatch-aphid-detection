// Live data transport.
//
// Server-Sent Events are the primary channel, with a setInterval REST fallback
// behind them. Both paths are real and both are needed:
//
//  * The manuscript commits to setInterval polling in §3.3.2.2 and §3.4.3, and
//    §3.5.1 says Selenium will track that function specifically. A fallback
//    that only existed on paper would make that test meaningless.
//  * SSE genuinely does die on a flaky field Wi-Fi link -- a phone that walks
//    behind a row of plants drops the connection, and the page has to keep
//    working rather than silently freeze.
//
// So: SSE first, and after three failed reconnects the bus switches to polling
// and stays there until the page is reloaded. Low-frequency data (the KPI
// tiles) always uses setInterval regardless, because refreshing a weekly
// aggregate over a push channel would be silly.
//
// window.__pw counters are exposed for the Selenium suite -- see
// tests/test_ui_selenium.py. They are the observable proof that the page keeps
// updating without a manual refresh.
const MAX_SSE_FAILURES = 3;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 15000;
const POLL_INTERVAL_MS = 2000;
// While polling, try live updates again this often: a Pi that rebooted is
// back long before the page is reloaded.
const SSE_RETRY_MS = 30000;
export class Bus {
    constructor() {
        this.handlers = {};
        this.source = null;
        this.failures = 0;
        this.mode = 'idle';
        this.pollTimer = null;
        this.sseRetryTimer = null;
        this.lastEventId = null;
        this.lastFrameSeq = null;
        // Observable state, for Selenium and for the debug chip.
        window.__pw = window.__pw || {
            sseMessages: 0,
            pollTicks: 0,
            intervalFires: 0,
            errors: 0,
            timeouts: 0,
            mode: 'idle',
            lastUpdate: 0,
        };
    }
    on(event, handler) {
        var _a;
        // TypeScript cannot follow K through the mapped type; the signature above keeps it honest.
        ((_a = this.handlers)[event] ?? (_a[event] = [])).push(handler);
        return this;
    }
    emit(event, data) {
        window.__pw.lastUpdate = Date.now();
        (this.handlers[event] ?? []).forEach((handler) => {
            try {
                handler(data);
            }
            catch (error) {
                window.__pw.errors += 1;
                console.error(`[pw] handler for ${event} failed`, error);
            }
        });
    }
    start() {
        this.connectSse();
        return this;
    }
    // -- SSE ---------------------------------------------------------------
    connectSse() {
        if (this.source)
            this.source.close();
        this.setMode('connecting');
        const source = new EventSource('/stream/events');
        this.source = source;
        source.addEventListener('open', () => {
            this.failures = 0;
            this.setMode('sse');
            this.stopPolling();
        });
        source.addEventListener('hello', (event) => {
            window.__pw.sseMessages += 1;
            const payload = safeParse(event.data);
            if (!payload)
                return;
            // The snapshot lets the page paint at once rather than showing empty
            // tiles until the next publication. Worker state goes last, so an
            // offline notice can say when the readings it greys out were taken.
            if (payload.session && payload.session.session_id)
                this.emit('session', payload.session);
            Object.entries(payload.latest || {}).forEach(([topic, data]) => {
                this.dispatchTopic(topic, data);
            });
            if (payload.worker)
                this.emit('worker', payload.worker);
        });
        [
            'telemetry/system', 'telemetry/pipeline', 'detect/frame', 'detect/flag',
            'event/persisted', 'status/worker', 'status/session', 'session/summary', 'data/cleared',
        ].forEach((topic) => {
            source.addEventListener(topic, (event) => {
                window.__pw.sseMessages += 1;
                const payload = safeParse(event.data);
                if (payload)
                    this.dispatchTopic(topic, payload);
            });
        });
        source.addEventListener('rejected', (event) => {
            // The server is at its SSE client limit. Polling is the right answer,
            // not a reconnect loop that keeps being refused.
            console.warn('[pw] SSE rejected', event.data);
            source.close();
            this.source = null;
            this.startPolling();
        });
        source.addEventListener('error', () => {
            window.__pw.errors += 1;
            source.close();
            this.source = null;
            this.failures += 1;
            if (this.failures >= MAX_SSE_FAILURES) {
                console.warn(`[pw] SSE failed ${this.failures}x — falling back to polling`);
                this.startPolling();
                return;
            }
            const delay = Math.min(RECONNECT_BASE_MS * 2 ** (this.failures - 1), RECONNECT_MAX_MS);
            this.setMode('reconnecting');
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = setTimeout(() => this.connectSse(), delay);
        });
    }
    // Server JSON, trusted to match api.d.ts as it always was; not validated.
    dispatchTopic(topic, payload) {
        switch (topic) {
            case 'telemetry/system':
                this.emit('telemetry', payload);
                break;
            case 'telemetry/pipeline':
                this.emit('pipeline', payload);
                break;
            case 'detect/frame':
                this.lastFrameSeq = payload.frame_seq ?? null;
                this.emit('detections', payload);
                break;
            case 'detect/flag':
                this.emit('flag', payload);
                break;
            case 'event/persisted':
                this.emit('persisted', payload);
                break;
            case 'status/worker':
                {
                    const { value } = payload;
                    this.emit('worker', { status: value, alive: value === 'online' });
                }
                break;
            case 'status/session':
                this.emit('session', payload);
                break;
            case 'session/summary':
                this.emit('summary', payload);
                break;
            case 'data/cleared':
                this.emit('cleared', payload);
                break;
            default:
                break;
        }
    }
    // -- polling fallback --------------------------------------------------
    startPolling() {
        if (this.pollTimer)
            return;
        this.setMode('polling');
        const tick = async () => {
            window.__pw.intervalFires += 1;
            try {
                const [telemetry, pipeline, detections, session] = await Promise.all([
                    fetchJson('/api/telemetry'),
                    fetchJson('/api/pipeline'),
                    fetchJson('/api/detections/latest'),
                    fetchJson('/api/session'),
                ]);
                window.__pw.pollTicks += 1;
                if (telemetry)
                    this.emit('telemetry', telemetry);
                if (pipeline)
                    this.emit('pipeline', pipeline);
                // Every camera's newest boxes, not just whichever camera was last.
                if (detections && detections.cameras && detections.cameras.length) {
                    detections.cameras.forEach((frame) => this.emit('detections', frame));
                }
                else if (detections) {
                    this.emit('detections', detections);
                }
                if (session && session.session)
                    this.emit('session', session.session);
                if (session && session.summary)
                    this.emit('summary', session.summary);
                const health = await fetchJson('/api/health');
                if (health && health.worker)
                    this.emit('worker', health.worker);
                // A newly saved photo, as the live channel would announce it.
                const newest = await fetchJson('/api/events?limit=1&archived=all');
                const event = newest && newest.items && newest.items[0];
                if (event && this.lastEventId !== null && event.id !== this.lastEventId)
                    this.emit('persisted', event);
                if (event)
                    this.lastEventId = event.id;
            }
            catch (error) {
                window.__pw.errors += 1;
                console.error('[pw] poll failed', error);
            }
        };
        tick();
        this.pollTimer = setInterval(tick, POLL_INTERVAL_MS);
        this.sseRetryTimer = setInterval(() => {
            if (!this.source)
                this.connectSse();
        }, SSE_RETRY_MS);
    }
    stopPolling() {
        if (this.pollTimer) {
            clearInterval(this.pollTimer);
            this.pollTimer = null;
        }
        if (this.sseRetryTimer) {
            clearInterval(this.sseRetryTimer);
            this.sseRetryTimer = null;
        }
    }
    setMode(mode) {
        this.mode = mode;
        window.__pw.mode = mode;
        this.emit('mode', mode);
    }
    stop() {
        if (this.source)
            this.source.close();
        this.stopPolling();
        clearTimeout(this.reconnectTimer);
    }
}
// A plain setInterval for low-frequency data, kept separate from the push
// channel. §3.5.1's Selenium test counts these fires.
export function poll(fn, intervalMs) {
    const wrapped = async () => {
        window.__pw.intervalFires += 1;
        try {
            await fn();
        }
        catch (error) {
            window.__pw.errors += 1;
            console.error('[pw] scheduled task failed', error);
        }
    };
    wrapped();
    return setInterval(wrapped, intervalMs);
}
export async function fetchJson(url, options = {}) {
    const controller = new AbortController();
    // A field link can hang rather than fail. Without a timeout the page would
    // wait forever on a request that will never answer, which is precisely the
    // API-timeout condition §3.5.1 asks Selenium to watch for.
    const timer = setTimeout(() => {
        controller.abort();
        window.__pw.timeouts += 1;
    }, options.timeoutMs || 8000);
    try {
        const response = await fetch(url, { ...options, signal: controller.signal });
        if (response.status === 204 || response.status === 304)
            return null;
        if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${response.status}`);
        }
        return (await response.json());
    }
    finally {
        clearTimeout(timer);
    }
}
export async function postJson(url, body = {}, extraHeaders = {}) {
    const headers = { 'Content-Type': 'application/json', ...extraHeaders };
    const response = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok)
        throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
}
export async function patchJson(url, body = {}, extraHeaders = {}) {
    const headers = { 'Content-Type': 'application/json', ...extraHeaders };
    const response = await fetch(url, { method: 'PATCH', headers, body: JSON.stringify(body) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok)
        throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
}
function safeParse(text) {
    try {
        return JSON.parse(text);
    }
    catch (error) {
        window.__pw.errors += 1;
        return null;
    }
}
