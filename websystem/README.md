# Pepper Watch — web system

Offline edge-IoT dashboard for the thesis *Pepper Watch: detecting aphid-induced
leaf damage in bell pepper (Capsicum annuum) on smallholder farms*.

A Raspberry Pi 5 runs YOLOv8n over a camera pointed at the canopy, broadcasts
its own Wi-Fi access point, and serves a dashboard to any phone that joins — no
internet, no cloud, no keyboard.

Implements manuscript Chapter III: Table 3.1 (functional requirements),
Table 3.4 (the five interface components), Figure 3.3 (the pipeline),
Figure 3.4 (the dashboard), and §3.5 (testing).

## Quick start on macOS

Everything runs on a development Mac against the Naawan field photographs, with
no Pi and no camera.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

brew install mosquitto

# Builds the mock frames from ~300 field photos on first run, then starts the
# broker, the worker (camera + inference) and the dashboard on :8000
./scripts/dev.sh
```

Then open <http://localhost:8000>. Check the setup at any time with:

```bash
.venv/bin/python -m pepperwatch.doctor
```

## How it fits together

Three processes and a broker, all on localhost:

| Process | Role |
|---|---|
| `mosquitto` | Broker bound to `127.0.0.1` only |
| `pepperwatch-worker` | Owns the camera. Capture → letterbox → infer → NMS → publish → persist |
| `pepperwatch-web` | Flask. MJPEG + SSE + REST + pages + captive-portal probes |

Two design decisions carry most of the weight.

**Preview and inference run at different rates.** §3.2 asks for ≥17 FPS, but an
INT8 YOLOv8n on a Pi 5 CPU at 640×640 realistically delivers 5–10. A capture
thread runs the camera flat out into a single-slot buffer while the inference
thread always takes the newest frame, so the live view stays smooth regardless
and the two numbers are reported separately. See *Known divergences* below.

**Frames move through shared memory, not files or MQTT.** `frames.py` publishes
each JPEG into one of two shared-memory slots behind a seqlock: the writer fills
the idle slot and bumps the header last, and a reader re-checks the sequence
after copying. No locks, so a phone on a weak link can never stall the inference
loop. MQTT carries only small JSON; images go over HTTP. That is exactly the
dual-protocol split §3.3.2.1 describes.

## Layout

```
config/          default.toml, recommendations.json (advisory text, en + ceb)
pepperwatch/
  config.py      TOML -> local.toml -> PW_* env, with type coercion
  db.py          SQLite in WAL mode; schema.sql has the three §3.3.2.1 entities
  frames.py      shared-memory seqlock + tmpfs frame transport
  worker.py      the two-thread pipeline
  thermal.py     inference backoff above 75 °C
  advisory.py    Actionable Output Panel content
  doctor.py      preflight diagnostics
  hal/           mock.py (field-photo replay) | pi.py (Picamera2 / USB, INA219)
  detect/        postprocess.py, onnx_detector.py, stub_detector.py
  web/           Flask app, ES6 modules, Jinja templates
deploy/          systemd units, mosquitto conf, NetworkManager AP, install.sh
scripts/         prepare_mock_frames.py, bench_fps.py, check_js.sh
tests/           281 unit/API tests + 19 Selenium
```

## Configuration

`config/default.toml` holds every tunable. Override in `config/local.toml`
(gitignored) or by environment variable, built from the table path:

```bash
PW_SITE_NAME="Buru-un" PW_INFERENCE_CONF_THRESHOLD=0.4 python -m pepperwatch.worker
```

Values are coerced to the type of the default they replace, so a bad value fails
at startup rather than halfway through a field trial.

## Testing

```bash
.venv/bin/python -m pytest              # 281 tests, ~1 min
.venv/bin/python -m pytest -m selenium  # 19 browser tests, ~4min, needs Chrome
./scripts/check_js.sh                   # ES module syntax (there is no build step)
```

Tests that map directly onto §3.5.1's stated requirements:

| §3.5.1 requirement | Test |
|---|---|
| Mock JSON payloads → HTTP success → boxes in SQLite | `test_api.py::test_mock_json_payload_writes_bounding_boxes` |
| No WAL concurrency failures under a live writer | `test_db_concurrency.py` (8 reader processes vs 1 writer) |
| `setInterval` keeps fetching | `test_ui_selenium.py::test_setinterval_keeps_firing` |
| No manual refresh needed | `test_ui_selenium.py::test_page_updates_without_a_manual_refresh` |
| No memory leaks | `test_ui_selenium.py::test_no_memory_leak_over_repeated_updates` |
| No webpage freezing | `test_ui_selenium.py::test_page_stays_responsive_under_load` |
| No API timeouts | `test_ui_selenium.py::test_no_api_timeouts_or_handler_errors` |

## Benchmarking

`scripts/bench_fps.py` is the canonical implementation of §3.5.1's "Python time
module embedded within the YOLOv8n execution loop". It times each stage
separately and writes CSV straight into `benchmarks/`:

```bash
.venv/bin/python scripts/bench_fps.py --sizes 320,416,640 --threads 1,2,3,4 --seconds 30
```

It reports three different things all called "FPS", because conflating them is
how a paper ends up defending a number it never measured:

- `capture_fps` — camera throughput, bounded by the sensor
- `inference_fps` — the model alone. **This is what the 17 FPS requirement means**
- `pipeline_fps` — the whole loop. Quote this one if you quote only one

## Deploying to the Pi

**Install Raspberry Pi OS (64-bit).** 64-bit is not optional: ONNX Runtime
publishes no 32-bit ARM wheels, and PyPI does not even support the `armv7l`
platform tag. `install.sh` refuses to continue on a 32-bit userland.

Lite is the deployment image. The desktop image also works and is handy on a
bench machine, but its session competes with inference for CPU and adds heat,
so boot it to the console before measuring anything (see *Before a field
trial*).

```bash
sudo ./deploy/install.sh
sudo systemctl start pepperwatch.target
```

### Board profiles

`config/default.toml` is sized for a **Pi 5**. Smaller boards get a profile
merged over it, chosen from `/proc/device-tree/model`:

```
config/default.toml -> config/profiles/<board>.toml -> config/local.toml -> PW_* env
```

`deploy/install.sh` also installs matching systemd drop-ins from
`deploy/systemd/<board>/`. Force a profile for testing with `PW_BOARD=pi3`.

**Pi 3** (`config/profiles/pi3.toml`, `deploy/systemd/pi3/`) — a 1 GB board with
a Cortex-A53, against the Pi 5's 8–16 GB and Cortex-A76:

| | Pi 5 default | Pi 3 |
|---|---|---|
| `inference.input_size` | 640 | **320** — 640 is about 1 FPS on an A53 |
| `inference.intra_op_threads` | 3 | **2** |
| `camera.preview_fps` | 25 | **15** |
| `frames.slot_bytes` | 512 KB | **256 KB** (2.8× headroom on the largest test frame) |
| `web.max_stream_clients` | 4 | **2** — single-antenna 2.4 GHz radio |
| worker `MemoryMax` | 2 G | **460 M** |
| web `MemoryMax` | 1 G | **200 M** |

`inference.target_fps` stays at **17** on every board. It is the §3.2
requirement, not a tuning knob, and the dashboard reporting "below the 17 fps
target" on a Pi 3 is correct rather than something to paper over.

The memory limits are the part that matters. The Pi 5 ceilings total 3 G on a
board with roughly 940 MB visible — **3.4× physical RAM**, so they never engage
and the kernel's OOM killer fires instead, picking its victim by heuristic.
`python -m pepperwatch.doctor` fails on this, and `install.sh` warns when a
board under 4 GB has no drop-ins.

A Pi 3 is a development and integration target. **Its frame rates are not
Chapter IV data** — those must come from the Pi 5. The A53 is ARMv8.0 and lacks
the dot-product instructions the A76 has, so INT8 quantization buys less there
too. Note also that the Pi 3 has no RTC connector at all (the Pi 5 has J5), so
an external DS3231 is the only way to get correct timestamps across a power cut.

Then join the `PepperWatch` network. The dashboard should open by itself.

The access point uses NetworkManager (`ipv4.method shared`), **not** the
hostapd + dnsmasq recipe in most Raspberry Pi tutorials — those predate
Bookworm and fight NetworkManager for the interface. Captive-portal behaviour
comes from two mechanisms: RFC 8910 DHCP option 114 for current devices, and
OS probe responses (`/generate_204`, `/hotspot-detect.html`, `/ncsi.txt`) for
everything else.

### Before a field trial

- **Boot to the console** if the Pi runs the desktop image. §3.2.2 specifies a
  headless system, and the desktop session costs CPU and thermal headroom that
  shows up directly in the FPS figures. The desktop stays installed; it just
  doesn't start:
  ```bash
  sudo systemctl set-default multi-user.target && sudo reboot
  # back to the desktop: sudo systemctl set-default graphical.target
  ```
  Also disable autoconnect on any Wi-Fi network saved through the desktop, or it
  will compete with `pepperwatch-ap` for `wlan0`:
  `nmcli connection modify "<name>" connection.autoconnect no`
- **Fit the RTC battery** (Pi 5 J5 connector). Offline there is no NTP, so
  without it the Pi boots to a meaningless date and every timestamp in the
  Historical Detection Log is wrong. `doctor` checks for this.
- **Drop in `models/pepperwatch.onnx`.** Without one the heuristic stub runs
  instead; its output is flagged `synthetic` everywhere and must not be
  reported as a measurement.
- **Get `config/recommendations.json` signed off** by an agriculturist. Until
  `meta.status` reads `validated` the dashboard shows a draft banner on all
  treatment advice.
- Consider a **USB SSD** instead of the SD card. Eight hours a day of image
  writes plus WAL fsync is the single most likely hardware failure.

## Known divergences from the manuscript

Each of these is a deliberate choice with a reason, not an oversight. They are
listed here so they can be defended or the manuscript amended.

| Manuscript | Built | Why |
|---|---|---|
| Figure 3.3: "Confidence Filter (80)" | Configurable, default **0.25** | 0.80 on clusters this small discards most true detections, and §4 counts False Negatives as a headline metric. Sweep the slider and report a precision/recall curve instead. |
| §3.2: ≥17 FPS | Measured and reported, not assumed | Published INT8 YOLOv8n figures on Pi 5 CPU are 5–10 FPS at 640×640. The pipeline decouples preview from inference and reports both honestly. |
| Figure 3.4 donut: Sick / Damaged / Healthy | **healthy / aphid_damage** | Matches Table 3.1, §4's binary confusion matrix, and the research aim's wording. Severity is derived in software instead. |
| Severity bands | Defined in `[severity]` config | Ch. III never defines them, but Figure 3.4 draws a Severity tile. Rises on cluster count **or** affected area, whichever is worse. |
| §3.3: boxes rendered "onto a localized physical display" | Any phone on the WAP | §3.2.2 mandates headless and §3.3.2.2 rejects native GUIs; a TFT plus a kiosk browser would burn exactly the resources those sections claim to save. |
| Figure 3.6: MongoDB | SQLite | Every prose section says SQLite. The figure is stale. |
| Figure 3.3: "Web Camera" | Both supported | Table 3.2 says MIPI CSI. `[camera].kind` selects either. |
| Nothing stated | Persist policy, PIN gate, RTC handling | 17 fps × 8 h is ~490k frames/day; an unauthenticated AP lets anyone pause a trial; no NTP means no valid clock. All three needed solving. |

The full list, including items that need fixing in the manuscript itself rather
than in code, is kept in `MANUSCRIPT-NOTES.md` alongside the manuscript (not in
this repository).
