import requests, json, sys
sys.path.insert(0, ".")
from gamebanana_browser import api_search_mods

# Test 1: Search "Reduced Effects" with Effects category (what happens when Effects is selected)
print("=== Effects category, query='Reduced Effects' ===")
total, recs = api_search_mods(query="Reduced Effects", category_id=1177, root_cat=1177)
print(f"Total: {total}, showing: {len(recs)}")
for r in recs[:5]:
    print(f"  {r['_sName']}")

# Test 2: Search "Reduced Effects" with All Other (merged path simulated)
print("\n=== Individual sub-cat calls (simulating All Other) ===")
for cid in [1177, 26521, 15929, 1760]:
    t, r = api_search_mods(query="Reduced Effects", category_id=cid, root_cat=cid, per_page=15)
    print(f"  cat {cid}: {t} results, {len(r)} returned")
    if r:
        print(f"    first: {r[0]['_sName']}")

# Test 3: No query, Effects category (just browsing)
print("\n=== Effects category, no query (browse) ===")
total, recs = api_search_mods(query="", category_id=1177, root_cat=1177)
print(f"Total: {total}, showing: {len(recs)}")
for r in recs[:3]:
    print(f"  {r['_sName']}")
