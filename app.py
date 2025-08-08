# app.py
import os
import re
import json
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, g
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, Integer, Text, DateTime
import requests
from bs4 import BeautifulSoup
import io
import random

# ---------- CONFIG ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "court_fetcher.db")
COURT_BASE_URL = "https://faridabad.dcourts.gov.in/case-status-search-by-case-number/"  # UI entry (for docs)
# Note: actual eCourts backend endpoints vary; we'll try the central services endpoint as fallback.
ECOURTS_SERVICE_BASE = "https://services.ecourts.gov.in/ecourtindia_v6/"

# Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "replace-this-with-secure-random")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + DB_PATH
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ---------- DB MODELS ----------
class QueryLog(db.Model):
    __tablename__ = "query_logs"
    id = Column(Integer, primary_key=True)
    case_type = Column(Text)
    case_number = Column(Text)
    filing_year = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)
    ip = Column(Text)

class RawResponse(db.Model):
    __tablename__ = "raw_responses"
    id = Column(Integer, primary_key=True)
    query_id = Column(Integer)
    raw_html = Column(Text)
    parsed_json = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

# create tables if not present
with app.app_context():
    db.create_all()

# ---------- SIMPLE MATH CAPTCHA ----------
def generate_captcha():
    a = random.randint(1, 12)
    b = random.randint(1, 12)
    op = random.choice(["+", "-"])
    if op == "-":
        # ensure non-negative
        if a < b:
            a, b = b, a
    question = f"{a} {op} {b}"
    answer = eval(question)
    return question, str(answer)

# ---------- SCRAPING / FETCHING LOGIC ----------
def detect_captcha_in_text(text):
    """Detect keyword hints that a captcha or challenge is present."""
    t = text.lower()
    if "captcha" in t or "enter the" in t and ("captcha" in t or "enter the captcha" in t):
        return True
    # some NIC pages show "Enter Captcha" or image-based captcha
    if "enter captcha" in t or "refresh image" in t:
        return True
    return False

def fetch_case_from_ecourts(case_type, case_number, filing_year):
    """
    Attempt to fetch case HTML. This is a best-effort call:
     - For district courts the central eCourts service sometimes exposes endpoints,
       but many pages are JS-driven or protected by captcha.
    We'll:
     1. Try an HTTP GET to a central services page with query params (if available).
     2. If HTML contains captcha, return an object signaling manual captcha required.
    """
    # Logging: build a simple query parameter payload (these parameters may differ)
    # We'll try a generic 'casestatus' endpoint as a best-effort.
    params = {
        "p": "casestatus/index",
        "filling_number": case_number,
        "year": filing_year,
        "case_type": case_type
    }

    # First attempt: central eCourts services entry page (GET)
    try:
        r = requests.get(ECOURTS_SERVICE_BASE, params=params, timeout=12, headers={
            "User-Agent": "Court-Data-Fetcher/1.0 (+https://github.com/yourname)"
        })
    except Exception as e:
        return {"error": "network", "message": f"Network error while contacting eCourts: {e}"}

    raw_html = r.text

    # Save raw HTML to DB will be handled by caller

    # Detect if the page requires captcha
    if detect_captcha_in_text(raw_html):
        return {"captcha_required": True, "raw_html": raw_html, "message": "Target site requires CAPTCHA / challenge. Manual solve required."}

    # Try to parse parties / filing / next hearing / latest order link
    soup = BeautifulSoup(raw_html, "html.parser")

    # Generic parsing heuristics (these pages have variable structure)
    # 1. Parties: try to find elements that contain 'Petitioner' or 'Petitioner Name' etc.
    parties = {}
    # look for labels
    text = soup.get_text(separator="\n")
    # Simple regex attempts
    petitioner = None
    respondent = None
    m_pet = re.search(r"Petitioner[s]?\:?\s*(.+)", text, re.IGNORECASE)
    if m_pet:
        petitioner = m_pet.group(1).splitlines()[0].strip()
    m_resp = re.search(r"Respondent[s]?\:?\s*(.+)", text, re.IGNORECASE)
    if m_resp:
        respondent = m_resp.group(1).splitlines()[0].strip()

    # filing and next hearing
    filing_date = None
    next_hearing = None
    m_filing = re.search(r"Filing Date\:?\s*([A-Za-z0-9 ,\-\/]+)", text, re.IGNORECASE)
    if m_filing:
        filing_date = m_filing.group(1).strip()
    m_next = re.search(r"Next Hearing Date\:?\s*([A-Za-z0-9 ,\-\/]+)", text, re.IGNORECASE)
    if m_next:
        next_hearing = m_next.group(1).strip()

    # Orders/judgments: find PDF links
    pdf_links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            text_label = a.get_text(strip=True)
            pdf_links.append({"label": text_label, "url": href})

    # Fallback: try to find /viewOrder or /viewJudgement patterns
    if not pdf_links:
        for a in soup.find_all("a", href=True):
            if "order" in a["href"].lower() or "judgement" in a["href"].lower() or "judgment" in a["href"].lower():
                pdf_links.append({"label": a.get_text(strip=True), "url": a["href"]})

    parsed = {
        "petitioner": petitioner,
        "respondent": respondent,
        "filing_date": filing_date,
        "next_hearing": next_hearing,
        "pdf_links": pdf_links
    }

    return {"captcha_required": False, "raw_html": raw_html, "parsed": parsed}

# ---------- ROUTES ----------
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        # generate captcha and store answer in session
        q, ans = generate_captcha()
        session["captcha_answer"] = ans
        session["captcha_q"] = q
        return render_template("index.html", captcha_q=q)

    # POST
    case_type = request.form.get("case_type", "").strip()
    case_number = request.form.get("case_number", "").strip()
    filing_year = request.form.get("filing_year", "").strip()
    user_captcha = request.form.get("captcha", "").strip()

    # Validate simple fields
    if not case_number or not filing_year or not case_type:
        flash("Please fill all form fields.", "danger")
        return redirect(url_for("index"))

    # Validate captcha
    expected = session.get("captcha_answer")
    if not expected or user_captcha != expected:
        flash("Captcha incorrect. Please try again.", "danger")
        # regenerate captcha
        q, ans = generate_captcha()
        session["captcha_answer"] = ans
        session["captcha_q"] = q
        return render_template("index.html", captcha_q=q, case_type=case_type, case_number=case_number, filing_year=filing_year)

    # Log query
    qlog = QueryLog(case_type=case_type, case_number=case_number, filing_year=filing_year, ip=request.remote_addr)
    db.session.add(qlog)
    db.session.commit()

    # Attempt fetch
    fetch_result = fetch_case_from_ecourts(case_type, case_number, filing_year)

    # Save raw response (if present)
    raw_html = fetch_result.get("raw_html") or ""
    r = RawResponse(query_id=qlog.id, raw_html=raw_html, parsed_json=json.dumps(fetch_result.get("parsed") or {}))
    db.session.add(r)
    db.session.commit()

    # handle errors and captcha
    if fetch_result.get("error"):
        flash(fetch_result.get("message", "Unknown error contacting court site."), "danger")
        return redirect(url_for("index"))

    if fetch_result.get("captcha_required"):
        # Show a helpful message — we detected a CAPTCHA on the court site.
        flash("The court site is requiring a CAPTCHA or challenge for this search. Our app logged the query and raw response. You can either:")
        flash("1) Manually open the court site and solve their CAPTCHA (we cannot bypass it automatically).")
        flash("2) Use the saved raw response to debug or try again later.")
        return render_template("result.html", parsed=None, captcha_block=True, raw_html_snippet=raw_html[:2000], query_id=qlog.id)

    parsed = fetch_result.get("parsed")
    # Render parsed details
    return render_template("result.html", parsed=parsed, raw_html_snippet=raw_html[:2000], query_id=qlog.id)

@app.route("/download_pdf")
def download_pdf():
    url = request.args.get("url")
    if not url:
        flash("Invalid PDF URL.", "danger")
        return redirect(url_for("index"))
    # Normalize relative URLs
    if url.startswith("/"):
        # try to make absolute using services domain
        url = "https://services.ecourts.gov.in" + url
    try:
        r = requests.get(url, stream=True, timeout=20)
    except Exception as e:
        flash(f"Failed to download PDF: {e}", "danger")
        return redirect(url_for("index"))
    # return as attachment
    return send_file(
        io.BytesIO(r.content),
        mimetype="application/pdf",
        as_attachment=True,
        attachment_filename="order.pdf"
    )

@app.route("/raw_response/<int:rid>")
def raw_response(rid):
    rr = RawResponse.query.get(rid)
    if not rr:
        flash("Not found.", "danger")
        return redirect(url_for("index"))
    return rr.raw_html, 200, {"Content-Type": "text/html; charset=utf-8"}

# ---------- RUN ----------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))
