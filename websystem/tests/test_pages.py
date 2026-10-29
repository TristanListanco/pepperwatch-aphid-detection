"""Page rendering, and the offline constraint §3.3.2.2 commits to.

The no-CDN assertion is the important one here. The manuscript rejects heavier
UI stacks partly on the grounds that this runs offline, so a stylesheet or
script that silently reached for an external host would invalidate the claim —
and would simply fail to load in a pepper field. A regex over the rendered HTML
catches that at build time; ``test_ui_selenium.py`` proves it again at runtime
by failing every off-host request and checking the page still renders.
"""

from __future__ import annotations

import re

import pytest

PAGES = ["/", "/archived", "/settings"]

# Any absolute URL to somewhere that is not this device.
EXTERNAL_URL = re.compile(r"""(?:src|href)\s*=\s*["'](?!/|data:|\#)([a-zA-Z]+:)?//""")


@pytest.mark.parametrize("path", PAGES)
def test_page_renders(client, path) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.mimetype == "text/html"


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_nothing_from_an_external_host(client, path) -> None:
    """§3.3.2.2: this must work with no internet at all."""
    html = client.get(path).get_data(as_text=True)
    offenders = EXTERNAL_URL.findall(html)
    assert not offenders, f"{path} references an external host: {offenders}"


@pytest.mark.parametrize("path", PAGES)
def test_page_declares_a_mobile_viewport(client, path) -> None:
    """It is read on a phone held in one hand, standing in a row of peppers."""
    html = client.get(path).get_data(as_text=True)
    assert 'name="viewport"' in html
    assert "width=device-width" in html


def test_dashboard_contains_every_table_3_4_component(client) -> None:
    """Table 3.4 names five interface components. All five must be present."""
    html = client.get("/").get_data(as_text=True)

    required = {
        "Live Inference View": "data-live",
        "Actionable Output Panel": "data-advice-headline",
        "Historical Detection Log": "data-gallery",
        "System Health Monitor": "data-health-temp",
        "Capture & Export Controls": "data-action-snapshot",
    }
    missing = [name for name, hook in required.items() if hook not in html]
    assert not missing, f"missing Table 3.4 components: {missing}"

    # Export and pause are part of that fifth component too.
    assert "data-action-export" in html
    assert "data-action-pause" in html


def test_dashboard_contains_the_figure_3_4_elements(client) -> None:
    """Figure 3.4 draws more than Table 3.4 lists."""
    html = client.get("/").get_data(as_text=True)
    for hook in (
        "data-kpi-detections",   # Weekly Detections tile
        "data-kpi-confidence",   # Avg. Confidence tile
        "data-kpi-trend",        # Trend tile
        "data-kpi-severity",     # Severity tile
        "data-donut",            # Detection Distribution
        "data-toggle-live",      # the Live toggle
    ):
        assert hook in html, f"Figure 3.4 element {hook} is missing"

    assert "Start Camera" in html
    assert "Detection Distribution" in html


def test_site_name_comes_from_config(client, cfg) -> None:
    """Figure 3.4 shows 'Buru-un'; the deployed name must be configurable."""
    assert cfg.site.name in client.get("/").get_data(as_text=True)


def test_operator_and_role_render_in_the_sidebar(client, cfg) -> None:
    html = client.get("/").get_data(as_text=True)
    assert cfg.site.operator in html
    assert cfg.site.role in html


def test_dashboard_shows_the_measured_performance_and_power(client) -> None:
    """The figures the results chapter reports, all on the dashboard."""
    html = client.get("/").get_data(as_text=True)
    for hook in (
        "data-pipeline-inference-rate",  # inference fps, against the 17 fps target
        "data-pipeline-inference-meter",
        "data-pipeline-preview-rate",
        "data-pipeline-latency",
        "data-health-power",
        "data-health-power-source",      # INA219 or the PMIC estimate, never unlabelled
        "data-session-energy",
        "data-accelerator-chip",         # CPU or which Hailo chip
    ):
        assert hook in html, f"performance element {hook} is missing"


def test_dashboard_offers_the_session_csv_files(client) -> None:
    html = client.get("/").get_data(as_text=True)
    for name in ("inference.csv", "system.csv", "summary.json"):
        assert f'data-session-file="{name}"' in html
    assert "/api/sessions.csv" in html
    assert "data-session-new" in html


def test_trimmed_metrics_are_gone(client) -> None:
    """Battery, storage, memory and the insight banner were cut from the dashboard."""
    html = client.get("/").get_data(as_text=True)
    for hook in ("data-health-battery", "data-health-disk", "data-health-mem", "data-insight"):
        assert hook not in html


def test_unvalidated_advice_shows_a_draft_banner(client) -> None:
    """Unreviewed pesticide guidance must never present as settled advice."""
    html = client.get("/").get_data(as_text=True)
    assert "Draft guidance" in html
    assert "agriculturist" in html
    # The banner is only hidden once the content file says it was validated.
    assert 'data-advice-draft hidden' not in html


def test_settings_explains_the_threshold_departure(client) -> None:
    """The 0.25 default contradicts Figure 3.3; the UI should say why."""
    html = client.get("/settings").get_data(as_text=True)
    assert "0.80" in html
    assert "Figure 3.3" in html


def test_security_headers_are_present(client) -> None:
    headers = client.get("/").headers
    assert "Content-Security-Policy" in headers
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_csp_forbids_external_origins(client) -> None:
    """The header is the runtime half of the no-CDN guarantee."""
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "script-src 'self'" in csp


@pytest.mark.parametrize(
    "attempt",
    [
        "../../etc/passwd",
        "....//....//etc/passwd",
        "/etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "%2e%2e/%2e%2e/etc/passwd",
    ],
)
def test_media_route_rejects_path_traversal(client, attempt) -> None:
    """The one route mapping user input onto the filesystem.

    Redirects are followed because Werkzeug normalises a doubled slash with a
    308 before routing; what matters is where the request finally lands, and
    that no file content comes back.
    """
    response = client.get(f"/media/{attempt}", follow_redirects=True)
    assert response.status_code in (403, 404)
    assert b"root:" not in response.get_data()


def test_media_serves_a_file_inside_the_root(client, cfg) -> None:
    root = cfg.media_root
    (root / "2026" / "09").mkdir(parents=True, exist_ok=True)
    (root / "2026" / "09" / "x.jpg").write_bytes(b"\xff\xd8test\xff\xd9")

    response = client.get("/media/2026/09/x.jpg")
    assert response.status_code == 200
    assert response.get_data() == b"\xff\xd8test\xff\xd9"


def test_static_assets_are_served(client) -> None:
    for asset in ("css/app.css", "js/main.js", "js/bus.js", "js/liveview.js"):
        assert client.get(f"/static/{asset}").status_code == 200, asset


def test_javascript_modules_import_only_local_files(client) -> None:
    """A bare or remote import specifier would break with no internet."""
    for module in ("main", "bus", "liveview", "history", "settings", "health"):
        source = client.get(f"/static/js/{module}.js").get_data(as_text=True)
        for match in re.finditer(r"""from\s+['"]([^'"]+)['"]""", source):
            target = match.group(1)
            assert target.startswith("./") or target.startswith("../"), (
                f"{module}.js imports {target!r}, which is not a local path"
            )
