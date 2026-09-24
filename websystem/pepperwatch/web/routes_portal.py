"""Captive-portal endpoints.

The requirement is that anyone who joins the Pi's Wi-Fi gets the dashboard
without being told a URL. Two mechanisms deliver that, and both are worth
shipping because device coverage differs.

**RFC 8910**, the modern path: DHCP option 114 hands the portal URL to the
client directly, and the client fetches ``/api/captive-portal`` for its state.
Current iOS and Android honour it, and it is clean — no deception involved.

**Probe spoofing**, the fallback: every OS silently fetches a known URL on join
and decides it has internet if the exact expected answer comes back. Answering
anything else is what makes the "Sign in to network" sheet appear. Each
response below is therefore deliberately *not* what the OS wants to see:

* Android fetches ``/generate_204`` expecting HTTP 204 with an empty body.
* iOS and macOS fetch ``/hotspot-detect.html`` expecting a page containing
  exactly "Success".
* Windows fetches ``/ncsi.txt`` expecting the body "Microsoft NCSI".
* Firefox and GNOME fetch ``/canonical.html`` expecting HTTP 204.

Paired with the wildcard DNS in ``deploy/network/dnsmasq-shared.d``, which
resolves every name to the Pi, this is what makes the dashboard open by itself.
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, redirect, request, url_for

bp = Blueprint("portal", __name__)


def _portal_url() -> str:
    """The dashboard's absolute URL on the access-point address."""
    address = current_app.config["PW_CONFIG"].portal.address
    port = current_app.config["PW_CONFIG"].web.port
    host = address if port in (80, 443) else f"{address}:{port}"
    return f"http://{host}/"


# -- Android ---------------------------------------------------------------


@bp.route("/generate_204")
@bp.route("/gen_204")
@bp.route("/generate204")
def android_probe():
    """Redirect instead of 204, which is what raises the portal sheet."""
    return redirect(_portal_url(), code=302)


# -- iOS and macOS ---------------------------------------------------------


@bp.route("/hotspot-detect.html")
@bp.route("/library/test/success.html")
@bp.route("/success.html")
def apple_probe():
    """A page that pointedly does not contain Apple's expected "Success"."""
    body = (
        "<!doctype html><html><head><title>Pepper Watch</title>"
        '<meta http-equiv="refresh" content="0; url=' + _portal_url() + '">'
        "</head><body>Pepper Watch field monitor — opening the dashboard."
        "</body></html>"
    )
    return Response(body, mimetype="text/html", status=200)


# -- Windows ---------------------------------------------------------------


@bp.route("/ncsi.txt")
def windows_ncsi():
    """Windows wants exactly "Microsoft NCSI"; anything else flags the portal."""
    return Response("Pepper Watch", mimetype="text/plain", status=200)


@bp.route("/connecttest.txt")
def windows_connecttest():
    return Response("Pepper Watch", mimetype="text/plain", status=200)


@bp.route("/redirect")
def windows_redirect():
    return redirect(_portal_url(), code=302)


# -- Firefox, GNOME, Ubuntu ------------------------------------------------


@bp.route("/canonical.html")
@bp.route("/success.txt")
@bp.route("/nm-check.txt")
def canonical_probe():
    return redirect(_portal_url(), code=302)


# -- RFC 8910 --------------------------------------------------------------


@bp.route("/api/captive-portal")
def captive_portal_api():
    """The RFC 8910 Captive Portal API.

    ``captive: true`` tells a compliant client it is behind a portal and offers
    the URL to open. The media type is mandated by the RFC and clients check it,
    so this is one of the few places the exact Content-Type is load-bearing.
    """
    payload = {
        "captive": True,
        "user-portal-url": _portal_url(),
        "can-extend-session": False,
        # Purely informational, but it makes the sheet's title useful.
        "venue-info-url": _portal_url(),
    }
    return current_app.response_class(
        response=current_app.json.dumps(payload),
        status=200,
        mimetype="application/captive+json",
    )


@bp.route("/portal")
def portal_landing():
    """Where a probe redirect lands: straight to the dashboard."""
    return redirect(url_for("pages.dashboard"), code=302)
