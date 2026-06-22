"""HTTP transport: tiny GET helpers over either `requests` or the stdlib urllib.

The backend is chosen once at import (config.HTTP_BACKEND) so the rest of the
package never branches on it. Both helpers swallow network errors and report them
as a None result (and, for JSON, an HTTP status code) so callers can stay simple.
"""

import json

from .config import HTTP_BACKEND, user_agent

if HTTP_BACKEND == "requests":
    import requests
else:  # pragma: no cover
    import urllib.error
    import urllib.request


def http_get_json(url, timeout):
    """GET `url` and parse JSON. Returns (data, status_code); data is None on any
    failure, with status_code carrying the HTTP code (or -1 for a network error)
    so callers can distinguish a 404 from a timeout."""
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
    """GET `url` and return the body as text, or None on any failure."""
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
