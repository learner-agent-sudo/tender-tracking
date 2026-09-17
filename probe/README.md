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

## Running it

```bash
python3 probe/phase0_probe.py
```

Python 3.8+. Standard library only — no `pip install`. Takes a few minutes;
it deliberately pauses between requests rather than hammering a government
server.

If a source fails to auto-discover, open the data.gov.hk dataset page in a
browser, copy the actual file URL, and pass it in:

```bash
python3 probe/phase0_probe.py --notices https://.../TenderNotice.xml
python3 probe/phase0_probe.py --no-docs        # skip the document probe
python3 probe/phase0_probe.py --out ./out      # change output directory
```

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

Logic tested against synthetic fixtures covering: XML and CSV parsing, key
present (across differing punctuation, e.g. `GLD/2026/1234` vs
`GLD-2026-1234`), key absent, CJK detection, and URL detection. It has **not**
yet been run against the real endpoints.
