import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.chdir(HERE)
import phase0_probe as p

fails = []
def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not cond:
        fails.append(label)

def load(path, ext="auto"):
    raw = open(path, "rb").read()
    raw, member = p.unpack_if_zip(raw)
    text, enc = p.decode_best(raw)
    recs, tag = p.parse_any(text, ext)
    return recs, tag, enc, member

print("TEST A: Big5-HKSCS encoded XML")
recs, tag, enc, _ = load("fixtures2/big5.xml", "xml")
print("    encoding detected:", enc, "| records:", len(recs))
print("    first:", recs[0])
check("5 records parsed", len(recs) == 5)
check("Chinese decoded correctly", recs[0].get("SUBJECT_TC") == "供應桌上電腦",
      repr(recs[0].get("SUBJECT_TC")))
check("dept decoded correctly", recs[0].get("DEPT") == "教育局", repr(recs[0].get("DEPT")))
st = p.field_stats(recs)
check("CJK detected on Chinese field", st["SUBJECT_TC"]["cjk_pct"] == 100.0)
check("REF is a key candidate", "REF" in p.key_candidate_fields(recs, st),
      str(p.key_candidate_fields(recs, st)))
check("CLOSE (a date) excluded from key candidates",
      "CLOSE" not in p.key_candidate_fields(recs, st))

print()
print("TEST B: namespaced XML, records nested one level deep")
recs2, tag2, _, _ = load("fixtures2/namespaced.xml", "xml")
print("    record element:", tag2, "| records:", len(recs2))
print("    first:", recs2[0])
check("4 nested records found", len(recs2) == 4, "got %d" % len(recs2))
check("namespace stripped from field names",
      any(k == "TenderRef" for k in recs2[0]), str(list(recs2[0].keys())))

print()
print("TEST C: HTML page must be REFUSED, not parsed into garbage")
try:
    load("fixtures2/landing.html", "html")
    check("HTML rejected", False, "it parsed HTML as data")
except ValueError as exc:
    check("HTML rejected with clear error", True, str(exc))

print()
print("TEST D: zipped CSV, UTF-8 BOM, bilingual headers")
recs3, tag3, enc3, member = load("fixtures2/awards.zip", "auto")
print("    zip member:", member, "| encoding:", enc3, "| records:", len(recs3))
print("    headers:", list(recs3[0].keys()))
check("picked the CSV member, not readme.txt", member == "ContractsAwarded.csv", str(member))
check("4 award rows", len(recs3) == 4, "got %d" % len(recs3))
check("BOM stripped from first header",
      "合約編號" in recs3[0] or "﻿合約編號" not in recs3[0], str(list(recs3[0].keys())[:1]))
check("Chinese supplier name intact",
      recs3[0].get("供應商名稱") == "大華科技有限公司", repr(recs3[0].get("供應商名稱")))

print()
print("TEST E: join across Big5 notices and zipped awards (different refs -> no key)")
st1 = p.field_stats(recs)
st3 = p.field_stats(recs3)
lines, exact = p.join_report("notices", recs, st1, "awards", recs3, st3)
check("correctly reports NO key (2001-2005 vs 3001-3004)", exact is False)

print()
print("TEST F: join across namespaced notices and zipped awards (SHOULD match)")
st2 = p.field_stats(recs2)
lines2, exact2 = p.join_report("notices", recs2, st2, "awards", recs3, st3)
print("\n".join(lines2[:14]))
check("correctly finds the key (GLD/2026/3001 vs GLD-2026-3001)", exact2 is True)

print()
print("TEST G: the real regression -- a site-wide RSS feed must not win")
# These are the two URLs discovery actually returned for the notices dataset.
# The RSS feed parsed cleanly and was chosen, producing 201 rows of nonsense.
cands = ["https://data.gov.hk/filestore/feeds/data_rss_en.xml",
         "https://pcms2.gld.gov.hk/iportal/TenderNotice.xml"]
ranked = p.rank_resources(cands)
print("    ranked:", ranked)
check("GLD tender file now ranks first",
      ranked[0] == "https://pcms2.gld.gov.hk/iportal/TenderNotice.xml", ranked[0])

rss_recs, rss_tag, _, _ = load("fixtures2/site_rss.xml", "xml")
print("    rss record element:", rss_tag, "| fields:", sorted(rss_recs[0]))
check("generic syndication feed is detected",
      p.looks_like_generic_feed(rss_recs, rss_tag) is True)
check("a real tender feed is NOT flagged as generic",
      p.looks_like_generic_feed(recs2, "TenderNotice") is False)
check("awards CSV is NOT flagged as generic",
      p.looks_like_generic_feed(recs3, "csv-rows") is False)

print()
print("=" * 62)
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL HARD TESTS PASSED")
