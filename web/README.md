# VeraCite web demo

A one-page "try it" front end for VeraCite's **online check, no LLM**, capped at
**10 entries**: paste a `.bib`, get the per-entry verification report and the 0–100
integrity score. It is a thin wrapper over the existing package — the verification
logic is unchanged.

```
web/
  index.html      the page (textarea + sample bib + result area)
  app.js          posts the bib to check.cgi and renders the JSON
  style.css       styling
  check.cgi       the Python CGI endpoint (stdlib only) -> veracite.check_bib_text
```

## How it works

The browser POSTs the bibliography text to `check.cgi`. The CGI script imports the
`veracite` package and calls `check_bib_text(raw, max_entries=10, timeout=8)`
([`veracite/webcheck.py`](../veracite/webcheck.py)), which runs the same layers the
CLI runs without `--tex`/`--llm` (syntax → static rules → record resolution →
verification → integrity) and returns the machine-readable report. The
Crossref/arXiv/OpenAlex lookups happen **server-side**, so there is **no browser
CORS problem** — exactly as the CLI makes them.

VeraCite has **zero required dependencies** and falls back to the stdlib `urllib`
when `requests` is absent, so the endpoint needs **no pip, no virtualenv, and no
long-running process** — which is what makes it deployable on plain CGI shared
hosting.

### Sources and speed (`fast` mode)

A shared-host CGI request is killed at a hard time limit (~120 s on OVH), so the web
endpoint runs `check_bib_text` in **`fast=True`** mode (the default). The full CLI
fans every entry out to many authoritative sources; some are slow, so fast mode keeps
the ones that earn their latency and drops the rest:

| Source | Fast mode | Why |
|--------|-----------|-----|
| Crossref, arXiv (id lookup) | **kept** | the core resolution, sub-second |
| OpenAlex (retraction) | **kept**, capped at `AUX_TIMEOUT` (3 s) | adds retraction detection — a real error |
| OpenLibrary (ISBN) | **kept**, capped, books only | the only way an `@book` verifies; self-limiting |
| INSPIRE-HEP | dropped | ~10 s/call and returns nothing usable for these queries |
| Crossref title search (auto-find DOI, errata) | dropped | ~7 s/call, miss-heavy |
| Semantic Scholar (abstract) | dropped | only feeds the `--llm` sweep, which the demo never runs |

The kept-but-slower sources (OpenAlex, OpenLibrary) are bounded by a separate short
`AUX_TIMEOUT`, so one slow host abandons just its own check instead of dragging the
request. Net effect: a typical run is a few seconds and a worst-case 10-entry all-DOI
bibliography stays under ~10 s, while the demo still catches the things that matter
(wrong year/author/volume, a dead or fabricated DOI, a retraction, an unverifiable
entry). Pass `fast=False` to `check_bib_text` for the full multi-source CLI check.

## Deploy to OVH shared "Web Hosting"

OVH shared hosting can't run a persistent Python server, but it runs **Python via
CGI**, which is all this needs.

1. **Upload** into your site's web root (`www/`) so the layout is:

   ```
   www/
     veracite/        ← the whole package, including data/*.json
     index.html       ← copied from web/
     app.js
     style.css
     check.cgi
     .htaccess        ← see below
   ```

   `check.cgi` adds its own directory to `sys.path`, so keep `check.cgi` and the
   `veracite/` folder **side by side** in `www/`. (Copy the package: `cp -r
   ../veracite www/` from the repo, or upload both over SFTP.)

2. **Enable CGI** for the directory with an `.htaccess` in `www/`:

   ```apache
   Options +ExecCGI
   AddHandler cgi-script .cgi
   ```

3. **Make the script executable and fix the shebang.** `chmod 755 check.cgi`. The
   shebang is `#!/usr/bin/env python3`; in the OVH control panel set the site's
   Python version to **3.8 or newer**. If `env python3` is not found, replace the
   first line with the absolute interpreter path OVH gives you (e.g.
   `#!/usr/bin/python3`).

4. **(Optional) Crossref polite pool.** Set `VERACITE_CONTACT_EMAIL` in the host
   environment (or a `veracite.json` settings file beside the package) so
   Crossref/OpenAlex calls carry a contact and get more reliable service.

5. **Visit** `https://yoursite/` and click **Check bibliography** (the textarea is
   pre-filled with a sample). The first run hits the live APIs and may take a few
   seconds.

### If it 500s

Check the OVH CGI error log. The usual causes, in order:

- **CGI not enabled / wrong handler** → the `.htaccess` above is missing or the host
  disallows `Options +ExecCGI` (some entry plans do — see the fallback below).
- **Wrong shebang / Python too old** → fix the first line / bump the Python version.
- **`veracite` not importable** → the `veracite/` folder isn't next to `check.cgi`,
  or `data/*.json` didn't upload.
- **Permissions** → `chmod 755 check.cgi`.

Test the endpoint directly:

```bash
curl -X POST --data-binary @../tests/fixtures/<some>.bib https://yoursite/check.cgi
```

## If your OVH plan forbids CGI

Some entry-level shared plans don't allow CGI execution. The same
`check_bib_text` runs unchanged behind a tiny server — deploy on a small OVH VPS (or
any host) with, e.g.:

```python
# app.py  (pip install flask, or use http.server)
from flask import Flask, request, jsonify
from veracite import check_bib_text
app = Flask(__name__, static_folder="web", static_url_path="")

@app.post("/check.cgi")          # same path the front end posts to
def check():
    return jsonify(check_bib_text(request.get_data(as_text=True),
                                  max_entries=10))   # fast mode + its own timeouts
```

Run it behind nginx/gunicorn. No front-end change is needed — `app.js` posts to
`check.cgi` either way.

## Local preview

From the `web/` directory, with the `veracite` package importable (run from the repo
root or `pip install -e .` first):

```bash
# serve the static page + run check.cgi as CGI
python -m http.server --cgi 8000 --directory .
# then open http://localhost:8000/   (the CGI must live under a cgi-bin/ path for
# http.server; see the project root verification notes for a direct test of check.cgi)
```

The most reliable local check is to call the function directly:

```bash
python -c "from veracite import check_bib_text; import json; \
print(json.dumps(check_bib_text(open('../tests/fixtures/clean.bib').read())['summary'], indent=2))"
```
