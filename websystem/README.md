# Pepper Watch — web system

Offline edge-IoT dashboard for the thesis *Pepper Watch: detecting aphid-induced
leaf damage in bell pepper (Capsicum annuum) on smallholder farms*.

A Raspberry Pi 5 runs YOLOv8n over a camera pointed at the canopy, on its CPU
or on a Raspberry Pi AI HAT+ when one is fitted, broadcasts its own Wi-Fi access
point, and serves a dashboard to any phone that joins: no internet, no cloud, no
keyboard. Every run is recorded as a session of CSV files for the results
chapter.

Implements manuscript Chapter III: Table 3.1 (functional requirements),
Table 3.4 (the five interface components), Figure 3.3 (the pipeline),
Figure 3.4 (the dashboard), and §3.5 (testing).

The repository's top-level `README.md` is the setup and operations guide. This
file holds the engineering notes.

## How it fits together

Three processes and a broker, all on the Pi:

| Process | Role |
|---|---|
| `mosquitto` | Broker bound to `127.0.0.1` only |
| `pepperwatch-worker` | Detects the camera and AI HAT+ per session, then capture → letterbox → infer → NMS → publish → persist → record CSV |
| `pepperwatch-web` | Flask. MJPEG + SSE + REST + pages + captive-portal probes + session downloads |

Three design decisions carry most of the weight.

**Preview and inference run at different rates.** §3.2 asks for ≥17 FPS, but an
INT8 YOLOv8n on a Pi 5 CPU at 640×640 realistically delivers 5–10. A capture
thread runs the camera flat out into a single-slot buffer while the inference
thread always takes the newest frame, so the live view stays smooth regardless
and the two numbers are reported separately. See *Known divergences* below.

**The hardware decides the profile, once per session.** `accel.py` looks for an
AI HAT+ on the PCIe bus and checks its driver, runtime and compiled HEF; the
session then runs on the HAT or the CPU and applies `[profiles.hailo]` or
`[profiles.cpu]`. Every fallback carries a reason, recorded in `session.json`
and shown on the dashboard, so a CPU result can never be mistaken for a HAT one.

**Frames move through shared memory, not files or MQTT.** `frames.py` publishes
each JPEG into one of two shared-memory slots behind a seqlock: the writer fills
the idle slot and bumps the header last, and a reader re-checks the sequence
after copying. No locks, so a phone on a weak link can never stall the inference
loop. MQTT carries only small JSON; images go over HTTP. That is exactly the
dual-protocol split §3.3.2.1 describes.

## Layout

```
config/          default.toml (with [profiles.cpu|hailo]), recommendations.json
pepperwatch/
  config.py      TOML -> profile -> local.toml -> PW_* env, with type coercion
  accel.py       AI HAT+ detection and the per-session CPU/Hailo choice
  session.py     per-session CSV recorder and the cross-session index
  db.py          SQLite in WAL mode; schema.sql has the three §3.3.2.1 entities
  frames.py      shared-memory seqlock + tmpfs frame transport
  worker.py      the two-thread pipeline
  thermal.py     inference backoff above 75 °C
  advisory.py    treatment-advice content (the Leaf health caption, /api/recommendations)
  doctor.py      preflight diagnostics
  hal/           discovery.py (CSI + USB cameras), pi.py (drivers, telemetry),
                 power.py (Pi 5 PMIC, throttle flags, fan)
  detect/        postprocess.py, hailo_detector.py, onnx_detector.py, stub_detector.py
  web/           Flask app, ES6 modules, Jinja templates
deploy/          install.sh, update.sh, systemd units, mosquitto conf, NetworkManager AP
scripts/         bench_fps.py (--accelerator cpu|hailo), check_js.sh
tests/           344 hardware-free unit and API tests
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

The suite needs no camera, HAT or Pi, so it runs on any machine with Python
3.11+, and CI runs it on x64 and arm64 for every push:

```bash
.venv/bin/python -m pytest              # 344 tests, ~1 min
./scripts/check_js.sh                   # ES module syntax (there is no build step)
```

Tests that map directly onto §3.5.1's stated requirements:

| §3.5.1 requirement | Test |
|---|---|
| Mock JSON payloads → HTTP success → boxes in SQLite | `test_api.py::test_mock_json_payload_writes_bounding_boxes` |
| No WAL concurrency failures under a live writer | `test_db_concurrency.py` (8 reader processes vs 1 writer) |

§3.5.1's browser requirements (`setInterval` keeps fetching, no manual refresh,
no memory leaks, no freezing, no API timeouts) were covered by a Selenium suite
that drove a mock camera. Both were removed when the system became Pi-only, so
those checks are now manual, on the Pi, with a phone on the access point. If the
manuscript keeps the Selenium claim, it needs automated tests again: a browser
run against the Pi itself would do it without bringing back a mock.

## Benchmarking

`scripts/bench_fps.py` is the canonical implementation of §3.5.1's "Python time
module embedded within the YOLOv8n execution loop". It times each stage
separately and writes CSV straight into `benchmarks/`:

```bash
.venv/bin/python scripts/bench_fps.py --accelerator cpu --sizes 320,416,640 --threads 1,2,3,4 --seconds 30
.venv/bin/python scripts/bench_fps.py --accelerator hailo --seconds 30
```

It reports three different things all called "FPS", because conflating them is
how a paper ends up defending a number it never measured:

- `capture_fps` — camera throughput, bounded by the sensor
- `inference_fps` — the model alone. **This is what the 17 FPS requirement means**
- `pipeline_fps` — the whole loop. Quote this one if you quote only one

For long runs, the session CSV files (`/var/lib/pepperwatch/sessions/`) carry
the same stage split for every inference, plus power and temperatures once a
second.

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

### Boards and tiers

`config/default.toml` is sized for a **Pi 5**. Every other board is classified
by `pepperwatch/board.py` from what Linux already reports: the device-tree
model and `compatible` strings, the ARM core types in `/proc/cpuinfo`, and RAM.
The result is a **tier**, the CPU class capped by memory:

| Tier | Typical boards | Input | Preview | Threads |
|---|---|---|---|---|
| `high` | Pi 5, Pi 500; RK3588 boards (Orange Pi 5, Banana Pi M7) | 640 | 25 fps | 3 |
| `mid` | Pi 4, Pi 400, CM4; RK3399 | 416 | 20 fps | 3 |
| `low` | Pi 3, Orange Pi Zero 2/3, Banana Pi M4/M5, RK3566; any board under 1.5 GB | 320 | 15 fps | 2 |
| `tiny` | Pi Zero 2 W; any board under 768 MB | 320 | 10 fps | 2 |

The core type is what counts, not the vendor: an RK3588's A76 cores put an
Orange Pi 5 in the same tier as a Pi 5. Profiles layer as:

```
config/default.toml -> profiles/<tier>.toml -> profiles/<board>.toml -> config/local.toml -> PW_* env
```

`profiles/<board>.toml` is optional and named by the board key
(`python -m pepperwatch.board` prints it, e.g. `orangepi-zero3`). Force a board
or tier for testing with `PW_BOARD=pi3` or `PW_TIER=low`.

Memory limits are chosen separately, by RAM alone. `install.sh` applies
`deploy/systemd/mem-512m/`, `mem-1g/` or `mem-2g/` below 3 GB; the base units'
2 G + 1 G would otherwise sit above physical memory, never engage, and leave
the kernel's OOM killer to pick a victim by heuristic. `doctor` fails on that.

| RAM | worker `MemoryMax` | web `MemoryMax` |
|---|---|---|
| ≥ 3 GB | 2 G | 1 G |
| 1.5–3 GB (`mem-2g`) | 1000 M | 400 M |
| 768 MB–1.5 GB (`mem-1g`) | 460 M | 200 M |
| < 768 MB (`mem-512m`) | 240 M | 100 M (best effort) |

`inference.target_fps` stays at **17** on every tier. It is the §3.2
requirement, not a tuning knob, and a slower board reporting "below the 17 fps
target" is correct rather than something to paper over.

A slower board is a comparison point, not a stand-in. **Its frame rates are
reported as what that board achieves, beside the Pi 5's**: the A53/A55 cores
lack the A76's dot-product instructions, so INT8 quantization buys less there
too. Only the Pi 5 measures its own power (PMIC), so energy comparisons across
boards need an INA219 on each, or the figures are not like for like. The Pi 3
and most Orange Pi and Banana Pi boards have no RTC battery connector; an
external DS3231 module keeps timestamps correct across a power cut.

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
| Figure 3.4: dark surface | Light surface, after Nija Design's "Smart Farm Monitoring Dashboard" | A light surface with dark ink stays readable in direct sun, where a dark one washes out; every text pairing is at least 4.5:1. Layout, panels and data are unchanged. |
| Table 3.4: Actionable Output Panel | Removed from the dashboard | Cut on request. The advice text still exists (`config/recommendations.json`, `/api/recommendations`) and the Leaf health tile shows its headline. |
| Figure 3.4: "Start Camera" button and Live toggle | Live by default; switches in Settings | The feed opens on page load; Live video, detection boxes and mirroring are per-device settings. |
| Figure 3.4: plain-language insight banner | Removed | It restated the four KPI tiles in a sentence. Cut when the dashboard was trimmed to the reported metrics. |
| Figure 3.4: System Health Monitor (temperature, CPU, memory, storage, battery) | Performance & Power card | Keeps temperature and CPU load; adds inference and camera fps against the target, latency, power with its source, and session energy. Memory, storage and battery (there is none) were cut. |
| Inference on the Pi 5 CPU | CPU, or an AI HAT+ when fitted | Detected per session; the accelerator is recorded with every result so the two are never mixed in Chapter IV. |
| Figure 3.3: "Web Camera" | Both supported | Table 3.2 says MIPI CSI. `[camera].kind` selects either. |
| Nothing stated | Persist policy, PIN gate, RTC handling | 17 fps × 8 h is ~490k frames/day; an unauthenticated AP lets anyone pause a trial; no NTP means no valid clock. All three needed solving. |

The full list, including items that need fixing in the manuscript itself rather
than in code, is kept in `MANUSCRIPT-NOTES.md` alongside the manuscript (not in
this repository).
