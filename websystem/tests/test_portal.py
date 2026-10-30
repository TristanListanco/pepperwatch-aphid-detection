"""Captive-portal behaviour.

Each OS probes a known URL on joining a network and concludes it has internet
only if it receives the exact expected answer. These tests assert we return
something *else* — which is what makes the "Sign in to network" sheet appear and
the dashboard open on its own, satisfying "anyone who connects sees the
dashboard" without telling anybody a URL.
"""

from __future__ import annotations


import pytest


def test_android_probe_does_not_return_204(client) -> None:
    """Android treats HTTP 204 as 'internet works' and shows no portal."""
    response = client.get("/generate_204")
    assert response.status_code != 204
    assert response.status_code == 302
    assert "192.168.4.1" in response.headers["Location"]


@pytest.mark.parametrize("route", ["/generate_204", "/gen_204", "/generate204"])
def test_all_android_probe_spellings_redirect(client, route) -> None:
    assert client.get(route).status_code == 302


@pytest.mark.parametrize(
    "route", ["/hotspot-detect.html", "/library/test/success.html", "/success.html"]
)
def test_apple_probe_body_omits_the_success_token(client, route) -> None:
    """iOS and macOS look for the literal word 'Success' in the body."""
    response = client.get(route)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Success" not in body
    assert "Pepper Watch" in body
    # The refresh gets the dashboard up without the user tapping anything.
    assert "192.168.4.1" in body


def test_windows_ncsi_probe_omits_the_expected_token(client) -> None:
    """Windows looks for exactly 'Microsoft NCSI'."""
    body = client.get("/ncsi.txt").get_data(as_text=True)
    assert "Microsoft NCSI" not in body
    assert "Pepper Watch" in body


@pytest.mark.parametrize("route", ["/canonical.html", "/success.txt", "/nm-check.txt"])
def test_firefox_and_gnome_probes_redirect(client, route) -> None:
    assert client.get(route).status_code == 302


def test_portal_landing_reaches_the_dashboard(client) -> None:
    response = client.get("/portal")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_a_stray_url_lands_on_the_dashboard(client) -> None:
    """With wildcard DNS, every hostname resolves here — so nothing 404s."""
    response = client.get("/some/random/path/a/device/probed")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")


def test_api_paths_still_return_a_real_404(client) -> None:
    """A mistyped endpoint needs a 404, not a redirect to HTML."""
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.get_json()["error"] == "not found"


def test_portal_url_follows_the_configured_address(cfg) -> None:
    from pepperwatch.web import create_app

    cfg.portal._data["address"] = "10.42.0.1"  # noqa: SLF001
    cfg.web._data["port"] = 80  # noqa: SLF001
    app = create_app(cfg, start_hub=False)

    location = app.test_client().get("/generate_204").headers["Location"]
    # Port 80 must not be spelled out, or some probe clients mishandle it.
    assert location == "http://10.42.0.1/"
