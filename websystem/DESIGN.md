---
name: Pepper Watch
description: Offline aphid-damage monitor for bell pepper farms, read on a phone in the sun.
colors:
  leaf-green: "#15803d"
  leaf-green-deep: "#116632"
  leaf-green-soft: "#dcefe2"
  young-olive: "#4d7c0f"
  ochre-warning: "#a16207"
  ochre-ink: "#8a5300"
  ochre-soft: "#fbeccb"
  brick-alarm: "#b42318"
  brick-soft: "#fbe3e0"
  sprout-dot: "#22a355"
  sun-dot: "#f0a20b"
  ember-dot: "#e5484d"
  ash-dot: "#b3b7b3"
  sun-bleached-grey: "#e6e7e4"
  field-paper: "#f6f6f4"
  dust-grey: "#ebece9"
  deep-dust: "#dfe0dc"
  furrow-line: "#d9dad6"
  soil-ink: "#1b1d1c"
  bark-grey: "#4a4f4c"
  stone-grey: "#646a66"
  pebble-grey: "#868b87"
  wet-soil: "#1f2120"
  white: "#ffffff"
typography:
  display:
    fontFamily: "Outfit, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"
    fontSize: "clamp(30px, 6vw, 44px)"
    fontWeight: 500
    lineHeight: 1.05
    letterSpacing: "-0.03em"
  numeral:
    fontFamily: "Outfit, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"
    fontSize: "clamp(34px, 4.4vw, 50px)"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "-0.04em"
    fontFeature: "tnum"
  headline:
    fontFamily: "Outfit, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"
    fontSize: "clamp(24px, 6.5vw, 30px)"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  title:
    fontFamily: "Outfit, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"
    fontSize: "20px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "-0.01em"
  body:
    fontFamily: "Outfit, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
  control:
    fontFamily: "Outfit, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"
    fontSize: "14px"
    fontWeight: 500
    lineHeight: 1.4
  note:
    fontFamily: "Outfit, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.55
  label:
    fontFamily: "Outfit, -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.4
rounded:
  sm: "12px"
  control: "14px"
  md: "18px"
  lg: "22px"
  full: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  xxl: "28px"
components:
  button:
    backgroundColor: "{colors.dust-grey}"
    textColor: "{colors.soil-ink}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
    height: "44px"
  button-hover:
    backgroundColor: "{colors.deep-dust}"
  button-primary:
    backgroundColor: "{colors.wet-soil}"
    textColor: "{colors.white}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "#000000"
  button-danger:
    backgroundColor: "{colors.brick-soft}"
    textColor: "{colors.brick-alarm}"
    rounded: "{rounded.control}"
    padding: "10px 16px"
    height: "44px"
  button-danger-hover:
    backgroundColor: "{colors.brick-alarm}"
    textColor: "{colors.field-paper}"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.bark-grey}"
    rounded: "{rounded.control}"
    padding: "8px 12px"
  input:
    backgroundColor: "{colors.dust-grey}"
    textColor: "{colors.soil-ink}"
    rounded: "{rounded.sm}"
    padding: "9px 12px"
    height: "44px"
  chip:
    backgroundColor: "{colors.field-paper}"
    textColor: "{colors.bark-grey}"
    typography: "{typography.label}"
    rounded: "{rounded.full}"
    padding: "5px 11px"
  badge:
    backgroundColor: "{colors.white}"
    textColor: "{colors.leaf-green-deep}"
    typography: "{typography.label}"
    rounded: "{rounded.full}"
    padding: "3px 10px"
  card:
    backgroundColor: "{colors.field-paper}"
    textColor: "{colors.soil-ink}"
    rounded: "{rounded.lg}"
  tile:
    backgroundColor: "{colors.field-paper}"
    textColor: "{colors.soil-ink}"
    rounded: "{rounded.lg}"
    padding: "18px 18px 16px"
  field-health:
    backgroundColor: "{colors.leaf-green}"
    textColor: "{colors.white}"
    rounded: "{rounded.lg}"
    padding: "22px 22px 20px"
  field-health-low:
    backgroundColor: "{colors.young-olive}"
  field-health-moderate:
    backgroundColor: "{colors.ochre-warning}"
  field-health-high:
    backgroundColor: "{colors.brick-alarm}"
  banner:
    backgroundColor: "{colors.field-paper}"
    textColor: "{colors.bark-grey}"
    typography: "{typography.control}"
    rounded: "{rounded.md}"
    padding: "13px 16px"
  banner-caution:
    backgroundColor: "{colors.ochre-soft}"
    textColor: "{colors.ochre-ink}"
  banner-alarm:
    backgroundColor: "{colors.brick-soft}"
    textColor: "{colors.brick-alarm}"
  nav-item:
    backgroundColor: "{colors.dust-grey}"
    textColor: "{colors.soil-ink}"
    rounded: "{rounded.md}"
    size: "56px"
  nav-item-active:
    backgroundColor: "{colors.wet-soil}"
    textColor: "{colors.white}"
  choice:
    backgroundColor: "{colors.dust-grey}"
    rounded: "{rounded.md}"
    padding: "16px"
  choice-selected:
    backgroundColor: "{colors.leaf-green-soft}"
---

# Design System: Pepper Watch

## Overview

**Creative North Star: "The Quiet Watchman"**

Pepper Watch is a calm, grey panel that keeps watch over a pepper row and raises its voice only when the plants need it. At rest it is almost colourless. Soft paper tiles float a little above a sun-bleached grey canvas, with dark soil ink and large, light numerals. One block carries saturated colour, the field-health card, and that colour *is* the reading: leaf green when the crop is clean, moving through young olive and ochre to brick red as the damage rises. Everything else stays neutral, so the reading is the first thing anyone sees from the end of a row.

The mood is calm, plain and trustworthy. The page is read on a phone in direct tropical sun, often by a farmer with no technical background. So the light theme is the default, contrast stays high in both themes, and nothing on the page is decorative. Colour, motion and red all appear only when they carry a reading or a failure. The simple view (the farmers' default) uses larger type and larger targets. The full view adds the expert detail in the same visual language.

The system is rejected in these forms:
- a dark-first "tech" dashboard with neon or glow;
- a generic SaaS template built from rows of identical KPI tiles and decorative charts;
- alarm red used for emphasis;
- glass and blur used as ornament.

**Key Characteristics:**
- Pale, sunlight-first surfaces; dark is the dusk alternative, not the default.
- One saturated block (field health), whose colour follows the severity band.
- Soft ambient lift for content. Alerts lie flat and speak through colour.
- One typeface (Outfit). Size, weight and ink separate the roles.
- Generous rounding and tonal fills; no outlines, no borders on surfaces.
- Thumb-sized targets (44px, 48px in the simple view) and quiet, quick feedback.

## Colors

A near-neutral, green-tinted grey world, where saturated colour is reserved for the plants' state and the system's health.

### Primary
- **Leaf Green** (`leaf-green`): the healthy state. It is the field-health card at rest, the `none` severity band, toggles and range sliders when on, and the selected outline. White on it is 5.0:1.
- **Leaf Green Deep** (`leaf-green-deep`): links, the end of the health gradient, falling-damage trends, and the badge text on the health card.
- **Leaf Green Soft** (`leaf-green-soft`): text selection and the selected choice card.

### Secondary
The severity ramp, which is how the plants are. It is used only for the field-health card, severity text and badges.
- **Young Olive** (`young-olive`): low damage.
- **Ochre Warning** (`ochre-warning`): moderate damage. **Ochre Ink** (`ochre-ink`) is its text-safe partner on **Ochre Soft** (`ochre-soft`), used for caution banners and rising-damage trends.
- **Brick Alarm** (`brick-alarm`): high damage, and failures such as detector offline, camera unplugged or destructive actions. **Brick Soft** (`brick-soft`) is the flat background of alarm banners and danger buttons.

### Tertiary
Status dots, 8px circles beside device and status labels.
- **Sprout Dot** (`sprout-dot`): live and working.
- **Sun Dot** (`sun-dot`): attention.
- **Ember Dot** (`ember-dot`): down.
- **Ash Dot** (`ash-dot`): idle or unknown, including stale readings.

### Neutral
- **Sun-Bleached Grey** (`sun-bleached-grey`): the page canvas.
- **Field Paper** (`field-paper`): cards, tiles, chips and dialogs.
- **Dust Grey** (`dust-grey`) and **Deep Dust** (`deep-dust`): tonal fills for controls, insets, meter tracks and hover states.
- **Furrow Line** (`furrow-line`): the only hairline, used for table rows and the top of the phone tab bar.
- **Soil Ink** (`soil-ink`): primary text.
- **Bark Grey** (`bark-grey`): secondary text and icons.
- **Stone Grey** (`stone-grey`): captions, 5.1:1 on paper.
- **Pebble Grey** (`pebble-grey`): the greyed units after large numerals only, 3.2:1.
- **Wet Soil** (`wet-soil`): primary buttons and the active navigation item.

### Dark theme (dusk and indoors)
This is set on `<html>` before the first paint, from the phone's setting or Settings. It is the same green-grey world with lifted accents:

| Role | Colour |
|---|---|
| canvas | `#121413` |
| paper | `#1c1f1d` |
| fills | `#262a28` / `#303532` |
| ink | `#edefec` |
| secondary text | `#c4c9c5` |
| captions | `#9ba29d` |
| units | `#80877f` |
| green / deep green | `#3fb46a` / `#5fcf89` |
| ochre ink | `#efb14a` |
| brick | `#f4786d` |

Wet Soil inverts to `#e8eae6`, with dark text, so the primary button stays the strongest object. The field-health card keeps its literal light-theme colours.

### Named Rules
**The Colour Carries the Reading Rule.** Saturated colour appears on the field-health card, severity text, status dots and alerts, and nowhere else. If a block is coloured, it must be telling the farmer something.

**The Red Is Earned Rule.** Brick red means real damage or real failure. It is never used for emphasis, decoration or a brand accent.

**The Rising Is Amber Rule.** Rising aphid damage is bad news, so an upward trend is ochre and a downward one green. Never use the finance convention.

**The Same In Both Themes Rule.** The field-health card uses literal colours and looks identical in light and dark, so a reading never shifts hue with the theme.

## Typography

**Body Font:** Outfit, a variable font (300–700) self-hosted from `fonts/outfit-latin-var.woff2` with `font-display: swap`. The fallback is -apple-system, Segoe UI, Roboto, Arial. It is the only family.
**Mono:** the system UI monospace, used only for commands and file paths, never as a "technical" costume.

**Character:** A soft geometric sans that stays friendly at farmer-facing sizes and turns quiet and exact in the large light numerals.

### Hierarchy
- **Display** (500, `clamp(30px, 6vw, 44px)`, line-height 1.05, -0.03em): the site name in the top bar.
- **Numeral** (400, `clamp(34px, 4.4vw, 50px)`, line-height 1, -0.04em, tabular figures): tile figures. Units follow at 0.72em, weight 300, in Pebble Grey.
- **Headline** (600, `clamp(24px, 6.5vw, 30px)`, 1.2): the field-health card's sentence, such as "No damage seen".
- **Title** (500, 20px): card and section headings.
- **Body** (400, 15px, 1.5): running text and item names at weight 500.
- **Control** (500, 14px): buttons, toggles, table cells and banners.
- **Note** (400, 13px, 1.55): helper text, tile notes and metadata, in Stone Grey.
- **Label** (500, 12px): chips, badges, field labels and table headers. Sentence case, never uppercase-tracked.

The simple view raises the scale for reading at arm's length: note 15px, control 16px, body 17px and title 22px. Headings use `text-wrap: balance` and paragraphs `pretty`.

### Named Rules
**The One Family Rule.** Outfit only. A new role is made from size, weight and ink, never from a second typeface.

**The Light Numeral Rule.** Big figures are weight 400 with weight-300 greyed units and tabular figures, so a changing value never jitters. In the dark theme the units step up to weight 400.

**The 16px Field Rule.** Inputs are never below 16px (`type-field`); below that, iOS zooms the page on focus.

## Layout

The layout is phone first, with three widths:
- **Phones:** a single column with 16px side gutters and a 16px vertical rhythm between blocks. Navigation is a translucent bottom tab bar within thumb reach, and content clears it by 84px plus the safe-area inset.
- **From 760px:** the tab bar becomes a sticky 92px icon rail on the left, made of 56px icon squares, and gutters grow to 28px.
- **Content width:** content caps at 1480px.

Dashboard grids:
- **Simple view:** the field-health card, the live camera, "this week" and history stack in one column. From 1240px, the card and week sit beside the live camera (1fr / 1.25fr).
- **Full view:** the live view and tiles form the main column. From 1240px the hardware list becomes a 340px sidebar.
- **Tiles:** two to a row on phones and four from 900px, with a 188px minimum height.
- **Cameras:** two to a row at every width, a portrait phone included; from 900px they auto-fit at 280px or wider. Tapping a camera expands it to the whole card.
- **Camera frames:** each frame takes the camera's own aspect ratio and is never taller than 60vh. Container queries shorten its messages when a frame is about 160px wide.
- **History gallery:** auto-fills 170px cards.

Internal spacing is tight within groups (4–12px) and generous between them (16–28px). Cards pad at 20px and tiles at 18px.

**The Thumb Reach Rule.** On a phone, every primary action and the navigation sit where a thumb lands. No control needed in the field is placed only at the top of a long page.

## Elevation & Depth

Content uses a soft ambient lift: paper tiles and cards float a little off the grey canvas on a long, low, almost invisible shadow. Alerts lie flat. Caution and alarm banners drop their shadow and speak through tonal colour, so urgency never looks like "higher". Depth inside a surface is tonal (Dust Grey fills), not stacked shadows.

### Shadow Vocabulary
- **Ambient lift** (`box-shadow: 0 1px 1px rgb(27 29 28 / 0.03), 0 10px 28px -18px rgb(27 29 28 / 0.28)`): cards, tiles, chips, devices and neutral banners. In the dark theme it is `0 1px 1px rgb(0 0 0 / 0.3), 0 10px 28px -18px rgb(0 0 0 / 0.8)`.
- **Health glow** (`box-shadow: 0 16px 30px -18px rgba(17, 102, 50, 0.85)`): the field-health card only. It is tinted per band: olive `rgba(59, 95, 11, 0.85)`, ochre `rgba(138, 83, 0, 0.85)` and brick `rgba(143, 27, 18, 0.85)`.
- **Active rail** (`box-shadow: 0 10px 20px -12px rgb(27 29 28 / 0.7)`): the current navigation square.
- **Floating toast** (`box-shadow: 0 12px 30px -10px rgb(27 29 28 / 0.45)`): toasts above the page.
- **Dialog** (`box-shadow: 0 30px 80px -30px rgb(27 29 28 / 0.6)`): the scan-detail dialog, over a `rgb(27 29 28 / 0.55)` backdrop.

### Named Rules
**The Alerts Lie Flat Rule.** A warning never gains elevation. It loses its shadow and takes a soft tonal fill (Ochre Soft or Brick Soft).

**The Purposeful Glass Rule.** Blur appears only to keep text readable over moving content: the phone tab bar (94% paper, `saturate(1.4) blur(10px)`) and chips over the camera image (92% paper). Never anywhere else.

## Shapes

The form language is generous and continuous rounding, on four steps:
- 22px for cards, tiles and the field-health card;
- 18px for inner surfaces such as devices, banners, camera frames, choice cards and desktop rail squares;
- 14px for buttons;
- 12px for fields, pills and phone tab items.

Chips, badges, meters and toggle tracks are full pills (999px), and status dots are circles.

Surfaces have no borders. The one 1px hairline (Furrow Line) separates table rows and caps the phone tab bar.

Icons come from a hand-drawn SVG sprite shipped with the app: 24px grid, 1.75 stroke, round caps and joins, shown at 20px (16–28px by context).

## Components

### Buttons
Soft and sure: rounded tonal fills, no outlines, thumb-sized.
- **Shape:** 14px radius, with a minimum height of 44px (48px in the simple view).
- **Default:** a Dust Grey fill with Soil Ink control text, padded 10px 16px with an 8px icon gap. Hover deepens it to Deep Dust.
- **Primary:** Wet Soil with white text. Hover goes to pure black, and in the dark theme it inverts.
- **Danger:** Brick Soft with Brick Alarm text, filling to solid Brick Alarm on hover. It is used only for destructive actions, behind an inline confirmation panel.
- **Quiet:** transparent with Bark Grey text and 8px 12px padding, taking a Dust Grey fill on hover.
- **Press and focus:** a 1px downward press on `:active`. Focus is a 2px Soil Ink outline at a 2px offset. Disabled buttons drop to 45% opacity.
- **Motion:** 0.16s ease on background, colour and transform.

### Chips and Badges
- **Status chip:** a Field Paper pill with ambient lift, label type, and a status dot before the text, such as "Watching" or "Not watching". Over the camera the chip turns to 92% paper with no shadow.
- **Badge:** a white pill with Leaf Green Deep text (the band's colour per severity), sitting only on the field-health card.
- **Pill note:** a 12px-radius tonal note, Ochre Soft or Brick Soft, for an inline warning inside a tile.

### Cards / Containers
- **Corner Style:** 22px.
- **Background:** Field Paper on the Sun-Bleached Grey canvas.
- **Shadow Strategy:** ambient lift (see Elevation).
- **Border:** none.
- **Internal Padding:** 18–20px around the header and 14–20px for the body. Headers pair a Bark Grey icon with a Title heading, and actions sit at the right.
- **Tiles:** a label row (icon, name, a corner chip), then the light numeral with a greyed unit. Notes and an optional 6px pill meter follow. When a reading goes stale, the figure fades to 55% and a dated note in Brick Alarm appears.

### Inputs / Fields
- **Style:** a Dust Grey fill with a transparent 1px border and a 12px radius, at least 44px tall. Text is 16px Soil Ink and placeholders are Stone Grey.
- **Focus:** a 2px Soil Ink outline with no offset, and the fill lifts to white (Deep Dust in dark).
- **Toggles:** a 40×24 pill track in Deep Dust that turns Leaf Green when on. The white knob slides 16px on `cubic-bezier(0.16, 1, 0.3, 1)` over 0.2s.
- **Sliders and radios:** native, tinted with `accent-color` Leaf Green.

### Navigation
- **On phones:** a fixed bottom tab bar of icon-plus-label items in 12px label type, centred, at most 420px wide. The current page is a Wet Soil pill with white text.
- **From 760px:** a sticky left rail of 56px icon-only squares (18px radius, Dust Grey, Deep Dust on hover). The current page is Wet Soil with the active-rail shadow, and labels stay in the accessibility tree.

### Field-Health Card (signature)
This is the one saturated surface: the farmer's "how are my plants" card in the simple view, and the hero tile in the full view.
- **Fill:** a 150° gradient from the band colour to its deeper partner. Leaf Green runs to `#116632`, Young Olive to `#3b5f0b`, Ochre to `#8a5300`, and Brick to `#8f1b12`.
- **Content:** white text, the band's health glow, 22px radius and 22px padding. An urgency pill at 20% white, a 600-weight headline, a short body and numbered actions sit on it.
- **Empty state:** with no photos yet, it falls back to plain Field Paper with ink text and makes no claim.

### Live Camera Frame (signature)
- **Frame:** an 18px-radius frame on a near-black `#101211` field once live, Dust Grey while idle.
- **Overlays:** chips sit in glass at the bottom-left corner. A 40px round expand button appears when several cameras share the card.
- **Stale:** a frozen frame is desaturated (`grayscale(0.7) brightness(0.8)`) so it is never read as live.
- **Unplugged or stalled:** the frame keeps its place and says so in words.

### Banners and Toasts
- **Shape:** 18px radius, a 13px 16px pad, control type and a leading 18px icon.
- **Neutral:** Field Paper with ambient lift, used for tips and a restart on purpose.
- **Caution:** flat Ochre Soft, used for low storage and clock drift.
- **Alarm:** flat Brick Soft, used when the detector is offline or there is no camera.
- **Toasts:** the same banner, floating centred above the tab bar (28px from the bottom on desktop) with the floating shadow. They clear after 6 seconds.

### Choice Cards
Settings asks for the view as two large radio cards. Each is a Dust Grey card at 18px radius with 16px padding. Selected, it is Leaf Green Soft with a 2px inset Leaf Green ring. Focus is a 2px Leaf Green outline.

### Motion
Quick and quiet. One thing moves on its own; everything else moves only to show a change.
- **The watchman's pulse:** while the detector runs, the status chip's dot sends out a soft ring (2.4s, scale 1 → 2.4, 60% → 0). It stops, and the dot turns red, when the detector does.
- **Feedback:** 0.16–0.2s for colour and press feedback. Things that travel use `cubic-bezier(0.16, 1, 0.3, 1)`: the toggle knob, the 2px card hover lift and the 0.4s meter fill.
- **Messages:** banners drop in 6px from above and toasts rise 10px, both over 0.28s on the same curve. Toasts leave faster than they arrive (0.16s). The scan dialog opens from 97% scale and 8px below over 0.3s, and closes at once.
- **Health colour:** the field-health card fades between band colours over 0.6s (registered `@property` colours). The first reading lands at once, so the placeholder green never shows on its way to the real band.
- **Camera expand:** a view transition, 0.36s. The tapped camera grows from its grid place to the whole card and back. The page around it snaps; only a camera appearing or leaving fades.
- **Going stale:** a figure fades to 55% over 0.4s, and a frozen camera frame desaturates over 0.6s.
- **Reduced motion** (`prefers-reduced-motion: reduce`): the pulse, the movement and the camera morph go. Messages and the dialog still fade in over 0.2s, and the health colour still fades, because those fades say something changed.

**The One Heartbeat Rule.** The live dot's pulse is the only motion that runs on its own. Anything else moves once, in answer to a change, and never on page load.

## Do's and Don'ts

### Do:
- **Do** keep the light theme the default and the dark theme for dusk. Keep body text at least 4.5:1 and large units at least 3:1 in both.
- **Do** let the field-health card be the only saturated block, and change its colour only with the severity band (Leaf Green → Young Olive → Ochre Warning → Brick Alarm).
- **Do** use tonal fills (Dust Grey, Deep Dust) for controls and insets, with targets of at least 44px (48px in the simple view).
- **Do** dim stale data visibly (55% figures, desaturated camera) and date it, instead of letting it pass as live.
- **Do** use tabular figures for every number that changes.
- **Do** ship every font and icon with the app, in one hand-written stylesheet, and set theme and view on `<html>` before the first paint.

### Don't:
- **Don't** default to dark, or use neon, glow or a dark-first "tech" look.
- **Don't** build the page from rows of identical KPI tiles, or add decorative charts that say nothing about the plants.
- **Don't** use Brick Alarm for emphasis, decoration or branding; red means real damage or real failure.
- **Don't** use blur or glass except to keep text readable over the camera image or the scrolling page (tab bar, camera chips).
- **Don't** show a rising-damage trend in green; rising is ochre.
- **Don't** give alerts elevation or borders. They lie flat in a tonal fill.
- **Don't** add borders to cards and tiles, or a second typeface.
- **Don't** load anything from a CDN or the internet.
