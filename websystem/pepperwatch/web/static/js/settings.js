// The Settings page: the simple or full view, display preferences, detection
// thresholds, how often photos are saved, locale, and clearing stored detections.
//
// The confidence slider matters more than it looks. Figure 3.3 specifies a
// fixed "Confidence Filter (80)", but 0.80 on small aphid clusters would
// discard most true detections, and §4 counts False Negatives -- infestations
// missed entirely -- as a headline metric. Exposing the threshold lets the
// value be chosen from a measured precision/recall sweep instead of asserted,
// which is a stronger result to defend.
import { fetchJson, patchJson, postJson } from './bus.js';
import { errorText, num } from './fmt.js';
import { displayPrefs, setDisplayPref } from './liveview.js';
// The admin PIN unlocks thresholds, photo saving and clearing data, for this
// tab: kept in sessionStorage so a reload stays unlocked, and in memory where
// storage is blocked.
const PIN_KEY = 'pw-admin-pin';
let pin = '';
// Set by bindLock: locks the cards again when the Pi refuses a kept PIN.
let pinRefused = () => { };
function adminHeaders() {
    return pin ? { 'X-Admin-Pin': pin } : {};
}
function keepPin(value) {
    pin = value;
    try {
        if (value)
            window.sessionStorage.setItem(PIN_KEY, value);
        else
            window.sessionStorage.removeItem(PIN_KEY);
    }
    catch (error) {
        // Blocked storage: kept in memory until the page closes.
    }
}
// Asks the Pi; changes nothing. Returns null when the PIN is right, or what to say.
async function checkPin(candidate) {
    try {
        const response = await fetch('/api/admin/unlock', { method: 'POST', headers: { 'X-Admin-Pin': candidate } });
        if (response.ok)
            return null;
        if (response.status === 429)
            return 'Too many wrong tries. Wait a minute, then try again.';
        return candidate ? "That PIN isn't right. Check it and try again." : 'Type the PIN first.';
    }
    catch (error) {
        return `Could not reach the Pi: ${errorText(error)}`;
    }
}
function bindLock(root, required) {
    const fields = [...root.querySelectorAll('[data-pin-fields]')];
    const chips = [...root.querySelectorAll('[data-lock-chip]')];
    const bar = root.querySelector('[data-pin-bar]');
    const dialog = root.querySelector('[data-pin-dialog]');
    const form = root.querySelector('[data-pin-form]');
    const input = root.querySelector('[data-pin-input]');
    const message = root.querySelector('[data-pin-error]');
    const submit = root.querySelector('[data-pin-submit]');
    const show = (locked, arriving = false) => {
        fields.forEach((set) => {
            set.disabled = locked;
            set.classList.remove('is-unlocking');
            if (arriving && !locked) {
                void set.offsetWidth; // restart the settle animation
                set.classList.add('is-unlocking');
            }
        });
        chips.forEach((chip) => {
            chip.hidden = !locked;
        });
        if (!bar)
            return;
        bar.classList.toggle('is-unlocked', !locked);
        bar.querySelector('[data-lock-icon]')?.setAttribute('href', locked ? '#i-lock' : '#i-lock-open');
        const title = bar.querySelector('[data-lock-title]');
        const note = bar.querySelector('[data-lock-note]');
        if (title)
            title.textContent = locked ? 'Locked: detection, photo saving and data' : 'Unlocked in this tab';
        if (note) {
            note.textContent = locked
                ? 'Anyone can see these settings. Changing them needs the admin PIN.'
                : 'You can change the settings below. Closing the tab, or Lock again, locks them.';
        }
        bar.querySelector('[data-unlock]')?.toggleAttribute('hidden', !locked);
        bar.querySelector('[data-relock]')?.toggleAttribute('hidden', locked);
    };
    if (!required) {
        // No PIN set up on this Pi (a development checkout): nothing to unlock.
        if (bar)
            bar.hidden = true;
        show(false);
        return;
    }
    if (bar)
        bar.hidden = false;
    let kept = '';
    try {
        kept = window.sessionStorage.getItem(PIN_KEY) || '';
    }
    catch (error) {
        // Not kept: asked for again.
    }
    pin = kept;
    show(!kept);
    // A PIN kept from earlier in this tab may have been changed on the Pi since.
    if (kept) {
        checkPin(kept).then((problem) => {
            if (problem) {
                keepPin('');
                show(true);
            }
        });
    }
    pinRefused = () => {
        keepPin('');
        show(true);
    };
    const say = (text) => {
        if (message)
            message.textContent = text;
        input?.setAttribute('aria-invalid', text ? 'true' : 'false');
    };
    root.querySelectorAll('[data-unlock]').forEach((button) => {
        button.addEventListener('click', () => {
            if (!dialog || !input)
                return;
            input.value = '';
            say('');
            dialog.showModal();
            input.focus();
        });
    });
    root.querySelector('[data-pin-cancel]')?.addEventListener('click', () => dialog?.close());
    root.querySelector('[data-relock]')?.addEventListener('click', () => {
        keepPin('');
        show(true);
    });
    form?.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!input || !dialog)
            return;
        const candidate = input.value.trim();
        if (submit)
            submit.disabled = true;
        const problem = await checkPin(candidate);
        if (submit)
            submit.disabled = false;
        if (problem) {
            say(problem);
            input.classList.remove('is-wrong');
            void input.offsetWidth;
            input.classList.add('is-wrong');
            input.value = ''; // typed again, rather than edited blind
            input.focus();
            return;
        }
        keepPin(candidate);
        dialog.close();
        show(false, true);
    });
}
export async function initSettings(root) {
    bindDisplayPrefs(root);
    bindClear(root);
    const config = await fetchJson('/api/config');
    if (!config)
        return;
    bindLock(root, config.pin_required === true);
    const showConf = bindSlider(root, 'conf', config.inference.conf_threshold);
    const showIou = bindSlider(root, 'iou', config.inference.iou_threshold);
    bindReset(root, config.inference.defaults, () => {
        showConf(config.inference.defaults.conf_threshold);
        showIou(config.inference.defaults.iou_threshold);
    });
    bindIntervals(root, config.persist);
    bindSite(root, config.site.name);
    const localeSelect = root.querySelector('[data-setting-locale]');
    if (localeSelect) {
        localeSelect.innerHTML = (config.locales || ['en'])
            .map((code) => `<option value="${code}">${code === 'ceb' ? 'Cebuano' : 'English'}</option>`)
            .join('');
        localeSelect.value = localStorage.getItem('pw-locale') || 'en';
        localeSelect.addEventListener('change', () => {
            localStorage.setItem('pw-locale', localeSelect.value);
            window.location.reload();
        });
    }
}
// The farm's name: on every page and on each new photo. No PIN; anyone may.
function bindSite(root, initial) {
    const form = root.querySelector('[data-site-form]');
    const input = root.querySelector('[data-setting-site]');
    const status = root.querySelector('[data-site-status]');
    if (!form || !input || !status)
        return;
    input.value = initial;
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = form.querySelector('button');
        if (button)
            button.disabled = true;
        status.className = 'note';
        try {
            const { name } = await patchJson('/api/site', { name: input.value });
            input.value = name;
            document.querySelectorAll('[data-site-name]').forEach((heading) => {
                heading.textContent = name;
            });
            document.title = `${document.title.split(' · ')[0]} · ${name}`;
            status.textContent = 'Saved. Other phones show the new name when they next open the page.';
        }
        catch (error) {
            status.textContent = `Not saved: ${errorText(error)}`;
            status.className = 'note sev-high';
        }
        finally {
            if (button)
                button.disabled = false;
        }
    });
}
// Saved in this browser and read by the dashboard when it next opens.
function bindDisplayPrefs(root) {
    const themeSelect = root.querySelector('[data-setting-theme]');
    const theme = window.pwTheme;
    if (themeSelect && theme) {
        themeSelect.value = theme.get();
        themeSelect.addEventListener('change', () => theme.set(themeSelect.value));
    }
    // Simple or full view: applied at once, so the cards below show or hide.
    const view = root.querySelector('[data-setting-view]');
    const pwView = window.pwView;
    if (view && pwView) {
        view.querySelectorAll('input[name="view"]').forEach((input) => {
            input.checked = input.value === pwView.get();
            input.addEventListener('change', () => {
                const note = root.querySelector('[data-view-note]');
                if (note)
                    note.hidden = pwView.set(input.value);
            });
        });
    }
    const prefs = displayPrefs();
    root.querySelectorAll('[data-pref]').forEach((input) => {
        const key = input.dataset.pref ?? '';
        input.checked = prefs[key] === true;
        input.addEventListener('change', () => setDisplayPref(key, input.checked));
    });
}
// Two steps, inline: a pop-up confirm is easy to dismiss by reflex on a phone.
function bindClear(root) {
    const start = root.querySelector('[data-clear-start]');
    const confirm = root.querySelector('[data-clear-confirm]');
    const status = root.querySelector('[data-clear-status]');
    const yes = root.querySelector('[data-clear-yes]');
    if (!start || !confirm || !status || !yes)
        return;
    start.addEventListener('click', () => {
        confirm.hidden = false;
        start.hidden = true;
    });
    root.querySelector('[data-clear-no]')?.addEventListener('click', () => {
        confirm.hidden = true;
        start.hidden = false;
    });
    yes.addEventListener('click', async () => {
        yes.disabled = true;
        try {
            const result = await postJson('/api/data/clear', { confirm: 'DELETE' }, adminHeaders());
            status.textContent = `Deleted ${result.deleted_events} detections and ${result.deleted_files} image files.`;
        }
        catch (error) {
            if (/PIN/.test(errorText(error)))
                pinRefused();
            status.textContent = `Could not clear: ${errorText(error)}`;
        }
        finally {
            yes.disabled = false;
            confirm.hidden = true;
            start.hidden = false;
        }
    });
}
// Returns how to show a value the detector has taken, set from elsewhere.
function bindSlider(root, key, initial) {
    const slider = root.querySelector(`[data-slider-${key}]`);
    const readout = root.querySelector(`[data-slider-${key}-value]`);
    const status = root.querySelector('[data-settings-status]');
    if (!slider)
        return () => { };
    let applied = initial;
    const show = (value) => {
        applied = value;
        slider.value = String(value);
        if (readout)
            readout.textContent = num(value, 2);
    };
    show(initial);
    slider.addEventListener('input', () => {
        if (readout)
            readout.textContent = num(Number(slider.value), 2);
    });
    // Commit on release rather than on every input event: dragging the slider
    // would otherwise fire a PATCH per pixel, and each one republishes to the
    // worker over MQTT.
    slider.addEventListener('change', async () => {
        const field = key === 'conf' ? 'conf_threshold' : 'iou_threshold';
        if (await applySetting({ [field]: Number(slider.value) }, status)) {
            applied = Number(slider.value);
        }
        else {
            show(applied); // what the detector is really using, not the value that failed
        }
    });
    return show;
}
// Both thresholds back to the values in the Pi's config files, in one change.
function bindReset(root, defaults, onApplied) {
    const button = root.querySelector('[data-thresholds-reset]');
    const note = root.querySelector('[data-thresholds-defaults]');
    if (!button)
        return;
    if (note) {
        note.textContent = `Confidence ${num(defaults.conf_threshold, 2)} and NMS IoU ${num(defaults.iou_threshold, 2)}, as set in the Pi's config files.`;
    }
    button.addEventListener('click', async () => {
        button.disabled = true;
        if (await applySetting(defaults, root.querySelector('[data-settings-status]')))
            onApplied();
        button.disabled = false;
    });
}
function bindIntervals(root, initial) {
    const status = root.querySelector('[data-interval-status]');
    root.querySelectorAll('[data-setting-interval]').forEach((select) => {
        const field = select.dataset.settingInterval;
        let applied = initial[field];
        // A value set in the config files may not be one of the choices; show it
        // anyway, so the page never claims an interval the Pi is not using.
        if (![...select.options].some((option) => Number(option.value) === applied)) {
            select.add(new Option(`${num(applied, 0)} seconds`, String(applied)));
        }
        select.value = String(applied);
        select.addEventListener('change', async () => {
            if (await applySetting({ [field]: Number(select.value) }, status))
                applied = Number(select.value);
            else
                select.value = String(applied);
        });
    });
}
// Sends one change and says whether the worker took it.
async function applySetting(changes, status) {
    if (status) {
        status.textContent = 'Applying…';
        status.className = 'note';
    }
    try {
        // Answers only once the worker reports the new value, a second or two.
        await patchJson('/api/config', changes, adminHeaders());
        if (status)
            status.textContent = 'Applied: the detector is using it now, and keeps it after restarts.';
        return true;
    }
    catch (error) {
        if (/PIN/.test(errorText(error)))
            pinRefused();
        if (status) {
            status.textContent = `Not applied: ${errorText(error)}`;
            status.className = 'note sev-high';
        }
        return false;
    }
}
