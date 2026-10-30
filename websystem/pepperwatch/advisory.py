"""The Actionable Output Panel's content layer.

Table 3.4 asks this panel to translate "raw mathematical classification outputs
into plain-text, localized diagnostic alerts and immediate treatment
recommendations." Two things follow from that sentence that the manuscript does
not spell out.

First, "localized" in Northern Mindanao will be read as *translated*, not merely
*computed on-device*. So the content file carries Cebuano alongside English and
the panel can switch without a reload.

Second, and more seriously: treatment advice going to smallholder farmers is not
decoration. Recommending the wrong intervention costs a season, and recommending
a chemical wrongly can cost a farmer their crop's saleability through a missed
pre-harvest interval. The content file therefore names no products or dosages,
escalates to "ask your DA or ATI technician", and carries a validation status
that the dashboard surfaces as a banner until an agriculturist signs it off.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_LOCALE = "en"


class Advisory:
    """Loads the recommendation content and maps detections onto it."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        try:
            self._data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.error("could not load %s: %s", self._path, exc)
            self._data = {"meta": {}, "classes": {}}

    # -- metadata ----------------------------------------------------------

    @property
    def meta(self) -> dict[str, Any]:
        return dict(self._data.get("meta", {}))

    @property
    def locales(self) -> list[str]:
        return list(self._data.get("meta", {}).get("locales", [DEFAULT_LOCALE]))

    @property
    def validated(self) -> bool:
        """Whether an agriculturist has signed the content off.

        Drives a visible banner. A dashboard that presents unreviewed pesticide
        guidance as settled advice is worse than one that admits it is a draft.
        """
        return str(self._data.get("meta", {}).get("status", "")).lower().startswith("validated")

    # -- lookup ------------------------------------------------------------

    def for_severity(
        self, severity: str, *, locale: str = DEFAULT_LOCALE, damage_class: str = "aphid_damage"
    ) -> dict[str, Any]:
        """Advice for a severity band, falling back sensibly.

        ``none`` maps to the healthy entry; an unknown band falls back to the
        most severe available, because under-reporting a risk is the worse error.
        """
        classes = self._data.get("classes", {})

        if severity == "none":
            entry = classes.get("healthy", {}).get("none")
            resolved_class, resolved_band = "healthy", "none"
        else:
            bands = classes.get(damage_class, {})
            entry = bands.get(severity)
            resolved_class, resolved_band = damage_class, severity
            if entry is None:
                for candidate in ("high", "moderate", "low"):
                    if candidate in bands:
                        entry, resolved_band = bands[candidate], candidate
                        log.warning("no advice for severity %r; using %r", severity, candidate)
                        break

        if entry is None:
            return {
                "class_name": resolved_class,
                "severity": severity,
                "locale": locale,
                "headline": "No guidance available",
                "body": "The advisory content file is missing an entry for this state.",
                "actions": [],
                "urgency": "unknown",
                "references": [],
                "validated": self.validated,
            }

        text = entry.get(locale) or entry.get(DEFAULT_LOCALE) or {}
        return {
            "class_name": resolved_class,
            "severity": resolved_band,
            "requested_severity": severity,
            "locale": locale if locale in entry else DEFAULT_LOCALE,
            "headline": text.get("headline", ""),
            "body": text.get("body", ""),
            "actions": list(text.get("actions", [])),
            "urgency": text.get("urgency", "routine"),
            "references": list(entry.get("references", [])),
            "validated": self.validated,
        }

    def insight(self, stats: dict[str, Any], *, locale: str = DEFAULT_LOCALE) -> str:
        """The plain-language banner sentence under the KPI tiles.

        Figure 3.4 draws a sentence, not a metric, and composing it from the
        same numbers the tiles show is what makes the tiles legible to someone
        who does not read dashboards for a living.
        """
        site = stats.get("site") or "this site"
        days = stats.get("days", 7)
        detections = stats.get("detections", 0)
        trend = stats.get("trend", "stable")
        severity = stats.get("severity", "none")
        delta = stats.get("delta_pct", 0.0)
        # None: aphid damage this window and none in the one before, so no percentage.
        if delta is None:
            trend = "new"

        if locale == "ceb":
            trend_text = {
                "new": f"Walay danyos sa aphid nga natala sulod sa niaging {days} ka adlaw.",
                "rising": f"Nagsaka ang danyos sa aphid og {abs(delta or 0):.0f}% kompara sa niaging {days} ka adlaw.",
                "falling": f"Nagkunhod ang danyos sa aphid og {abs(delta or 0):.0f}% kompara sa niaging {days} ka adlaw.",
                "stable": "Wala kaayo nagbag-o ang danyos sa aphid sulod sa gitan-aw nga panahon.",
            }[trend]
            severity_text = {
                "none": "Maayo ang kondisyon. Padayon sa naandang pagbantay.",
                "low": "Gaan ra ang danyos. Bantayi pag-ayo.",
                "moderate": "Kasarangan ang danyos. Pangandam sa aksyon.",
                "high": "Grabe ang danyos. Kontaka ang teknisyan karon.",
            }[severity]
            return (
                f"May {detections} ka detection sa {site} sulod sa niaging {days} ka adlaw. "
                f"{trend_text} {severity_text}"
            )

        trend_text = {
            "new": f"No aphid damage was recorded in the previous {days} days.",
            "rising": f"Aphid damage is up {abs(delta or 0):.0f}% on the previous {days} days.",
            "falling": f"Aphid damage is down {abs(delta or 0):.0f}% on the previous {days} days.",
            "stable": "Aphid damage has held steady over the observed period.",
        }[trend]
        severity_text = {
            "none": "Current conditions appear favourable. Maintain regular monitoring practices.",
            "low": "Damage is light so far. Keep monitoring closely before intervening.",
            "moderate": "Damage is established. Prepare to act and confirm by hand inspection.",
            "high": "Damage is heavy. Contact your agricultural technician today.",
        }[severity]

        counted = "No detections were" if detections == 0 else f"A total of {detections} detections were"
        return f"{counted} recorded on {site} over the past {days} days. {trend_text} {severity_text}"


def load_advisory(cfg) -> Advisory:
    return Advisory(cfg.path("config/recommendations.json"))
