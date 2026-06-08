"""Apply the user's edits + my proposed final-7 resolutions and write the final categorization CSV."""
import csv

UPLOADED = "/sessions/wizardly-determined-sagan/mnt/uploads/cleanup_depth1_categorization.csv"
FINAL = "/sessions/wizardly-determined-sagan/mnt/Music Cleanup/cleanup_depth1_categorization_FINAL.csv"

# What I'm proposing for the last 6 + Phantom typo (each can be overridden by user)
PROPOSED_FINAL = {
    "Family Friendly": "COMPILATION",  # 52 untagged Disney/Barbie/Enchanted tracks, flat folder
    "Downloads": "IGNORE",  # all audio inside is unreadable (HeaderNotFoundError) -> goes to quarantine
    "Google Play": "IGNORE",  # truly empty
    "Melanie's": "IGNORE",  # truly empty
    "Misc": "IGNORE",  # 8 empty subfolders
    "New folder": "COMPILATION",  # Goth Oddity - A Tribute To David Bowie split across 2 artist subfolders
    "The Phantom Of The Opera Original London Cast": "SOUNDTRACK",  # was "s" - typo
}

def read_tolerant(path):
    raw = open(path, "rb").read().replace(b"\x00", b"").decode("utf-8", errors="replace")
    import io
    return list(csv.DictReader(io.StringIO(raw)))

rows = read_tolerant(UPLOADED)
applied = 0
for r in rows:
    name = r["folder_name"]
    if name in PROPOSED_FINAL:
        r["auto_category"] = PROPOSED_FINAL[name]
        r["confidence"] = "user-confirmed"
        r["reason"] = "user-resolved final 7"
        applied += 1

with open(FINAL, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader()
    w.writerows(rows)

from collections import Counter
print(f"Applied {applied} final resolutions")
print()
print("Final auto_category distribution:")
for k, n in Counter(r["auto_category"] for r in rows).most_common():
    print(f"  {k:<15} {n}")
print()
remaining_review = [r for r in rows if r["auto_category"] == "REVIEW"]
print(f"REVIEW remaining: {len(remaining_review)}")
for r in remaining_review:
    print(f"  {r['folder_name']}")
