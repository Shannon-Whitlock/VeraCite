#!/usr/bin/python3
"""CGI endpoint for the VeraCite web demo: POST a .bib, get the JSON report.

Stdlib only -- no framework, no pip, no virtualenv -- so it runs on a shared host
(e.g. OVH Web Hosting) that offers Python via CGI. It reads the .bib from the POST
body, runs the online no-LLM check (`veracite.check_bib_text`), and writes the JSON
report. Every error path still returns HTTP 200 with a JSON `{"error": ...}` so the
page always has something to render.

The Crossref/arXiv lookups happen here, server-side, so there is no browser CORS
problem -- exactly as the VeraCite CLI makes them.
"""

import json
import os
import sys

# Bound the request: a 10-entry .bib is a few KB; reject anything larger before we
# parse, so the public endpoint can't be handed a huge body.
# Bump this whenever check.cgi or the demo behaviour changes, so the live response's
# "build" field tells us at a glance whether the server is running current code.
BUILD = "2026-06-24-doi-recovery"

MAX_BODY_BYTES = 64 * 1024
MAX_ENTRIES = 10
# Per-request HTTP timeout for the core sources. The whole request must finish inside
# the shared-host CGI limit (~120 s on OVH); fast mode's per-call caps (this, plus
# webcheck.AUX_TIMEOUT for the slower need-to-basis sources) keep it there.
HTTP_TIMEOUT = 10


def _send(obj, status="200 OK"):
    """Emit a CGI JSON response and exit. Status stays 200 even for input errors so
    the front end always parses a JSON body (it reads `error` if present).

    We emit a Content-Type header first and DROP the `Status:` header for 200 (Apache
    defaults to 200, and some shared-hosting CGI configs reject a `Status:` line as a
    malformed header, returning their own HTML 500 -- which then breaks JSON.parse on
    the client). Non-200 statuses still send `Status:` since the front end only needs
    the body, and a rejected status line there is harmless."""
    body = json.dumps(obj)
    sys.stdout.write("Content-Type: application/json; charset=utf-8\r\n")
    sys.stdout.write("Cache-Control: no-store\r\n")
    if not status.startswith("200"):
        sys.stdout.write(f"Status: {status}\r\n")
    sys.stdout.write("\r\n")
    sys.stdout.write(body)
    sys.stdout.flush()
    sys.exit(0)


def _read_body():
    """Read the raw request body (the .bib text), capped at MAX_BODY_BYTES. We accept
    the body as raw text (the front end POSTs the textarea contents directly) or as a
    single `bib=` form field, whichever was sent."""
    try:
        length = int(os.environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return ""
    if length > MAX_BODY_BYTES:
        _send({"error": f"bibliography too large (limit {MAX_BODY_BYTES // 1024} KB); "
               "this demo checks up to 10 entries"}, status="413 Payload Too Large")
    data = sys.stdin.buffer.read(length)
    text = data.decode("utf-8", "replace")
    ctype = os.environ.get("CONTENT_TYPE", "")
    if "application/x-www-form-urlencoded" in ctype and text.startswith("bib="):
        from urllib.parse import parse_qs
        vals = parse_qs(text, keep_blank_values=True).get("bib", [""])
        return vals[0]
    return text


def main():
    if os.environ.get("REQUEST_METHOD", "GET").upper() != "POST":
        _send({"error": "POST a .bib body to this endpoint"},
              status="405 Method Not Allowed")

    # Make `import veracite` resolve from the package copied next to this script.
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    try:
        from veracite import check_bib_text, load_settings
        from veracite.config import SETTINGS
    except Exception as ex:                       # package missing / import error
        _send({"error": f"VeraCite is not installed next to this endpoint: {ex}"},
              status="500 Internal Server Error")

    raw = _read_body()
    if not raw.strip():
        _send({"error": "no bibliography submitted"}, status="400 Bad Request")

    # Be a good API citizen: a contact email puts Crossref/OpenAlex calls in the
    # "polite pool". Set VERACITE_CONTACT_EMAIL in the host environment (or a settings
    # file) to enable it; absent, the calls still work, just without the courtesy tag.
    load_settings()
    email = os.environ.get("VERACITE_CONTACT_EMAIL")
    if email:
        SETTINGS["contact_email"] = email

    try:
        report = check_bib_text(raw, max_entries=MAX_ENTRIES, timeout=HTTP_TIMEOUT)
    except Exception as ex:
        _send({"error": f"check failed: {ex}"}, status="500 Internal Server Error")
    # Build marker: lets us confirm from the live response WHICH code is deployed (so a
    # stale upload / cached .pyc is obvious). Bump BUILD when changing behavior.
    import veracite
    report["build"] = BUILD
    report["veracite_path"] = os.path.dirname(os.path.abspath(veracite.__file__))
    _send(report)


if __name__ == "__main__":
    # Top-level guard: catch ANY failure (including an import or environment error
    # that escapes main's own try/except) and return it as a JSON body, so the
    # browser never receives a bare Apache HTML 500 (which would break JSON.parse).
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        try:
            _send({"error": "check.cgi crashed", "traceback": tb})
        except BaseException:
            sys.stdout.write("Content-Type: application/json; charset=utf-8\r\n\r\n")
            sys.stdout.write(json.dumps({"error": "check.cgi crashed", "traceback": tb}))
