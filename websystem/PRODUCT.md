# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **Primary: smallholder bell pepper farmers.** They read the dashboard on their own phone, standing in the pepper row, after joining the Pi's Wi-Fi. They read it in Cebuano or English. Their job is to know whether the plants are being damaged by aphids and what to do about it, without needing technical knowledge. The simple view is the default and is built for them.
- **Second: the agricultural officer or DA/ATI technician.** They install and update the Pi, and use the full (expert) view, chosen in Settings, for detection detail, thresholds, telemetry, sessions and exports.
- **Third: the thesis researchers and panel.** They judge the system on the measurements it records.

## Product Purpose

Pepper Watch detects aphid-induced leaf damage on bell pepper (*Capsicum annuum*) in real time. A camera points at the canopy and a Raspberry Pi 5 runs a YOLOv8 model with no internet. The Pi tells the farmer how the plants are and what to do, and records every run as a session for the thesis results chapter.

Success is two things, weighted equally:
- Farmers catch aphid damage early and know what to do, offline.
- The same system yields trustworthy, reproducible measurements: detection quality, inference speed and power, per session.

## Positioning

Everything runs on the farm. The Pi broadcasts its own Wi-Fi hotspot with a captive portal, and any phone that joins gets the dashboard. There is no internet, cloud, app install, account or keyboard. If the Pi can join a saved Wi-Fi network instead, it does.

It is honest about what it knows:
- No advice without photos.
- No results from a missing model (stub detections are excluded and flagged).
- Stale readings are dimmed and dated.
- A CPU result is never presented as an AI HAT+ result.

## Operating Context

- **Deployment:** season-long, on a smallholder plot. The configured site is Naawan, in Mindanao. It runs unattended between technician visits.
- **Hardware:**
  - A Raspberry Pi 5 with one to four USB or CSI cameras, and an optional AI HAT+.
  - The cameras are hot-plugged.
  - Power cuts happen, and the clock drifts without an RTC battery.
  - The SD card can fill up.
- **Phones:**
  - They are read in direct tropical sun.
  - Several phones share the Pi's single radio with the live video.
  - Android and iOS may drop a Wi-Fi network that has no internet.
- **Protection:** an admin PIN guards destructive actions, namely clearing data and changing thresholds.
- **The technician's work:** they deploy and update over SSH (`deploy/update.sh`). They export detections as CSV, or as a ZIP with the JPEG frames, for analysis.

## Capabilities and Constraints

**Capabilities**
- Live camera view with detection boxes, one view per camera. Tap a camera to expand it.
- The farmer status card: the typical severity of the last 24 hours, falling back to 7 days, with what to do.
- A week summary and a history gallery with scan details.
- Sessions with CSV records, plus CSV/ZIP export.
- Settings: theme, view, admin PIN and thresholds.
- Banners that say what is wrong, such as camera unplugged, detector offline, storage low or clock off.

**Constraints (binding)**
- **Offline only.** Nothing loads from a CDN or the internet; fonts and icons ship with the app. Every byte shares one Wi-Fi radio with the video.
- **Sunlight first.** The page is read on a phone in direct sun. The light theme is the default and contrast stays high; dark is for dusk and indoors.
- **English + Cebuano.** Every farmer-facing phrase exists in both languages.
- **No chemical advice.** Advice never names chemicals, products or doses. It covers monitoring and thresholds, and escalates to naming a person to call (the DA/ATI technician).

**Terminology**
- "Aphid damage" means damaged leaves; the model detects damage, not the insects.
- The severity bands are none, low, moderate and high.
- Other terms: "session", "simple view", "full view".

**Open**
- The advisory content is a draft until an agronomist validates it (`config/recommendations.json`, `validated_by` is null). The dashboard shows it as a draft until then.
- The Cebuano wording still needs a native speaker's review.

## Brand Commitments

- The name is **Pepper Watch**.
- The simple view uses plain words with no technicalities; expert detail lives in the full view.

## Evidence on Hand

- **Training dataset:** `Pepper Watch Thesis.v10-v10.yolov8/` (YOLOv8 export; local, gitignored).
- **Field photos:** kept in the git tag `datasets-v1`, under `datasets/`.
- **Trained model:** kept outside git (`models/`).
- **Session CSVs and benchmark scripts:** in `benchmarks/` and `scripts/`.
- **Not yet available, so never fabricate it:**
  - field-trial results;
  - farmer feedback or testimonials;
  - usability scores;
  - agronomist validation of the advice.

## Product Principles

1. **Say only what the camera saw.** Show no claim without photos behind it, no advice from a missing model, and no stale reading presented as live.
2. **Farmers first, experts one switch away.** The simple view is the default. Expert detail is moved behind the switch, never removed.
3. **Works with nothing.** The system recovers from power cuts, unplugged cameras, a full card and a lost network without a technician on site.
4. **Every byte crosses one radio.** Weight on the page is weight taken from the live picture.
5. **Advice points to people, not products.**

## Accessibility & Inclusion

- **Legibility in the sun:** text is at least 4.5:1 and large units at least 3:1, in both themes.
- **Language:** bilingual (English and Cebuano).
- **Phone first:** the dashboard is built for phone-sized screens and touch.
- **Motion:** reduced-motion preferences are respected.
