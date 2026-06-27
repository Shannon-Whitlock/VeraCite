"""HTTP transport: tiny GET helpers over either `requests` or the stdlib urllib.

The backend is chosen once at import (config.HTTP_BACKEND) so the rest of the
package never branches on it. Both helpers swallow network errors and report them
as a None result (and, for JSON, an HTTP status code) so callers can stay simple.

Rate limiting is PER SERVICE and time-based: each host keeps the time of its last
request and a minimum interval; a new request to that host waits only the remaining
time (and not at all if enough time has already passed, e.g. while OTHER services
were being queried). So the pacing is proportional to requests actually made -- an
entry resolved by Crossref alone never pays an arXiv delay -- and the slow arXiv
limit (one request per ~3 s) naturally spaces out across many entries instead of
blocking each one. There is no global sleep: only a real outbound GET waits, and
only for the service it targets.
"""

import json
import time
from urllib.parse import urlsplit

from .config import HTTP_BACKEND, SETTINGS, allowed_hosts, user_agent

if HTTP_BACKEND == "requests":
    import requests
else:  # pragma: no cover
    import urllib.error
    import urllib.request


# Minimum seconds between requests to a given service, by host substring. arXiv asks
# for ~1 request / 3 s; the rest are comfortable in Crossref/OpenAlex's polite pool,
# so they fall back to the configurable `request_delay` (default 0.2 s). The match is
# a substring of the URL host, so 'export.arxiv.org' and 'arxiv.org' both map to the
# arXiv limit.
_HOST_MIN_INTERVAL = {
    "arxiv.org": 3.0,
}

# host -> monotonic time of its last request, so the next one to that host waits only
# the remainder of its interval (process-local; one run is a single process).
_last_request = {}


def _service_interval(host):
    for frag, secs in _HOST_MIN_INTERVAL.items():
        if frag in host:
            return secs
    return float(SETTINGS.get("request_delay", 0.2) or 0)


def _throttle(url):
    """Wait, if needed, so this request respects its service's minimum interval --
    counting time already elapsed since that service's last call (work on other
    services or the rest of the pipeline counts), so a fast service is not slowed and
    a slow one (arXiv) spaces out across entries rather than blocking each."""
    host = urlsplit(url).hostname or ""
    interval = _service_interval(host)
    now = time.monotonic()
    last = _last_request.get(host)
    if last is not None and interval:
        wait = interval - (now - last)
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
    _last_request[host] = now


def reset_throttle():
    """Clear the per-host timers. For tests, so one test's timing does not leak into
    the next; a real run is a fresh process and never needs this."""
    _last_request.clear()


def _host_allowed(url):
    """True if `url` targets one of the configured API hosts (scheme + hostname).

    Defense in depth against URL injection: even though identifiers are shape-gated
    and percent-encoded, DOIs/ids keep '/' literal in the path, so a crafted value
    could in principle steer a request to an unexpected place. Every outbound GET is
    checked against config.allowed_hosts() and dropped if it does not match, so a
    `.bib` can never make VeraCite talk to a host it was not pointed at. The check is
    on (scheme, hostname) only -- the path is where a value lives, so locking the
    host is what matters; a same-host path is already constrained by the DOI gate."""
    parts = urlsplit(url)
    return (parts.scheme, parts.hostname) in allowed_hosts()


def http_get_json(url, timeout):
    """GET `url` and parse JSON. Returns (data, status_code); data is None on any
    failure, with status_code carrying the HTTP code (or -1 for a network error)
    so callers can distinguish a 404 from a timeout. Paced per service (see
    _throttle): the wait, if any, is only for this URL's host."""
    if not _host_allowed(url):
        return None, -1                  # not a configured host -- never reach out
    _throttle(url)
    headers = user_agent()
    if HTTP_BACKEND == "requests":
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException:
            return None, -1
        return (r.json(), 200) if r.status_code == 200 else (None, r.status_code)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), 200
    except urllib.error.HTTPError as ex:
        return None, ex.code
    except Exception:
        return None, -1


def http_get_text(url, timeout):
    """GET `url` and return the body as text, or None on any failure. Paced per
    service (see _throttle)."""
    if not _host_allowed(url):
        return None                      # not a configured host -- never reach out
    _throttle(url)
    headers = user_agent()
    if HTTP_BACKEND == "requests":
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException:
            return None
        return r.text if r.status_code == 200 else None
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return None
