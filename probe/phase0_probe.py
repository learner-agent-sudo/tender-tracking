#!/usr/bin/env python3
"""
Phase 0 probe for the HK tender-tracking project.

Answers four questions about Hong Kong public procurement open data, so that the
database schema is designed against real records instead of assumptions:

  (a) SHAPE     -- do the feeds fetch and parse, and what fields do they have?
  (b) JOIN KEY  -- is there a shared identifier linking tender notices to awards?
  (c) DOCUMENTS -- can a tender document be fetched without a portal session?
  (d) LANGUAGE  -- how bilingual is the data, field by field?

Standard library only; no pip install. Nothing is uploaded anywhere. Every byte
fetched is written under ./out so you can inspect it yourself.

Usage:
    python3 phase0_probe.py                      # probe the default sources
    python3 phase0_probe.py --notices <URL>      # override a source URL
    python3 phase0_probe.py --no-docs            # skip the document-access probe
    python3 phase0_probe.py --out ./somewhere    # change the output directory

When it finishes, read out/report.md.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from xml.etree import ElementTree

USER_AGENT = "tender-tracking-phase0-probe/0.1 (one-off research probe)"
TIMEOUT = 45
MAX_BYTES = 25 * 1024 * 1024      # cap on any single download
DOC_PROBE_BYTES = 64 * 1024       # we only need the first chunk of a document
POLITE_DELAY = 2.0                # seconds between requests to the same host

# data.gov.hk dataset landing pages. The probe reads these pages to discover the
# actual resource file URLs, so it keeps working if those URLs change.
DEFAULT_SOURCES = {
    "notices": "https://data.gov.hk/en-data/dataset/hk-gld-gldetb-gldetb-tendernotice",
    "awards": "https://data.gov.hk/en-data/dataset/hk-gld-procure4-contracts-awarded",
    "gitp": "https://data.gov.hk/en-data/dataset/hk-ogcio-ogcio_hp-list-of-gitp-providers",
}

RESOURCE_RE = re.compile(
    r'https?://[^\s"\'<>()\\]+\.(?:xml|csv|json|txt|zip)(?:\?[^\s"\'<>()\\]*)?', re.I
)
URLISH_RE = re.compile(r'https?://[^\s"\'<>()\\]+', re.I)
CJK_RE = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
DATE_RE = re.compile(r'^\s*\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}')
LOGIN_HINTS = ("login", "logon", "sign in", "signin", "password", "user id",
               "登入", "登錄", "密碼", "session expired", "not authorised",
               "not authorized")

_last_request_at = {}


# ---------------------------------------------------------------- http + io


def polite_wait(url):
    """Space out requests per host so we are not hammering a government server."""
    host = urllib.parse.urlparse(url).netloc
    last = _last_request_at.get(host)
    if last is not None:
        gap = POLITE_DELAY - (time.time() - last)
        if gap > 0:
            time.sleep(gap)
    _last_request_at[host] = time.time()


def http_get(url, max_bytes=MAX_BYTES):
    """Fetch a URL. Returns (status, headers, body_bytes). Raises on transport error."""
    polite_wait(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en,zh-HK;q=0.8,zh;q=0.6",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read(max_bytes)
    except urllib.error.HTTPError as exc:
        # An HTTP error still carries a body, and that body is often the
        # interesting part (e.g. a login page rather than the document).
        body = b""
        try:
            body = exc.read(max_bytes)
        except Exception:
            pass
        return exc.code, dict(exc.headers or {}), body


def decode_best(raw):
    """Decode bytes, trying the encodings HK government files actually use."""
    for enc in ("utf-8-sig", "utf-8", "big5hkscs", "big5", "cp950", "latin-1"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8/replace"


def looks_like_html(text):
    """Distinguish an HTML page from an XML data file.

    Discovery can hand us a landing page instead of a data file. Without this
    guard the CSV parser happily turns HTML into nonsense "records", which is
    far worse than a clean failure.
    """
    head = text[:4000].lstrip().lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        return True
    return ("<body" in head or "<div" in head) and "<?xml" not in head


def unpack_if_zip(raw):
    """If raw is a zip, return the most data-looking member. HK archives are zipped."""
    if not raw.startswith(b"PK\x03\x04"):
        return raw, None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                return raw, "(empty zip)"
            ranked = sorted(names, key=lambda n: (
                0 if n.lower().endswith((".xml", ".csv", ".json")) else 1, len(n)))
            member = ranked[0]
            return archive.read(member), member
    except Exception as exc:
        return raw, "(unreadable zip: {})".format(exc)


def save_raw(outdir, name, raw):
    path = os.path.join(outdir, "raw", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(raw)
    return path


def guess_extension(url, headers):
    ctype = (headers.get("Content-Type") or "").lower()
    for ext in ("xml", "csv", "json"):
        if ext in ctype:
            return ext
    if "zip" in ctype:
        return "zip"
    path = urllib.parse.urlparse(url).path.lower()
    for ext in ("xml", "csv", "json", "pdf", "zip"):
        if path.endswith("." + ext):
            return ext
    if "html" in ctype:
        return "html"
    return "bin"


# ---------------------------------------------------------------- discovery


def discover_resources(page_url):
    """Find candidate data-file URLs on a data.gov.hk dataset page.

    Tries two routes: the CKAN-style package API, then scraping the page HTML.
    Returns a de-duplicated list, most promising first.
    """
    found = []
    dataset_id = urllib.parse.urlparse(page_url).path.rstrip("/").split("/")[-1]
    base = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(page_url))

    api_url = "{}/en-data/api/3/action/package_show?id={}".format(base, dataset_id)
    try:
        status, _headers, raw = http_get(api_url, max_bytes=4 * 1024 * 1024)
        if status == 200:
            payload = json.loads(decode_best(raw)[0])
            for res in payload.get("result", {}).get("resources", []) or []:
                url = res.get("url")
                if url:
                    found.append(url)
    except Exception:
        pass  # the API may not exist; the HTML scrape below is the fallback

    try:
        status, _headers, raw = http_get(page_url, max_bytes=8 * 1024 * 1024)
        if status == 200:
            text = decode_best(raw)[0]
            found.extend(RESOURCE_RE.findall(text))
    except Exception:
        pass

    seen, ordered = set(), []
    for url in found:
        url = url.rstrip('\\",\'')
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def rank_resources(urls):
    """Prefer English resources and real data extensions over everything else."""
    def score(url):
        low = url.lower()
        points = 0
        if low.endswith((".xml", ".csv", ".json")):
            points -= 10
        if re.search(r'(^|[^a-z])(en|eng|english)([^a-z]|$)', low):
            points -= 3
        if re.search(r'(sc|schinese|simplified)', low):
            points += 2
        return points
    return sorted(urls, key=score)


# ---------------------------------------------------------------- parsing


def flatten_element(elem, prefix=""):
    """Flatten one XML record into {leaf_path: text}."""
    out = {}
    for child in elem:
        tag = child.tag.split("}")[-1]
        path = "{}/{}".format(prefix, tag) if prefix else tag
        kids = list(child)
        if kids:
            out.update(flatten_element(child, path))
        else:
            value = (child.text or "").strip()
            if path in out and value:
                out[path] = out[path] + " | " + value
            else:
                out.setdefault(path, value)
        for attr, val in (child.attrib or {}).items():
            out["{}@{}".format(path, attr)] = (val or "").strip()
    return out


def parse_xml(text):
    """Parse XML into a list of record dicts by finding the repeating element."""
    root = ElementTree.fromstring(text)
    tag_counts = Counter(child.tag for child in root)
    if not tag_counts:
        return [flatten_element(root)], root.tag
    record_tag, count = tag_counts.most_common(1)[0]
    if count == 1:
        # Records are probably one level deeper (e.g. <root><body><item/>...).
        only = list(root)[0]
        deeper = Counter(child.tag for child in only)
        if deeper:
            record_tag, _ = deeper.most_common(1)[0]
            return ([flatten_element(node) for node in only if node.tag == record_tag],
                    record_tag.split("}")[-1])
    records = [flatten_element(node) for node in root if node.tag == record_tag]
    return records, record_tag.split("}")[-1]


def parse_csv(text):
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    records = []
    for row in reader:
        records.append({(k or "").strip(): (v or "").strip()
                        for k, v in row.items() if k is not None})
    return records, "csv-rows"


def parse_json(text):
    payload = json.loads(text)
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                payload = value
                break
    if isinstance(payload, list):
        return [{k: ("" if v is None else str(v)) for k, v in row.items()}
                for row in payload if isinstance(row, dict)], "json-array"
    return [], "json-unknown"


def parse_any(text, ext):
    if looks_like_html(text):
        raise ValueError("content is an HTML page, not a data file")
    stripped = text.lstrip()
    if ext == "xml" or stripped.startswith("<?xml") or stripped.startswith("<"):
        try:
            return parse_xml(text)
        except ElementTree.ParseError:
            pass
    if ext == "json" or stripped.startswith(("{", "[")):
        try:
            return parse_json(text)
        except json.JSONDecodeError:
            pass
    return parse_csv(text)


# ---------------------------------------------------------------- analysis


def normalise_key(value):
    return re.sub(r'[^A-Za-z0-9]', '', value or '').upper()


def looks_like_reference(value):
    """True if a value looks like an identifier rather than prose, a date or a URL.

    This is the guard that stops the join test from reporting "SUBJECT matches
    Subject" as if a shared title were a join key. A title matching a title
    tells you fuzzy matching is possible; it is not an identifier.
    """
    val = (value or "").strip()
    if not val or len(val) > 60:
        return False
    if DATE_RE.match(val) or URLISH_RE.search(val):
        return False
    if len(val.split()) > 4:            # prose, not an identifier
        return False
    norm = normalise_key(val)
    if not (4 <= len(norm) <= 40):
        return False
    if not any(c.isdigit() for c in norm):
        return False                   # a pure word token is never a reference
    if not any(c.isalpha() for c in norm) and len(norm) < 6:
        return False                   # a short bare number is just a number
    return True


def is_alnum_ref(value):
    """Real tender references almost always mix letters and digits."""
    val = (value or "").strip()
    return any(c.isalpha() for c in val) and any(c.isdigit() for c in val)


def field_stats(records):
    """Per-field: fill rate, distinctness, CJK share, and sample values."""
    stats = {}
    fields = []
    for rec in records:
        for key in rec:
            if key not in stats:
                stats[key] = {"nonempty": 0, "values": [], "distinct": set(),
                              "cjk": 0, "urls": 0}
                fields.append(key)
    for rec in records:
        for key in fields:
            value = (rec.get(key) or "").strip()
            entry = stats[key]
            if not value:
                continue
            entry["nonempty"] += 1
            entry["distinct"].add(value)
            if CJK_RE.search(value):
                entry["cjk"] += 1
            if URLISH_RE.search(value):
                entry["urls"] += 1
            if len(entry["values"]) < 3:
                entry["values"].append(value[:120])
    total = max(len(records), 1)
    out = {}
    for key in fields:
        entry = stats[key]
        ne = entry["nonempty"]
        out[key] = {
            "fill_pct": round(100.0 * ne / total, 1),
            "nonempty": ne,
            "distinct": len(entry["distinct"]),
            "distinct_ratio": round(len(entry["distinct"]) / ne, 3) if ne else 0.0,
            "cjk_pct": round(100.0 * entry["cjk"] / ne, 1) if ne else 0.0,
            "url_pct": round(100.0 * entry["urls"] / ne, 1) if ne else 0.0,
            "samples": entry["values"],
        }
    return out


def key_candidate_fields(records, stats, limit=12):
    """Fields whose values look like identifiers: well filled, distinct, ref-shaped."""
    scored = []
    for key, st in stats.items():
        if st["nonempty"] < 3 or st["distinct_ratio"] < 0.5:
            continue
        values = [(r.get(key) or "").strip() for r in records]
        values = [v for v in values if v][:300]
        if not values:
            continue
        ref_share = sum(1 for v in values if looks_like_reference(v)) / len(values)
        if ref_share < 0.6:
            continue
        alnum_share = sum(1 for v in values if is_alnum_ref(v)) / len(values)
        scored.append((ref_share + alnum_share + st["distinct_ratio"], key))
    scored.sort(reverse=True)
    return [key for _score, key in scored[:limit]]


def text_candidate_fields(records, stats, limit=6):
    """Prose fields (titles/subjects) -- useful for fuzzy matching, never a key."""
    out = []
    for key, st in stats.items():
        if st["nonempty"] < 3 or st["url_pct"] > 20:
            continue
        values = [(r.get(key) or "").strip() for r in records]
        values = [v for v in values if v][:300]
        if not values:
            continue
        prose_share = sum(1 for v in values if len(v.split()) >= 3) / len(values)
        if prose_share >= 0.6:
            out.append((prose_share, key))
    out.sort(reverse=True)
    return [key for _s, key in out[:limit]]


def join_report(a_label, a_recs, a_stats, b_label, b_recs, b_stats):
    """The core question: is there a shared IDENTIFIER linking the two datasets?

    Reports identifier matches and text matches separately. Only an identifier
    match counts as a join key; a shared title is merely a fuzzy-matching hint.
    """
    lines = []
    a_keys = key_candidate_fields(a_recs, a_stats)
    b_keys = key_candidate_fields(b_recs, b_stats)
    lines.append("Identifier-like fields in {}: {}".format(
        a_label, ", ".join("`{}`".format(k) for k in a_keys) or "(none found)"))
    lines.append("Identifier-like fields in {}: {}".format(
        b_label, ", ".join("`{}`".format(k) for k in b_keys) or "(none found)"))
    lines.append("")

    def norm_set(recs, key, predicate):
        out = set()
        for rec in recs:
            raw = (rec.get(key) or "").strip()
            if raw and predicate(raw):
                norm = normalise_key(raw)
                if len(norm) >= 4:
                    out.add(norm)
        return out

    # --- identifier matches: the real join-key test ---
    ref_results = []
    for ak in a_keys:
        avals = norm_set(a_recs, ak, looks_like_reference)
        if not avals:
            continue
        for bk in b_keys:
            bvals = norm_set(b_recs, bk, looks_like_reference)
            if not bvals:
                continue
            shared = avals & bvals
            if shared:
                pct = 100.0 * len(shared) / min(len(avals), len(bvals))
                alnum = sum(1 for v in shared if is_alnum_ref(v)) / len(shared)
                ref_results.append((alnum, len(shared), round(pct, 1), ak, bk,
                                    sorted(shared)[:3]))
    ref_results.sort(reverse=True)

    exact_found = bool(ref_results)
    if ref_results:
        lines.append("### Identifier matches (candidate join keys)")
        lines.append("")
        lines.append("| {} field | {} field | shared values | % of smaller set | examples |".format(
            a_label, b_label))
        lines.append("|---|---|---|---|---|")
        for _alnum, count, pct, ak, bk, examples in ref_results[:10]:
            lines.append("| `{}` | `{}` | {} | {}% | {} |".format(
                ak, bk, count, pct, ", ".join(examples)))
    else:
        lines.append("### Identifier matches (candidate join keys)")
        lines.append("")
        lines.append("**None.** No identifier-shaped field on one side shares any value")
        lines.append("with an identifier-shaped field on the other.")

    # --- text matches: informational only, NOT a key ---
    a_text = text_candidate_fields(a_recs, a_stats)
    b_text = text_candidate_fields(b_recs, b_stats)
    text_results = []
    for ak in a_text:
        avals = norm_set(a_recs, ak, lambda v: True)
        for bk in b_text:
            bvals = norm_set(b_recs, bk, lambda v: True)
            shared = avals & bvals
            if shared:
                pct = 100.0 * len(shared) / min(len(avals), len(bvals))
                text_results.append((len(shared), round(pct, 1), ak, bk))
    text_results.sort(reverse=True)
    lines.append("")
    lines.append("### Text matches (NOT a join key -- fuzzy-matching signal only)")
    lines.append("")
    if text_results:
        for count, pct, ak, bk in text_results[:5]:
            lines.append("  - `{}` and `{}` share {} identical values ({}% of the smaller set)".format(
                ak, bk, count, pct))
        lines.append("")
        lines.append("  Identical titles mean fuzzy matching has something to work with,")
        lines.append("  but titles are not stable identifiers -- never join on them alone.")
    else:
        lines.append("  - none")

    # --- containment: the reference may be embedded in a longer string ---
    lines.append("")
    lines.append("### Containment (is a {} reference buried inside {} text?)".format(
        a_label, b_label))
    lines.append("")
    refs = set()
    for ak in a_keys:
        refs |= norm_set(a_recs, ak, looks_like_reference)
    refs = sorted(v for v in refs if len(v) >= 6)[:400]
    hits = []
    if refs:
        scan_fields = list(dict.fromkeys(b_keys + b_text + list(b_stats)))[:20]
        for bk in scan_fields:
            found, example = 0, None
            for rec in b_recs[:1500]:
                hay = normalise_key(rec.get(bk, ""))
                if len(hay) < 6:
                    continue
                for ref in refs:
                    if ref in hay:
                        found += 1
                        example = example or (ref, (rec.get(bk) or "")[:80])
                        break
            if found:
                hits.append((found, bk, example))
    hits.sort(reverse=True)
    if hits:
        for found, bk, example in hits[:5]:
            lines.append('  - `{}` contains a {} reference in {} rows (e.g. {} inside "{}")'.format(
                bk, a_label, found, example[0], example[1]))
    else:
        lines.append("  - none found")

    return lines, exact_found


def title_overlap_report(a_recs, a_stats, b_recs, b_stats):
    """If there is no key, how viable is fuzzy matching on titles?"""
    def title_field(stats):
        best, best_len = None, 0
        for key, st in stats.items():
            if st["nonempty"] < 5:
                continue
            avg = sum(len(s) for s in st["samples"]) / max(len(st["samples"]), 1)
            if 15 < avg and avg > best_len and st["url_pct"] < 50:
                best, best_len = key, avg
        return best

    a_field, b_field = title_field(a_stats), title_field(b_stats)
    if not a_field or not b_field:
        return ["Could not identify title fields on both sides."]

    def tokens(value):
        return {t for t in re.split(r'[^A-Za-z0-9\u4e00-\u9fff]+', (value or "").lower())
                if len(t) > 2}

    b_tokens = [(tokens(r.get(b_field, "")), r.get(b_field, "")) for r in b_recs[:1500]]
    scores = []
    for rec in a_recs[:300]:
        at = tokens(rec.get(a_field, ""))
        if not at:
            continue
        best, best_text = 0.0, ""
        for bt, btext in b_tokens:
            if not bt:
                continue
            j = len(at & bt) / len(at | bt)
            if j > best:
                best, best_text = j, btext
        scores.append((best, rec.get(a_field, "")[:60], best_text[:60]))

    if not scores:
        return ["No comparable titles."]
    strong = sum(1 for s, _, _ in scores if s >= 0.6)
    medium = sum(1 for s, _, _ in scores if 0.35 <= s < 0.6)
    lines = [
        "Fuzzy title matching viability (using `{}` vs `{}`):".format(a_field, b_field),
        "  - sampled {} notices against {} awards".format(len(scores), len(b_tokens)),
        "  - strong match (Jaccard >= 0.60): {} ({}%)".format(
            strong, round(100.0 * strong / len(scores), 1)),
        "  - medium match (0.35-0.60): {} ({}%)".format(
            medium, round(100.0 * medium / len(scores), 1)),
        "",
        "  Best examples:",
    ]
    for score, atext, btext in sorted(scores, reverse=True)[:5]:
        lines.append('    {:.2f}  "{}"  ~  "{}"'.format(score, atext, btext))
    return lines


def document_probe(records, stats, outdir, limit=3):
    """(c) Can a linked tender document be fetched without a session?"""
    lines = []
    url_fields = [k for k, st in stats.items() if st["url_pct"] > 20]
    urls = []
    for rec in records:
        for key in url_fields or list(stats):
            match = URLISH_RE.search(rec.get(key, "") or "")
            if match:
                url = match.group(0).rstrip('.,;)"\'')
                if url not in urls:
                    urls.append(url)
        if len(urls) >= limit * 3:
            break

    if not urls:
        return ["No document URLs found in the notice records.",
                "That itself is a finding: the feed may only give references, not links."]

    lines.append("Found {} URL(s) in the records. Probing the first {}:".format(
        len(urls), min(limit, len(urls))))
    lines.append("")
    for url in urls[:limit]:
        try:
            status, headers, raw = http_get(url, max_bytes=DOC_PROBE_BYTES)
        except Exception as exc:
            lines.append("  - {}\n      ERROR: {}".format(url, exc))
            continue
        ctype = headers.get("Content-Type", "?")
        text_head = decode_best(raw[:8192])[0].lower()
        smells_like_login = any(hint in text_head for hint in LOGIN_HINTS)
        verdict = "LOOKS LIKE A REAL DOCUMENT"
        if status != 200:
            verdict = "HTTP {} -- not retrievable anonymously".format(status)
        elif smells_like_login:
            verdict = "LOGIN/GATE PAGE -- document is behind a session"
        elif "html" in ctype.lower():
            verdict = "HTML page (may be a landing page, not the document itself)"
        name = re.sub(r'[^A-Za-z0-9._-]', '_', url)[-80:]
        save_raw(outdir, "doc_probe_" + name, raw)
        lines.append("  - {}".format(url))
        lines.append("      status={} type={} bytes={}".format(status, ctype, len(raw)))
        lines.append("      => {}".format(verdict))
    return lines


def robots_report(hosts):
    lines = []
    for host in sorted(hosts):
        url = "https://{}/robots.txt".format(host)
        try:
            status, _headers, raw = http_get(url, max_bytes=64 * 1024)
            if status == 200:
                text = decode_best(raw)[0].strip()
                lines.append("### {}".format(url))
                lines.append("```")
                lines.append("\n".join(text.splitlines()[:40]))
                lines.append("```")
            else:
                lines.append("### {} -- HTTP {}".format(url, status))
        except Exception as exc:
            lines.append("### {} -- error: {}".format(url, exc))
    return lines


# ---------------------------------------------------------------- driver


def probe_source(label, page_or_file_url, outdir, report):
    report.append("## Source: {}".format(label))
    report.append("")
    report.append("Starting point: {}".format(page_or_file_url))

    if re.search(r'\.(xml|csv|json)(\?|$)', page_or_file_url, re.I):
        candidates = [page_or_file_url]
        report.append("Treated as a direct data file (no discovery needed).")
    else:
        candidates = rank_resources(discover_resources(page_or_file_url))
        report.append("")
        report.append("Discovered {} candidate resource URL(s):".format(len(candidates)))
        for url in candidates[:15]:
            report.append("  - {}".format(url))
        if not candidates:
            report.append("")
            report.append("**No resource files discovered.** Open the dataset page in a")
            report.append("browser, copy the real file URL, and re-run with --{} <URL>.".format(label))
            report.append("")
            return None

    for url in candidates[:6]:
        try:
            status, headers, raw = http_get(url)
        except Exception as exc:
            report.append("  ! fetch failed for {}: {}".format(url, exc))
            continue
        if status != 200 or not raw:
            report.append("  ! HTTP {} for {}".format(status, url))
            continue

        truncated = len(raw) >= MAX_BYTES
        raw, zip_member = unpack_if_zip(raw)
        ext = guess_extension(url, headers)
        if zip_member:
            low = zip_member.lower()
            for candidate in ("xml", "csv", "json"):
                if low.endswith("." + candidate):
                    ext = candidate
                    break
        text, encoding = decode_best(raw)
        try:
            records, record_tag = parse_any(text, ext)
        except Exception as exc:
            report.append("  ! parse failed for {}: {}".format(url, exc))
            continue
        if not records:
            report.append("  ! parsed zero records from {}".format(url))
            continue

        path = save_raw(outdir, "{}.{}".format(label, ext), raw)
        stats = field_stats(records)
        report.append("")
        report.append("### Parsed OK")
        report.append("")
        report.append("- URL: {}".format(url))
        report.append("- Saved raw: {}".format(path))
        report.append("- Format: {} | encoding: {} | record element: {}".format(
            ext, encoding, record_tag))
        if zip_member:
            report.append("- Unpacked from zip archive, member: `{}`".format(zip_member))
        if truncated:
            report.append("- **WARNING: download hit the {} MB cap and was truncated.** "
                          "Record count below is a floor, not the real total."
                          .format(MAX_BYTES // (1024 * 1024)))
        report.append("- **Records: {}** | fields: {}".format(len(records), len(stats)))
        report.append("")
        report.append("| field | fill % | distinct | distinct ratio | CJK % | sample |")
        report.append("|---|---|---|---|---|---|")
        for key, st in sorted(stats.items(), key=lambda kv: -kv[1]["fill_pct"]):
            sample = (st["samples"][0] if st["samples"] else "").replace("|", "/")
            report.append("| `{}` | {} | {} | {} | {} | {} |".format(
                key, st["fill_pct"], st["distinct"], st["distinct_ratio"],
                st["cjk_pct"], sample))
        report.append("")
        report.append("First record, verbatim:")
        report.append("```json")
        report.append(json.dumps(records[0], ensure_ascii=False, indent=2)[:2500])
        report.append("```")
        report.append("")
        return {"label": label, "url": url, "records": records, "stats": stats}

    report.append("")
    report.append("**Every candidate failed to parse.** Inspect out/raw/ by hand.")
    report.append("")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, url in DEFAULT_SOURCES.items():
        parser.add_argument("--" + name, default=url,
                            help="URL for the {} source (dataset page or data file)".format(name))
    parser.add_argument("--out", default="out", help="output directory (default: ./out)")
    parser.add_argument("--no-docs", action="store_true",
                        help="skip the tender-document access probe")
    parser.add_argument("--print-report", action="store_true",
                        help="also write the full report to stdout (useful in CI logs)")
    args = parser.parse_args()

    outdir = os.path.abspath(args.out)
    os.makedirs(outdir, exist_ok=True)

    report = [
        "# Phase 0 probe report",
        "",
        "Generated: {}".format(time.strftime("%Y-%m-%d %H:%M:%S %Z")),
        "Probe version: 0.1",
        "",
        "Four questions: (a) feed shape, (b) join key, (c) document access, (d) language.",
        "",
        "---",
        "",
    ]

    sources = {}
    for label in ("notices", "awards", "gitp"):
        try:
            result = probe_source(label, getattr(args, label), outdir, report)
        except Exception as exc:
            report.append("**Unhandled error probing {}: {}**".format(label, exc))
            report.append("")
            result = None
        if result:
            sources[label] = result
        report.append("---")
        report.append("")

    # (b) THE JOIN KEY
    report.append("## (b) Join key: notices <-> awards")
    report.append("")
    if "notices" in sources and "awards" in sources:
        lines, exact = join_report(
            "notices", sources["notices"]["records"], sources["notices"]["stats"],
            "awards", sources["awards"]["records"], sources["awards"]["stats"])
        report.extend(lines)
        report.append("")
        if exact:
            report.append("**VERDICT: a shared key appears to exist.** Confirm the top pair")
            report.append("above is semantically a tender reference, then join on it directly.")
        else:
            report.append("**VERDICT: no shared key found.** Fuzzy matching would be required;")
            report.append("viability below.")
            report.append("")
            report.extend(title_overlap_report(
                sources["notices"]["records"], sources["notices"]["stats"],
                sources["awards"]["records"], sources["awards"]["stats"]))
    else:
        report.append("Skipped: need both notices and awards to parse successfully.")
        report.append("Got: {}".format(", ".join(sources) or "nothing"))
    report.append("")
    report.append("---")
    report.append("")

    # (c) DOCUMENT ACCESS
    report.append("## (c) Tender document access (no login)")
    report.append("")
    if args.no_docs:
        report.append("Skipped (--no-docs).")
    elif "notices" in sources:
        report.extend(document_probe(sources["notices"]["records"],
                                     sources["notices"]["stats"], outdir))
    else:
        report.append("Skipped: the notices feed did not parse.")
    report.append("")
    report.append("---")
    report.append("")

    # (d) LANGUAGE
    report.append("## (d) Bilingual coverage")
    report.append("")
    for label, src in sources.items():
        cjk_fields = {k: st["cjk_pct"] for k, st in src["stats"].items()
                      if st["cjk_pct"] > 0}
        report.append("**{}**: {} of {} fields contain any Chinese text.".format(
            label, len(cjk_fields), len(src["stats"])))
        for key, pct in sorted(cjk_fields.items(), key=lambda kv: -kv[1])[:10]:
            report.append("  - `{}`: {}% of values contain CJK".format(key, pct))
        if not cjk_fields:
            report.append("  - none; this resource is English-only "
                          "(a separate Chinese file probably exists)")
        report.append("")
    report.append("---")
    report.append("")

    # robots.txt, for the record
    report.append("## robots.txt of touched hosts")
    report.append("")
    hosts = {urllib.parse.urlparse(src["url"]).netloc for src in sources.values()}
    report.extend(robots_report(hosts) if hosts else ["No hosts touched."])
    report.append("")

    report_path = os.path.join(outdir, "report.md")
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(report) + "\n")

    if args.print_report:
        print("=" * 68)
        print("BEGIN REPORT")
        print("=" * 68)
        print("\n".join(report))
        print("=" * 68)
        print("END REPORT")
        print("=" * 68)

    print("\n" + "=" * 68)
    print("Phase 0 probe finished.")
    print("=" * 68)
    for label in ("notices", "awards", "gitp"):
        if label in sources:
            print("  {:8} OK  {} records".format(label, len(sources[label]["records"])))
        else:
            print("  {:8} FAILED (see report)".format(label))
    print("")
    print("  Report:   {}".format(report_path))
    print("  Raw data: {}".format(os.path.join(outdir, "raw")))
    print("")
    print("Send me out/report.md and we design the schema against it.")
    print("")


if __name__ == "__main__":
    sys.exit(main())
