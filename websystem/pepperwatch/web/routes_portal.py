"""Captive-portal endpoints.

The requirement is that anyone who joins the Pi's Wi-Fi gets the dashboard
without being told a URL.

Every OS silently fetches a known URL over plain HTTP, on port 80, when it
joins a network, and decides it has internet if the exact expected answer
comes back. Answering anything else is what makes the "Sign in to network"
sheet appear (or, on a laptop, the browser open). Each response below is
therefore deliberately *not* what the OS wants to see:

* Android fetches ``/generate_204`` expecting HTTP 204 with an empty body.
* iOS and macOS fetch ``/hotspot-detect.html`` expecting a page containing
  exactly "Success".
* Windows fetches ``/ncsi.txt`` expecting the body "Microsoft NCSI".
* Firefox and GNOME fetch ``/canonical.html`` expecting HTTP 204.

Any other path lands on the dashboard through the app's 404 handler. Paired
with the wildcard DNS in ``deploy/network/dnsmasq-shared.d``, which resolves
every name to the Pi, and gunicorn also listening on port 80, this is what
makes the dashboard open by itself.

RFC 8910 (DHCP option 114) is not used: phones only accept a portal API URL
over HTTPS, and an offline Pi has no certificate they would trust.
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, redirect, url_for

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


@bp.route("/portal")
def portal_landing():
    """Where a probe redirect lands: straight to the dashboard."""
    return redirect(url_for("pages.dashboard"), code=302)
