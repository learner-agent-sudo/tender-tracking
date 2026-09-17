# Phase 0 probe

A one-off script to check four assumptions about Hong Kong public procurement
open data **before** we design a database schema around them.

It answers:

| # | Question | Why it matters |
|---|---|---|
| a | Do the feeds fetch and parse, and what fields do they really have? | Decides the schema |
| b | **Is there a shared identifier linking tender notices to awards?** | Decides whether contract history is a SQL join or a fuzzy-matching problem |
| c | Can a tender document be downloaded without a portal session? | Decides whether eligibility requirements can be extracted automatically at all |
| d | How bilingual is the data, field by field? | Decides column layout and how supplier-name matching has to work |

## Running it — no local machine needed

Go to the repository's **Actions** tab → **Phase 0 probe** → **Run workflow**.

The findings render on the run's **Summary** page, readable in a browser. The
raw data it fetched is attached to the run as a downloadable artifact
(`phase0-output`), so the script's reading of the data can always be checked
against the source.

The workflow takes optional inputs to override any source URL, for when
auto-discovery picks the wrong file.

### Or locally, if you have Python

```bash
python3 probe/phase0_probe.py
```

Python 3.8+. Standard library only — no `pip install`. Takes a few minutes; it
deliberately pauses between requests rather than hammering a government server.

```bash
python3 probe/phase0_probe.py --notices https://.../TenderNotice.xml
python3 probe/phase0_probe.py --no-docs        # skip the document probe
python3 probe/phase0_probe.py --out ./out      # change output directory
```

## Tests

```bash
cd probe/tests && python3 test_logic.py && python3 test_hard.py
```

Offline, no network, no dependencies. They run automatically before the probe
in CI. Covered:

- XML and CSV parsing; JSON path
- **join key present** across differing punctuation (`GLD/2026/1234` vs `GLD-2026-1234`)
- **join key absent** → falls back to measuring fuzzy-match viability
- title columns are *not* mistaken for a join key
- date and URL fields excluded from key candidacy
- Big5-HKSCS decoding, with Chinese text round-tripping intact
- XML namespaces stripped; records nested a level deeper still found
- zipped archives unpacked, picking the data member over a readme
- UTF-8 BOM stripped from CSV headers; bilingual headers preserved
- an HTML landing page is **refused**, not parsed into garbage rows

## Output

- `out/report.md` — the findings, in markdown
- `out/raw/` — every byte fetched, saved unmodified so you can check the
  script's reading of it against the source

Nothing is uploaded anywhere. `out/` is gitignored.

## Reading the join-key result

The report separates two things that are easy to confuse:

- **Identifier matches** — a genuine candidate join key. This is the answer to
  question (b).
- **Text matches** — two datasets sharing identical titles. Useful (it means
  fuzzy matching has something to work with) but titles are not stable
  identifiers. Never join on them alone.

A field appearing as an "identifier-like" candidate only means it is shaped
like an identifier. Confirm the field name actually means what you think
before joining on it — a contract sum is also a distinct number.

## Status

Logic is tested against synthetic fixtures (see Tests above) covering the
encoding, structure and failure modes HK government data files actually
exhibit.

**Not yet run against the live data.gov.hk endpoints.** Until it has been,
treat the expected field names, record structure and the answer to the join-key
question as unverified.
