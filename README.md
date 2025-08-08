# Court-Data Fetcher & Mini-Dashboard (Faridabad)

**Court chosen:** Faridabad District Court (eCourts). Source: Faridabad case status page and Central eCourts portal. 

**Stack:** Python 3, Flask, SQLite, Requests, BeautifulSoup.

**Run:** see instructions in project root (app.py).

**CAPTCHA:**
- The app implements a *local math CAPTCHA* to avoid mass automated queries.
- If the court site presents its own CAPTCHA, the app detects that and stops; it logs the raw HTML and informs the user to manually visit the official court portal and solve the CAPTCHA. This is the recommended legal approach unless you obtain explicit permission or use a sanctioned CAPTCHA-solving service (documented).

**Files:**
- `app.py` — main app
- `templates/` — index + result pages
- `court_fetcher.db` — created at runtime
- `schema.sql` — optional DB schema
