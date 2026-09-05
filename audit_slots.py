"""Read-only audit: which installed skins have a broken config.json?

Answers "why do only *some* of my skins freeze at match load" by
classifying every mod under ``<SD>/ultimate/mods/``.  It writes nothing —
run it as often as you like.

Two defects are detectable after the fact:

1. **Stripped share tables.**  ``_regenerate_config_json`` drops
   ``share-to-vanilla`` / ``share-to-added`` entries whose *destination*
   isn't a file inside the mod folder.  A share entry means "this path is
   NOT shipped — read it from that other path instead", so the
   destination is never on disk and every entry is deleted.  Everything
   the slot was meant to inherit (motion, camera, cmn, Metal/Kirby-copy
   meshes) then resolves to nothing.  Detected by diffing the installed
   ``config.json`` against the pristine copy still in ``.mod_cache/``.

2. **Undeclared added slots.**  Slots c08+ don't exist in ``data.arc``.
   Registering files under ``new-dir-files`` isn't enough — the directory
   tree has to be declared in ``new-dir-infos`` and based on an existing
   slot.  Detected structurally, no cache copy needed.

Usage:  python audit_slots.py [SD_DRIVE]      e.g.  python audit_slots.py E:\\
"""
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SHARE_TABLES = ("share-to-vanilla", "share-to-added")
SLOT_RE = re.compile(r"^c(\d{2})$", re.IGNORECASE)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _table_size(config, key):
    """Total destination paths across a share table."""
    tbl = (config or {}).get(key) or {}
    if not isinstance(tbl, dict):
        return 0
    return sum(len(v) for v in tbl.values() if isinstance(v, list))


def _occupied_slots(mod_path):
    """Slots this mod actually ships content for, from directory names."""
    slots = set()
    fighter_root = os.path.join(mod_path, "fighter")
    for root, dirs, _files in os.walk(fighter_root):
        for d in dirs:
            m = SLOT_RE.match(d)
            if m:
                slots.add(f"c{int(m.group(1)):02d}")
    return slots


def _find_pristine_config(mod_id):
    """Locate the un-rewritten config.json in .mod_cache/<mod_id>/."""
    if not mod_id:
        return None
    cache = os.path.join(SCRIPT_DIR, ".mod_cache", str(mod_id))
    if not os.path.isdir(cache):
        return None
    for root, _dirs, files in os.walk(cache):
        for fn in files:
            if fn.lower() == "config.json":
                return os.path.join(root, fn)
    return None


def _detect_sd():
    if len(sys.argv) > 1:
        return sys.argv[1]
    for letter in "DEFGHIJKL":
        drive = f"{letter}:\\"
        if os.path.isdir(os.path.join(drive, "ultimate", "mods")):
            return drive
    return None


def audit(sd_card):
    mods_root = os.path.join(sd_card, "ultimate", "mods")
    if not os.path.isdir(mods_root):
        print(f"No mods folder at {mods_root}")
        return 1

    rows = []
    for name in sorted(os.listdir(mods_root)):
        mod_path = os.path.join(mods_root, name)
        if not os.path.isdir(mod_path):
            continue

        installed = _read_json(os.path.join(mod_path, "config.json"))
        meta = _read_json(os.path.join(mod_path, ".gb_meta.json")) or {}
        slots = _occupied_slots(mod_path)
        added = sorted(s for s in slots if int(s[1:]) >= 8)

        problems = []
        notes = []

        # Which install path did this mod take?  config.json is only
        # rewritten when a slot was picked (install_to_sd only calls
        # _apply_slot_map under `if slot_map:` / `elif target_slot`), so
        # a mod installed at its native slot keeps the author's config
        # untouched — which is why those skins work.
        remapped = bool(meta.get("slot"))
        notes.append("slot picked at install — config.json was rewritten"
                     if remapped else
                     "installed at native slot — config.json left as shipped")

        # ── Defect 1: share tables stripped vs the pristine copy ──
        pristine_path = _find_pristine_config(meta.get("mod_id"))
        if pristine_path:
            pristine = _read_json(pristine_path)
            lost = {}
            for key in SHARE_TABLES:
                before = _table_size(pristine, key)
                after = _table_size(installed, key)
                if before > after:
                    lost[key] = (before, after)
            if lost:
                problems.append("SHARE TABLES STRIPPED")
                for key, (before, after) in lost.items():
                    notes.append(f"{key}: {before} -> {after} entries")
        elif installed is None:
            notes.append("ships no config.json (plain slot replacement)")
        else:
            notes.append("no cached original to compare against")

        # ── Defect 2: added slot never declared ──
        if added:
            declared = installed.get("new-dir-infos") if installed else None
            declared = declared if isinstance(declared, list) else []
            undeclared = [s for s in added
                          if not any(s in str(d) for d in declared)]
            if undeclared:
                problems.append("ADDED SLOT UNDECLARED")
                notes.append(
                    f"occupies {', '.join(undeclared)} but new-dir-infos "
                    f"does not declare {'it' if len(undeclared) == 1 else 'them'}")

        rows.append({
            "name": name,
            "slots": ", ".join(sorted(slots)) or "-",
            "problems": problems,
            "notes": notes,
        })

    broken = [r for r in rows if r["problems"]]
    ok = [r for r in rows if not r["problems"]]
    unverifiable = [r for r in ok
                    if any("no cached original" in n for n in r["notes"])]

    print(f"\n=== Slot config audit — {len(rows)} mod(s) under {mods_root} ===\n")

    if broken:
        print(f"--- LIKELY TO FREEZE AT MATCH LOAD ({len(broken)}) ---")
        for r in broken:
            print(f"\n  {r['name']}")
            print(f"    slots   : {r['slots']}")
            print(f"    problem : {'; '.join(r['problems'])}")
            for n in r["notes"]:
                print(f"    · {n}")
        print()

    if ok:
        print(f"--- NO CONFIG PROBLEM DETECTED ({len(ok)}) ---")
        for r in ok:
            note = f"  ({r['notes'][0]})" if r["notes"] else ""
            print(f"  {r['name']}  [{r['slots']}]{note}")
        print()

    if unverifiable:
        print(f"NOTE: {len(unverifiable)} mod(s) have no pristine copy left "
              f"in .mod_cache/, so a stripped share table cannot be proven "
              f"or ruled out for them.  Re-download one of those mods and "
              f"re-run this audit to check it properly.\n")

    if not broken:
        print("No config.json defects found. If skins still freeze, the "
              "cause is elsewhere — most likely two mods for the same "
              "fighter both shipping a per-fighter shared file such as "
              "effect/fighter/<name>/ef_<name>.eff, which is not "
              "slot-scoped and silently resolves last-write-wins.\n")
    return 0


if __name__ == "__main__":
    sd = _detect_sd()
    if not sd:
        print("Could not find an SD card with ultimate/mods.\n"
              "Pass the drive explicitly:  python audit_slots.py E:\\")
        raise SystemExit(1)
    raise SystemExit(audit(sd))
