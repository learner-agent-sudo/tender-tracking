import os, sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.chdir(HERE)
import phase0_probe as p

def load(path, ext):
    text, enc = p.decode_best(open(path, "rb").read())
    recs, tag = p.parse_any(text, ext)
    return recs, tag, enc

print("=" * 70)
print("TEST 1: XML notices parse")
print("=" * 70)
n_recs, n_tag, n_enc = load("fixtures/notices.xml", "xml")
print("records:", len(n_recs), "| record element:", n_tag, "| encoding:", n_enc)
print("first record:", n_recs[0])
assert len(n_recs) == 6, "expected 6 notice records"
assert n_recs[0]["TENDER_REF"] == "GLD/2026/1234"
n_stats = p.field_stats(n_recs)
print("\nfield stats:")
for k, v in n_stats.items():
    print("  {:14} fill={:5} distinct={:2} ratio={:5} cjk%={:5} url%={:5}".format(
        k, v["fill_pct"], v["distinct"], v["distinct_ratio"], v["cjk_pct"], v["url_pct"]))
assert n_stats["SUBJECT_TC"]["cjk_pct"] == 100.0, "should detect Chinese"
assert n_stats["SUBJECT"]["cjk_pct"] == 0.0, "English field should show no CJK"
assert n_stats["DOC_URL"]["url_pct"] == 100.0, "should detect URLs"
print("\n-> XML parse, CJK detection, URL detection all OK")

print()
print("=" * 70)
print("TEST 2: join key PRESENT (GLD/2026/1234 vs GLD-2026-1234)")
print("=" * 70)
a_recs, a_tag, _ = load("fixtures/awards_withkey.csv", "csv")
print("award records:", len(a_recs), "| headers:", list(a_recs[0].keys()))
a_stats = p.field_stats(a_recs)
lines, exact = p.join_report("notices", n_recs, n_stats, "awards", a_recs, a_stats)
print("\n".join(lines))
print("\nexact_found =", exact)
assert exact is True, "FAILED: should have detected the shared key across punctuation"
print("\n-> join detection across differing punctuation OK")

print()
print("=" * 70)
print("TEST 3: join key ABSENT -> falls back to title similarity")
print("=" * 70)
b_recs, b_tag, _ = load("fixtures/awards_nokey.csv", "csv")
b_stats = p.field_stats(b_recs)
lines2, exact2 = p.join_report("notices", n_recs, n_stats, "awards", b_recs, b_stats)
print("\n".join(lines2))
print("\nexact_found =", exact2)
assert exact2 is False, "FAILED: should NOT find a key here"
print()
print("--- fuzzy fallback ---")
print("\n".join(p.title_overlap_report(n_recs, n_stats, b_recs, b_stats)))
print("\n-> no-key path and fuzzy fallback OK")

print()
print("=" * 70)
print("ALL LOGIC TESTS PASSED")
print("=" * 70)
