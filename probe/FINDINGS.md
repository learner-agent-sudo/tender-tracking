# Phase 0 findings

Verified against live endpoints on 2026-09-17 (GitHub Actions run 35265236585).

## Sources

| Source | URL | Result |
|---|---|---|
| Tender notices | `https://pcms2.gld.gov.hk/iportal/TenderNotice.xml` | **54 records, 24 fields** |
| Contract awards | `https://www.gld.gov.hk/datagovhk/procurement/ContractsAwarded_EN.csv` | **98 records, 5 fields** |
| GITP provider list | data.gov.hk dataset page | **Not found** — no resource URL discoverable |

Chinese variants exist for awards (`ContractsAwarded_TC.csv`, `_SC.csv`).
`robots.txt` returns 404 on both `pcms2.gld.gov.hk` and `www.gld.gov.hk`.

## (b) The join key — it exists, but not where value-matching can see it

Zero shared values between the two datasets. That is expected, not a problem:
the notices feed lists **currently open** tenders and the awards file lists
contracts awarded in the **past 12 months**. An open tender has not been
awarded yet, so the snapshots are disjoint by construction.

Shape comparison shows the identifier spaces overlap:

| Field | Shape | Count | Example |
|---|---|---|---|
| awards `Tender Reference` | `A9999999999` | 80 / 98 | `A0600622024` |
| awards `Tender Reference` | `AA999999999` | 17 / 98 | `AD200782023` |
| notices `TenderNo` | `A9999999999` | 6 / 54 | `A6900102026` |
| notices `TenderNo` | *(39 distinct shapes total)* | — | `EDB 1168-2005-8030-9020-00012-P001(1)` |

**7 of 54 open notices use the same identifier format as the awards file.**

The split is structural: GLD-issued central tenders use `A`/`AA` + 9-10 digits
and are what the awards file almost entirely contains. Department-issued
tenders use free-form references (39 distinct shapes across 54 rows) and are
largely absent from this awards file — they are awarded and published
elsewhere.

**Consequence: the join is exact, on `TenderNo` = `Tender Reference`, for the
GLD-issued subset — but only once you have collected both ends over time.**
Every day without a snapshot is a permanently lost link, because the notices
feed only ever shows what is open right now.

Fuzzy title matching is not a substitute: 1.9% strong matches, and the best
example pairs *sodium chloride* with *sodium silicofluoride*. Different
contracts. Do not join on titles.

## (c) Documents are not reachable

Every notice carries a `Link` of the form
`https://pcms2.gld.gov.hk/iprod/#/STA00303?tenderReferenceNumber=<ref>`.
All return a **912-byte HTML shell** — a single-page app that renders via
JavaScript. No tender document is retrievable as a file from the feed.

The feed's own descriptive fields are largely placeholders:
`ETQtyDescriptionEnglish` is "Please refer to tender documents" in most rows
(28 distinct values across 54 records), and the same for `ETApplication` and
`DeliverySchedule`.

**So the eligibility-requirement extraction layer cannot be built from this
feed.** The requirements live in documents this source does not expose.

## (d) Bilingual: paired columns, in-file

The notices feed is fully bilingual as **paired fields**, not separate files:
`SubjectEnglish`/`SubjectChinese`, `ReqDepartmentNameEnglish`/`Chinese`,
`ETQtyDescription*`, `ETApplication*`, `DeliverySchedule*`,
`IssuingDepartmentName*`. Chinese fields are 92-100% populated.

The awards EN file is English-only; TC/SC are separate downloads.

## Data quality notes for the schema

- **`Amount` needs real parsing.** 8 distinct shapes. 29 of 98 rows are
  `Not applicable` — roughly 30% of awards carry no price at all. Others
  carry a trade-term suffix: `HK$52,940,000.00 F.I.S./Hong Kong`.
- **`Contract Award Date`** is `27-Jun-25` / `7-Mar-25` — two-digit year,
  unpadded day.
- **`Contractor(s)`** is plural and free text; 82 distinct across 98 rows.
  Entity resolution needed before any incumbent analysis.
- **`NewAddendumIndicator` and `RevClosingDateIndicator` exist in the feed** —
  addendum and deadline-change tracking is supported natively, no diffing
  required for the flag itself (still diff for the content).
- `Status` is `I` on all 54 rows; `DownloadOnly` is `N` on all 54.

## Scale

54 open tenders, 98 awards in 12 months. This is GLD central procurement only.
Works contracts (ETS), departmental procurement, Hospital Authority, MTR,
Airport Authority and the universities are all separate sources.
