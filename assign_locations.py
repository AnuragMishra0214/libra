"""Assign physical shelf locations (block/floor/rack/shelf) to books that have none.
Stop the Flask server first, then run:  python tools\\assign_locations.py   (add --force to overwrite existing)
Layout: one block per category (A, B, C...), books sorted by ID, 6 books per shelf, 5 shelves per rack,
4 racks per floor. Edit the constants below to match your real library, or fix single books in Shelf Locator."""
import json, os, shutil, sys, tempfile
from datetime import datetime
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
F = os.path.join(BASE, "data", "library_data.json")
PER_SHELF, SHELVES, RACKS_PER_FLOOR = 6, 5, 4
force = "--force" in sys.argv
d = json.load(open(F, encoding="utf-8"))
os.makedirs(os.path.join(BASE, "data", "backups"), exist_ok=True)
shutil.copy2(F, os.path.join(BASE, "data", "backups", f"library_data_before_locations_{datetime.now():%Y%m%d_%H%M%S}.json"))
cats = sorted({b.get("category") or "General" for b in d["booksData"]})
block = {c: chr(65 + i) for i, c in enumerate(cats)}
n = 0
for c in cats:
    books = sorted([b for b in d["booksData"] if (b.get("category") or "General") == c], key=lambda b: str(b["id"]))
    for i, b in enumerate(books):
        if not force and any(b.get(k) for k in ("block", "floor", "rack", "shelf")):
            continue
        shelf_no = i // PER_SHELF
        rack_no = shelf_no // SHELVES
        b["block"] = block[c]
        b["floor"] = str(rack_no // RACKS_PER_FLOOR + 1)
        b["rack"] = str(rack_no + 1)
        b["shelf"] = str(shelf_no % SHELVES + 1)
        n += 1
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(F), suffix=".tmp")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(d, f, indent=2, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
os.replace(tmp, F)
print(f"Assigned locations to {n} of {len(d['booksData'])} books. Blocks:", block)
