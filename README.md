# Libra Smart Library Management System

Flask backend + single-page frontend (`frontend/index.html`) with JSON storage.

## Setup (Windows PowerShell)
```powershell
cd Microproject
python -m venv venv
.\venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
python backend\app.py
```
Open http://localhost:5000 . Optional env vars: `LIBRA_PORT`, `LIBRA_HOST` (default 0.0.0.0 so the QR registration works on your LAN; use 127.0.0.1 to restrict), `LIBRA_ADMIN_PASSWORD` (initial admin password, only used when `data/users.json` is first created), `LIBRA_DEBUG=1`.

## Login
- Admin: `admin@gmail.com` / `admin123` (first run). The password is stored hashed in `data/users.json`. **Click "Change Password" in the sidebar after first login.**
- Students: register on the app. Passwords are hashed (Werkzeug). Sessions are Flask signed HttpOnly SameSite=Lax cookies (8 h). The signing key is generated into `data/.secret_key` (do not share/commit it).
- **Migration of old accounts:** old accounts lived in the browser's localStorage. When such a student signs in with the old email+password in the same browser, the account is automatically created on the server (hashed) and the plain-text copy is then removed from that browser. Accounts in other browsers cannot be read by the server: those students simply re-register with the same email.

## What was done
- **Shelf Locator** (sidebar, all users): search by title/author/ISBN/Book ID, availability, branch, block, floor, rack, shelf, breadcrumb, floor + rack/shelf diagram highlighting the book, "location not set" handling. Admin can add/edit locations there (`PUT /api/books/<id>/location`) and in the existing Inventory editor. Uses the existing `block/floor/rack/shelf/branch` fields only.
- **Complaints** persist in `data/complaints.json` (atomic writes). Reactions expire after 15 s (reaction cleared, record kept). Students see/create only their own; only admin changes status/ETA/notes.
- **Auth/RBAC:** every `/api/*` data route requires a session. Admin may save everything (validated: unique book IDs, duplicate-ISBN rejection, types). Students are restricted server-side to: self-issue to a student ID registered under their own name, return *requests*, reservations, seat booking, suggestions (always saved as Pending), adding a student ID, activity log. Student attempts to edit books, fines, approvals, etc. are ignored. CORS wildcard removed; JSON-only + same-origin check on writes; login rate limiting; Flask debugger off by default.
- **Data safety:** atomic writes, backup at each startup and every 10 min on save (`data/backups`, rolling), corrupt-file fallback to latest backup, defaults for new location fields. Your original files are in `data/backups/original_upload/`. No data was reset.

## Tests actually run
- `py_compile` on the backend; `node --check` on all 4 inline scripts: OK.
- 30 API integration checks against a running Flask server, all passed: anonymous access blocked (data/save/complaints); admin & student login, bad password; registration + duplicate/reserved email; hashed passwords; student cannot wipe/alter books/students/records or set locations; non-JSON rejected; duplicate ISBN and malformed payload rejected; location set; add/archive/delete book; student self-issue with stock deduction; issuing to another student's ID blocked; return request; admin approval with fine; complaint create/ownership/admin response/permissions; reaction expiry without deleting the record; logout invalidates session.
- Restart persistence: a fresh server process read the earlier location edit, books, records, complaints and logins correctly.

## Known limitations (honest list)
- **The browser UI was not clicked through** (no browser available in my environment). Frontend changes are syntax-checked and the API they call is tested, but please smoke-test login, Shelf Locator, dark mode and responsive layout yourself and report anything off.
- Dark mode/responsive layout: existing CSS untouched; new locator CSS includes dark-mode rules but was not visually verified. Seat/reservation/QR flows were not individually exercised (QR/server-IP endpoint now requires login).
- The frontend still saves the whole dataset as one blob; if two admins edit simultaneously, last save wins. Student writes are merged on the server so they cannot overwrite others' data.
- Students are matched to a student ID by name equality with their account name (existing app convention). Reservation/seat/suggestion identity fields are free text, as before.
- Student accounts have no email verification or self-service password reset; an admin can delete the user entry in `data/users.json` so the student re-registers.
- The legacy `books.html`, `dashboard.html`, `script.js` uploads were empty and `style.css` is unused by the app (styles are inline), so they are not included.

## Shelf locations
Run `python tools\assign_locations.py` (server stopped) to auto-assign locations to books without one. It backs up first; `--force` overwrites existing locations.

`backend/app.py` also fills missing locations automatically at startup (never overwrites existing ones). Set `LIBRA_AUTO_LOCATE=0` to disable.

## Deploy on Render (GitHub)
See the step-by-step in the chat/DEPLOY notes: private GitHub repo -> Render Web Service (Python), build `pip install -r requirements.txt`, start `gunicorn --chdir backend --workers 1 --threads 4 --bind 0.0.0.0:$PORT app:app`, attach a Disk at `/var/data`, env vars `LIBRA_DATA_DIR=/var/data`, `LIBRA_SECURE_COOKIES=1`, `LIBRA_SECRET_KEY`, `LIBRA_ADMIN_PASSWORD`, `LIBRA_PUBLIC_URL`. `render.yaml` is included.
