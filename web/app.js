"use strict";

// A small sample bibliography: mostly correct, with a few planted errors VeraCite's
// ONLINE check catches against the real record, so a first visitor sees a meaningful
// report in one click. Every entry carries a DOI or arXiv id so the demo's fast mode
// (Crossref + arXiv only) resolves them all quickly.
//   - einstein1935  : clean, correct DOI (the EPR paper) -> VERIFIED
//   - shor1999      : correct DOI, but a deliberately WRONG year (1997 vs the
//                     record's 1999) -> metadata_mismatch, the subtle kind of error
//   - maldacena1998 : clean, correct DOI -> VERIFIED
//   - vaswani2017   : the "Attention Is All You Need" preprint by arXiv id only
//                     -> resolves via arXiv; truncated author list is noted
//   - higgs1964     : a FABRICATED DOI (real paper, invented identifier)
//                     -> dead_doi (an error)
const SAMPLE = `@article{einstein1935,
  author  = {Einstein, A. and Podolsky, B. and Rosen, N.},
  title   = {Can Quantum-Mechanical Description of Physical Reality Be Considered Complete?},
  journal = {Physical Review},
  year    = {1935},
  volume  = {47},
  pages   = {777--780},
  doi     = {10.1103/PhysRev.47.777}
}

@article{shor1999,
  author  = {Shor, Peter W.},
  title   = {Polynomial-Time Algorithms for Prime Factorization and Discrete Logarithms on a Quantum Computer},
  journal = {SIAM Review},
  year    = {1997},
  volume  = {41},
  pages   = {303--332},
  doi     = {10.1137/S0036144598347011}
}

@article{maldacena1998,
  author  = {Maldacena, Juan},
  title   = {The Large N Limit of Superconformal Field Theories and Supergravity},
  journal = {Advances in Theoretical and Mathematical Physics},
  year    = {1998},
  volume  = {2},
  pages   = {231--252},
  doi     = {10.4310/ATMP.1998.v2.n2.a1}
}

@article{vaswani2017,
  author = {Vaswani, Ashish and others},
  title  = {Attention Is All You Need},
  year   = {2017},
  eprint = {1706.03762},
  archivePrefix = {arXiv}
}

@article{higgs1964,
  author  = {Higgs, Peter W.},
  title   = {Broken Symmetries and the Masses of Gauge Bosons},
  journal = {Physical Review Letters},
  year    = {1964},
  volume  = {13},
  pages   = {508--509},
  doi     = {10.1103/PhysRevLett.99.999999}
}
`;

const $ = (sel) => document.querySelector(sel);
const bibEl = $("#bib");
const resultEl = $("#result");

bibEl.value = SAMPLE;

$("#sample").addEventListener("click", () => {
  bibEl.value = SAMPLE;
  resultEl.hidden = true;
  resultEl.innerHTML = "";
});

$("#check").addEventListener("click", runCheck);

async function runCheck() {
  const raw = bibEl.value;
  if (!raw.trim()) { show(`<div class="banner err">Paste a .bib first.</div>`); return; }
  const btn = $("#check");
  btn.disabled = true; btn.textContent = "Checking…";
  const stop = startProgress();
  try {
    const resp = await fetch("check.cgi", {
      method: "POST",
      headers: { "Content-Type": "text/plain; charset=utf-8" },
      body: raw,
    });
    const data = await resp.json();
    if (data.error) {
      const tb = data.traceback
        ? `<pre style="white-space:pre-wrap;font-size:12px;overflow:auto">${esc(data.traceback)}</pre>`
        : "";
      show(`<div class="banner err">${esc(data.error)}${tb}</div>`);
      return;
    }
    render(data);
  } catch (e) {
    show(`<div class="banner err">Could not reach the checker: ${esc(String(e))}</div>`);
  } finally {
    stop();
    btn.disabled = false; btn.textContent = "Check bibliography";
  }
}

// An indeterminate progress display while check.cgi runs. The CGI returns one
// response (no streaming), so this is an animated bar + an elapsed-time counter and
// rotating status lines that reflect the phases the check actually goes through --
// reassurance that work is happening, not a frozen page. Returns a stop() to clear
// the timers when the response arrives.
function startProgress() {
  // Messages roughly track the layers; later ones explain why a run can take longer
  // (a dead/missing DOI triggers a slower title search that recovers the real one).
  const phases = [
    "Parsing the bibliography…",
    "Resolving records against Crossref and arXiv…",
    "Checking for retractions (OpenAlex)…",
    "Recovering missing or dead DOIs by title search… (this is the slow part)",
    "Almost there — finishing the integrity score…",
  ];
  show(`<div class="progress">
    <div class="bar"><span></span></div>
    <p class="phase" id="phase">${phases[0]}</p>
    <p class="elapsed" id="elapsed">0s</p>
  </div>`);
  const t0 = Date.now();
  const elapsed = $("#elapsed");
  const phaseEl = $("#phase");
  const tick = setInterval(() => {
    if (elapsed) elapsed.textContent = Math.round((Date.now() - t0) / 1000) + "s";
  }, 250);
  let i = 0;
  const advance = setInterval(() => {
    i = Math.min(i + 1, phases.length - 1);
    if (phaseEl) phaseEl.textContent = phases[i];
  }, 4000);
  return () => { clearInterval(tick); clearInterval(advance); };
}

function render(data) {
  const parts = [];
  const s = data.summary || {};
  const score = (s.integrity_score === null || s.integrity_score === undefined)
    ? "–" : s.integrity_score;
  const verdict = [
    s.verified != null ? `${s.verified} verified` : null,
    s.unverified ? `${s.unverified} unverified` : null,
    s.mismatch ? `${s.mismatch} mismatch` : null,
  ].filter(Boolean).join(" · ");
  parts.push(`<div class="score">
    <span class="num">${score}</span><span class="out">/ 100 integrity</span>
    <span class="verdict">${esc(verdict || "")}</span></div>`);

  if (data.truncated) {
    parts.push(`<div class="banner warn">Showing the first ${data.max_entries} of
      ${data.n_entries} entries (this demo caps at ${data.max_entries}).</div>`);
  }

  const refs = data.references || [];
  // File-level findings (duplicates, brace balance, dropped keys) aren't tied to a
  // reference; surface any from the flat findings list under a "<file>" key.
  const fileIssues = (data.findings || []).filter((f) => f.key === "<file>");
  if (fileIssues.length) {
    parts.push(entryBlock("(whole file)", null, null, null, fileIssues));
  }

  for (const ref of refs) {
    parts.push(entryBlock(ref.key, ref.status, ref.confidence, ref.verify, ref.issues || []));
  }
  if (!refs.length && !fileIssues.length) {
    parts.push(`<div class="banner">No entries to check.</div>`);
  }
  show(parts.join("\n"));
}

function entryBlock(key, status, conf, verify, issues) {
  const badge = status ? `<span class="badge ${esc(status)}">${esc(status)}</span>` : "";
  const confTxt = (conf != null) ? ` (confidence ${conf})` : "";
  const link = verify ? ` &middot; <a href="${esc(verify)}" target="_blank" rel="noopener">verify</a>` : "";
  const statusLine = (status || verify)
    ? `<div class="status">${badge}${esc(status || "")}${confTxt}${link}</div>` : "";
  let body;
  if (issues.length) {
    body = `<ul class="issues">${issues.map(issueLine).join("")}</ul>`;
  } else {
    body = `<div class="clean">✓ no problems found</div>`;
  }
  return `<div class="entry"><h3>${esc(key)}</h3>${statusLine}${body}</div>`;
}

function issueLine(f) {
  const sev = (f.severity || "").toUpperCase();
  const tag = sev === "INFO" ? "note" : sev;
  let fix = "";
  if (f.suggested && (f.suggested.to !== undefined)) {
    const from = f.suggested.from !== undefined ? `'${esc(String(f.suggested.from))}' → ` : "";
    fix = ` <span class="fix">(suggested: ${from}'${esc(String(f.suggested.to))}')</span>`;
  }
  const line = f.line ? ` (line ${f.line})` : "";
  return `<li><span class="sev ${esc(sev)}">[${esc(tag)}]</span> ` +
    `<span class="cat">${esc(f.category || f.layer || "")}</span>${esc(line)}: ` +
    `${esc(f.message || "")}${fix}</li>`;
}

function show(html) { resultEl.hidden = false; resultEl.innerHTML = html; }
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
