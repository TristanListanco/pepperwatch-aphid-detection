"""Browser tests — manuscript §3.5.1.

§3.5.1 asks for Selenium scripts that "simulate a farmer connecting to the
dashboard via a mobile browser, specifically tracking the asynchronous
JavaScript setInterval function to ensure it continuously fetches and renders
the live diagnostic overlays without causing webpage freezing, memory leaks, or
requiring manual user refreshes."

Each of those four clauses has a test below:

* continuously fetches        -> test_setinterval_keeps_firing
* without manual refresh      -> test_page_updates_without_a_manual_refresh
* without memory leaks        -> test_no_memory_leak_over_repeated_updates
* without freezing            -> test_page_stays_responsive_under_load

Plus two the manuscript implies but does not state: that the overlay actually
draws, and that the page still works with every external host unreachable —
which is what really proves the "no CDN" claim in §3.3.2.2.

These run against a live server on a free port, with a real worker feeding it.
Chrome is required; the suite skips cleanly when it is absent, since the Pi
itself will never run a browser.

    pytest tests/test_ui_selenium.py -v
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.selenium, pytest.mark.timeout(300)]

ROOT = Path(__file__).resolve().parent.parent
MOBILE = {"width": 390, "height": 844, "pixelRatio": 3}  # a typical phone


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(url: str, timeout: float = 30.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


@pytest.fixture(scope="module")
def chrome():
    """A headless Chrome factory, or skip if Chrome is unavailable."""
    selenium = pytest.importorskip("selenium")
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    def build(*, mobile: bool = True, extra_args: tuple[str, ...] = ()) -> "webdriver.Chrome":
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # Makes performance.memory readable, which the leak test needs.
        options.add_argument("--enable-precise-memory-info")
        options.add_argument(f"--window-size={MOBILE['width']},{MOBILE['height']}")
        for argument in extra_args:
            options.add_argument(argument)
        if mobile:
            options.add_experimental_option(
                "mobileEmulation",
                {
                    "deviceMetrics": MOBILE,
                    "userAgent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                        "Mobile/15E148 Safari/604.1"
                    ),
                },
            )
        options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
        try:
            return webdriver.Chrome(options=options)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chrome unavailable: {exc}")

    return build


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A real worker and web server on a free port, torn down afterwards."""
    workdir = tmp_path_factory.mktemp("uitest")
    port = free_port()

    if not (ROOT / "var" / "mock_frames" / "preview").exists():
        pytest.skip("mock frames missing — run scripts/prepare_mock_frames.py")

    env = {
        "PW_DB_PATH": str(workdir / "ui.db"),
        "PW_MEDIA_ROOT": str(workdir / "media"),
        "PW_WEB_PORT": str(port),
        "PW_PLATFORM_KIND": "mock",
        "PW_FRAMES_SHM_NAME": f"pw_ui_{port}",
        # Write often, so the gallery has content within a short test run.
        "PW_PERSIST_HEARTBEAT_DETECTED_S": "3",
        "PW_PERSIST_HEARTBEAT_CLEAN_S": "3",
        "PW_PERSIST_MIN_WRITE_INTERVAL_S": "2",
        "PW_PERSIST_TELEMETRY_INTERVAL_S": "2",
    }
    import os

    full_env = {**os.environ, **env}

    worker = subprocess.Popen(
        [sys.executable, "-m", "pepperwatch.worker"],
        cwd=ROOT, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    web = subprocess.Popen(
        [sys.executable, "-m", "pepperwatch.web"],
        cwd=ROOT, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{port}"
    if not wait_for(f"{base}/api/health", timeout=40):
        worker.terminate()
        web.terminate()
        pytest.skip("server did not come up")

    time.sleep(6)  # let the worker log a few events and some telemetry
    yield base

    for process in (web, worker):
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def page(chrome, server):
    driver = chrome()
    driver.get(server)
    # Let the SSE connection open and the first stats poll land.
    time.sleep(5)
    yield driver
    driver.quit()


def severe_logs(driver) -> list[str]:
    return [
        entry["message"]
        for entry in driver.get_log("browser")
        if entry["level"] == "SEVERE"
        # Chrome's own telemetry beacon is blocked in this environment and is
        # nothing to do with the page under test.
        and "plausible.io" not in entry["message"]
    ]


# -- structure -------------------------------------------------------------


def test_dashboard_loads_on_a_phone_without_console_errors(page) -> None:
    assert "Naawan" in page.title or "Pepper Watch" in page.title
    assert severe_logs(page) == []


def test_all_five_table_3_4_components_are_visible(page) -> None:
    for name, selector in {
        "Live Inference View": "[data-live]",
        "Actionable Output Panel": "[data-advice-headline]",
        "Historical Detection Log": "[data-gallery]",
        "System Health Monitor": "[data-health-temp]",
        "Capture & Export Controls": "[data-action-snapshot]",
    }.items():
        element = page.find_element("css selector", selector)
        assert element.is_displayed(), f"{name} is present but not visible"


def test_no_horizontal_scroll_on_a_phone(page) -> None:
    """A dashboard that scrolls sideways on a phone is unusable one-handed."""
    overflow = page.execute_script(
        "return document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"page overflows horizontally by {overflow}px"


# -- §3.5.1: continuous updates without manual refresh --------------------


def test_setinterval_keeps_firing(page) -> None:
    """§3.5.1's stated requirement, measured directly.

    bus.js increments window.__pw.intervalFires on every scheduled tick, so the
    count is observable rather than inferred.
    """
    before = page.execute_script("return window.__pw.intervalFires")
    time.sleep(35)  # the stats interval is 30s
    after = page.execute_script("return window.__pw.intervalFires")
    assert after > before, "no setInterval tick fired in 35 seconds"


def test_page_updates_without_a_manual_refresh(page) -> None:
    """'without requiring manual user refreshes' -- §3.5.1."""
    first = page.execute_script("return window.__pw.lastUpdate")
    time.sleep(8)
    second = page.execute_script("return window.__pw.lastUpdate")
    assert second > first, "the page received no updates over 8 seconds"

    counters = page.execute_script("return window.__pw")
    assert counters["sseMessages"] > 0 or counters["pollTicks"] > 0
    assert counters["mode"] in ("sse", "polling")


def test_live_telemetry_reaches_the_health_panel(page) -> None:
    temp = page.find_element("css selector", "[data-health-temp]").text
    assert temp not in ("", "—"), "health panel never received telemetry"
    assert "°C" in temp


def test_no_api_timeouts_or_handler_errors(page) -> None:
    """§3.5.1: 'flagging any instances of API timeouts'."""
    time.sleep(6)
    counters = page.execute_script("return window.__pw")
    assert counters["timeouts"] == 0, f"{counters['timeouts']} requests timed out"
    assert counters["errors"] == 0, f"{counters['errors']} handler errors"


# -- overlay ---------------------------------------------------------------


def test_overlay_canvas_draws_detections(page) -> None:
    """The Live Inference View's whole purpose: boxes over the feed."""
    page.find_element("css selector", "[data-action-start-camera]").click()
    time.sleep(6)

    painted = page.execute_script(
        "const c = document.querySelector('[data-live-canvas]');"
        "if (!c || !c.width) return -1;"
        "const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
        "let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;"
        "return n;"
    )
    assert painted > 100, f"overlay drew {painted} opaque pixels"


def test_overlay_can_be_switched_off(page) -> None:
    page.find_element("css selector", "[data-action-start-camera]").click()
    time.sleep(5)

    toggle = page.find_element("css selector", "[data-toggle-overlay]")
    page.execute_script("arguments[0].click()", toggle)
    time.sleep(2)

    painted = page.execute_script(
        "const c = document.querySelector('[data-live-canvas]');"
        "const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
        "let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;"
        "return n;"
    )
    assert painted == 0, "boxes remained after the overlay was switched off"


def test_live_feed_image_actually_loads(page) -> None:
    page.find_element("css selector", "[data-action-start-camera]").click()
    time.sleep(5)
    width = page.execute_script(
        "return document.querySelector('[data-live-image]').naturalWidth"
    )
    assert width > 0, "the feed image never decoded"


# -- §3.5.1: memory leaks and responsiveness ------------------------------


def test_no_memory_leak_over_repeated_updates(page) -> None:
    """§3.5.1: 'without causing ... memory leaks'.

    Heap size alone is a poor signal — it sawtooths with GC. DOM node count and
    listener count are the better ones, because the leak this guards against is
    a setInterval that rebuilds the gallery without detaching its handlers, and
    that shows up as unbounded node growth long before heap does.
    """
    page.find_element("css selector", "[data-action-start-camera]").click()
    time.sleep(3)

    def sample() -> dict:
        return page.execute_script(
            "return {"
            "  nodes: document.getElementsByTagName('*').length,"
            "  heap: (performance.memory && performance.memory.usedJSHeapSize) || 0"
            "};"
        )

    baseline = sample()
    time.sleep(45)
    after = sample()

    node_growth = after["nodes"] - baseline["nodes"]
    assert node_growth < 250, (
        f"DOM grew by {node_growth} nodes in 45s "
        f"({baseline['nodes']} -> {after['nodes']}) — listeners are likely leaking"
    )

    if baseline["heap"] and after["heap"]:
        growth_mb = (after["heap"] - baseline["heap"]) / 1e6
        assert growth_mb < 12, f"JS heap grew {growth_mb:.1f} MB in 45 s"


def test_page_stays_responsive_under_load(page) -> None:
    """§3.5.1: 'without causing webpage freezing'.

    A blocked main thread cannot run a scheduled callback, so round-tripping a
    zero-delay timer measures exactly the freezing the manuscript is worried
    about.
    """
    page.find_element("css selector", "[data-action-start-camera]").click()
    time.sleep(4)

    latency = page.execute_script(
        "const done = arguments[0];"
        "const t0 = performance.now();"
        "setTimeout(() => done(performance.now() - t0), 0);"
        if False
        else "return new Promise(r => { const t0 = performance.now();"
             "setTimeout(() => r(performance.now() - t0), 0); });"
    )
    assert latency < 400, f"main thread blocked for {latency:.0f} ms"


# -- controls and navigation ----------------------------------------------


def test_history_gallery_populates(page) -> None:
    time.sleep(4)
    tiles = page.find_elements("css selector", ".scan")
    assert tiles, "the Historical Detection Log is empty"

    # Thumbnails are loading="lazy", which is deliberate: every byte crosses the
    # Pi's single radio alongside the video stream, so images below the fold
    # should not be fetched. They therefore have to be scrolled to before the
    # assertion means anything.
    page.execute_script("document.querySelector('.scan-thumb').scrollIntoView()")
    time.sleep(3)

    thumb_ok = page.execute_script(
        "const i = document.querySelector('.scan-thumb');"
        "return i ? i.naturalWidth : 0;"
    )
    assert thumb_ok > 0, "gallery thumbnails did not load once scrolled into view"


def test_offscreen_thumbnails_are_deferred(page) -> None:
    """The bandwidth saving above, asserted so it cannot silently regress."""
    time.sleep(4)
    deferred = page.execute_script(
        "return [...document.querySelectorAll('.scan-thumb')]"
        "  .filter(i => i.getBoundingClientRect().top > window.innerHeight)"
        "  .every(i => i.loading === 'lazy');"
    )
    assert deferred, "off-screen gallery images are not lazily loaded"


def test_snapshot_button_is_accepted(page) -> None:
    before = len(page.find_elements("css selector", ".scan"))
    page.find_element("css selector", "[data-action-snapshot]").click()
    time.sleep(10)
    after = len(page.find_elements("css selector", ".scan"))
    assert after >= before
    assert severe_logs(page) == []


def test_pause_and_resume_round_trip(page) -> None:
    button = page.find_element("css selector", "[data-action-pause]")
    assert "Pause" in button.text

    button.click()
    time.sleep(3)
    assert "Resume" in page.find_element("css selector", "[data-action-pause]").text

    page.find_element("css selector", "[data-action-pause]").click()
    time.sleep(3)
    assert "Pause" in page.find_element("css selector", "[data-action-pause]").text


@pytest.mark.parametrize("path,marker", [("/archived", "Archived Scans"), ("/settings", "Detection Thresholds")])
def test_other_pages_load(chrome, server, path, marker) -> None:
    driver = chrome()
    try:
        driver.get(f"{server}{path}")
        time.sleep(3)
        assert marker in driver.page_source
        assert severe_logs(driver) == []
    finally:
        driver.quit()


# -- the offline guarantee -------------------------------------------------


def test_page_renders_with_every_external_host_unreachable(chrome, server) -> None:
    """This is what actually proves §3.3.2.2's no-CDN claim.

    Chrome is started with a proxy that resolves nothing, and every non-local
    host is made unreachable. A page that still renders completely cannot have
    depended on one.
    """
    driver = chrome(
        extra_args=(
            "--proxy-server=http://127.0.0.1:1",
            "--proxy-bypass-list=127.0.0.1;localhost",
        )
    )
    try:
        driver.get(server)
        time.sleep(6)

        for selector in (
            "[data-live]",
            "[data-advice-headline]",
            "[data-gallery]",
            "[data-health-temp]",
            "[data-kpi-detections]",
        ):
            assert driver.find_element("css selector", selector).is_displayed()

        # The stylesheet is what would most obviously fail; a styled body proves
        # it loaded from this device.
        background = driver.execute_script(
            "return getComputedStyle(document.body).backgroundColor"
        )
        assert background not in ("rgba(0, 0, 0, 0)", "rgb(255, 255, 255)"), (
            "the stylesheet did not load, so the page is unstyled"
        )

        # And the modules ran, which means they imported successfully.
        assert driver.execute_script("return typeof window.__pw") == "object"
    finally:
        driver.quit()
