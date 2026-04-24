#!/usr/bin/env python3
"""Inject autoslot and error handling into profile functions."""

import re

p = r'c:\Users\gvopa\OneDrive\Desktop\smash_night\smash_night.py'
with open(p, 'r', encoding='utf-8', errors='replace') as f:
    content = f.read()

# Inject background autoslot into _open_profile
old_open = '''    def _open_profile(self, profile_name):
        """Open a profile detail view showing each mod with thumbnails and remove buttons."""
        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return'''

new_open = '''    def _open_profile(self, profile_name):
        """Open a profile detail view showing each mod with thumbnails and remove buttons."""
        def _do_autoslot():
            try:
                slot_fix = autoslot_missing_profile_entries(profile_name)
                if slot_fix.get("assigned", 0):
                    print(f"  Auto-slotted {slot_fix['assigned']} missing entries.")
                if slot_fix.get("unslotted", 0):
                    print(f"  {slot_fix['unslotted']} skins unslotted (all slots used).")
            except Exception as e:
                import sys
                print(f"  Auto-slot error: {e}", file=sys.stderr)
        
        threading.Thread(target=_do_autoslot, daemon=True).start()

        profiles = load_profiles()
        profile = profiles.get(profile_name)
        if not profile:
            return'''

if old_open in content:
    content = content.replace(old_open, new_open)
    print("[OK] Injected autoslot into _open_profile")
else:
    print("[SKIP] _open_profile autoslot pattern not found (may be already present)")

# Add error handling to _show_profiles
old_show = '''    def _show_profiles(self):
        """Display all saved profiles as a scrollable list."""
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")

        for w in self.results_inner.winfo_children():
            w.destroy()

        profiles = load_profiles()
        self.results_label.configure(text=f"{len(profiles)} profile(s)")

        if not profiles:'''

new_show = '''    def _show_profiles(self):
        """Display all saved profiles as a scrollable list."""
        self.prev_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.page_label.configure(text="")

        for w in self.results_inner.winfo_children():
            w.destroy()

        try:
            profiles = load_profiles()
        except Exception as e:
            print(f"Error loading profiles: {e}", file=__import__('sys').stderr)
            tk.Label(self.results_inner,
                     text="Error loading profiles.",
                     bg=T.SURFACE, fg=T.RED,
                     font=(T.FONT, T.SZ_MD)).pack(pady=20)
            return

        self.results_label.configure(text=f"{len(profiles)} profile(s)")

        if not profiles:'''

if old_show in content:
    content = content.replace(old_show, new_show)
    print("[OK] Added error handling to _show_profiles")
else:
    print("[SKIP] _show_profiles error handling pattern not found")

# Write back
with open(p, 'w', encoding='utf-8') as f:
    f.write(content)

print("\nProfile functions updated successfully.")
