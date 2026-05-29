"""
Read URL pairs from the 'urls' tab of a Google Sheet, run a rebuild
comparison for each row, and append validated comparison rows in the
'report' tab.

Also writes two derived summary tabs:
  - 'sections': one row per paired (old, new) section with html types
  - 'seo':      one row per restaurant run with the page-level H1s on each side

Triggered by the GitHub Actions workflow. Requires two env vars:
    GOOGLE_SERVICE_ACCOUNT_JSON  — full JSON contents of the SA key
    SPREADSHEET_ID                — Google Sheets ID
"""

import json
import os
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from scraper import scrape_page
from comparator import (
    build_validated_rows,
    build_section_pairs,
    summarize_h1,
)
from ai_classifier import classify_sections_pair


# All report timestamps are written in Belgrade local time (Europe/Belgrade,
# CET/CEST with DST). Keeping a single timezone here makes ordering across
# tabs unambiguous and matches the reviewer's working hours.
REPORT_TZ = ZoneInfo("Europe/Belgrade")


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
URLS_TAB = "urls"
REPORT_TAB = "report"
SECTIONS_TAB = "sections"
SEO_TAB = "seo"


# ----- Headers for each tab ----------------------------------------------

REPORT_HEADERS = [
    "Run timestamp",
    "Restaurant",
    "Old URL",
    "New URL",
    "Service",
    "Old element",
    "Old text",
    "Old href",
    "Old hidden",
    "Old HTML type",
    "New element",
    "New text",
    "New href",
    "New hidden",
    "New HTML type",
    "Match",
]
# Report tab spans 16 columns → A:P
REPORT_RANGE = f"{REPORT_TAB}!A:P"
REPORT_HEADER_RANGE = f"{REPORT_TAB}!A1:P1"


SECTIONS_HEADERS = [
    "Restaurant name",
    "Run timestamp",
    "Old site section name",
    "Old HTML type",
    "Old heading text",
    "Old AI section type",
    "New site section name",
    "New HTML type",
    "New heading text",
    "New AI section type",
]
# Sections tab spans 10 columns → A:J
SECTIONS_RANGE = f"{SECTIONS_TAB}!A:J"
SECTIONS_HEADER_RANGE = f"{SECTIONS_TAB}!A1:J1"


SEO_HEADERS = [
    "Restaurant name",
    "Run timestamp",
    "Old H1 status",
    "Old H1 text",
    "Old H1 visibility",
    "New H1 status",
    "New H1 text",
    "New H1 visibility",
]
# SEO tab spans 8 columns → A:H
SEO_RANGE = f"{SEO_TAB}!A:H"
SEO_HEADER_RANGE = f"{SEO_TAB}!A1:H1"


def get_sheets_service():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON env var is not set", file=sys.stderr)
        sys.exit(1)
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_url_pairs(service, spreadsheet_id):
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{URLS_TAB}!A:B",
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return []

    pairs = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        old, new = (row[0] or "").strip(), (row[1] or "").strip()
        if old and new and old.startswith("http") and new.startswith("http"):
            pairs.append((old, new))
    return pairs


def _ensure_headers(service, spreadsheet_id, header_range, write_anchor, headers):
    """
    Write the header row if missing OR if the existing row doesn't match
    the expected shape. Existing data rows underneath are left in place;
    if the schema changed they may be shifted, which is the caller's call
    to clean up separately.
    """
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=header_range,
    ).execute()
    existing = result.get("values", [])
    if not existing or existing[0] != headers:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=write_anchor,
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()


def ensure_all_headers(service, spreadsheet_id):
    """Ensure all three tabs have correct headers in row 1."""
    _ensure_headers(service, spreadsheet_id,
                    REPORT_HEADER_RANGE, f"{REPORT_TAB}!A1", REPORT_HEADERS)
    _ensure_headers(service, spreadsheet_id,
                    SECTIONS_HEADER_RANGE, f"{SECTIONS_TAB}!A1", SECTIONS_HEADERS)
    _ensure_headers(service, spreadsheet_id,
                    SEO_HEADER_RANGE, f"{SEO_TAB}!A1", SEO_HEADERS)


def clear_data_rows(service, spreadsheet_id):
    """
    Wipe every row below the header (row 1) in each result tab BEFORE the
    run appends new data, so each run produces a clean snapshot rather than
    appending to old runs. The header row itself is left untouched.

    Uses values.clear on the tab name with no row 1 — Sheets interprets the
    tab-only reference as "all data in the tab" and clear leaves no choice
    of starting row, so we use an explicit "A2:" range per tab to skip row 1.
    """
    for tab in (REPORT_TAB, SECTIONS_TAB, SEO_TAB):
        service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=f"{tab}!A2:ZZ",
        ).execute()


# ----- Row builders ------------------------------------------------------

def build_report_rows(timestamp, restaurant, old_url, new_url, comparison_rows):
    """Flatten validated comparison rows into spreadsheet rows for the report tab."""
    out = []
    for r in comparison_rows:
        out.append([
            timestamp,
            restaurant,
            old_url,
            new_url,
            r.get("service", ""),
            r.get("old_element", ""),
            r.get("old_text", ""),
            r.get("old_href", ""),
            r.get("old_hidden", ""),
            r.get("old_html_type", ""),
            r.get("new_element", ""),
            r.get("new_text", ""),
            r.get("new_href", ""),
            r.get("new_hidden", ""),
            r.get("new_html_type", ""),
            r.get("match", ""),
        ])
    return out


def build_sections_tab_rows(restaurant, timestamp, section_pairs, ai_labels=None):
    """
    One row per paired section for the sections tab.

    ai_labels (optional) is a dict mapping "old_{idx}" / "new_{idx}" to the
    AI-determined label, as returned by ai_classifier.classify_sections_pair.
    When None or missing keys, the AI columns are left blank.
    """
    ai_labels = ai_labels or {}
    out = []
    for p in section_pairs:
        old_idx = p.get("old_index")
        new_idx = p.get("new_index")
        old_ai = ai_labels.get(f"old_{old_idx}", "") if old_idx is not None else ""
        new_ai = ai_labels.get(f"new_{new_idx}", "") if new_idx is not None else ""

        out.append([
            restaurant,
            timestamp,
            p.get("old_section_name", ""),
            p.get("old_html_type", ""),
            p.get("old_heading_text", ""),
            old_ai,
            p.get("new_section_name", ""),
            p.get("new_html_type", ""),
            p.get("new_heading_text", ""),
            new_ai,
        ])
    return out


def build_seo_tab_rows(restaurant, timestamp, old_data, new_data):
    """
    One row per restaurant run for the seo tab.

    For each side (old / new) we emit three columns:
      - H1 status:     "text" / "empty" / "missing"
      - H1 text:       joined text of all non-empty H1s on that side (or "")
      - H1 visibility: "visible" / "hidden" / "mixed" / ""

    "missing" means no <h1> tag exists on the page at all.
    "empty" means an <h1> tag exists but has no text content.
    "text" means at least one <h1> with non-empty text exists.

    The visibility flag is "" when status is "missing", and otherwise
    reflects whether the H1 element (or any of its ancestors) is hidden
    via class, inline style, aria-hidden, or the hidden attribute.
    """
    old_h1 = summarize_h1(old_data)
    new_h1 = summarize_h1(new_data)
    return [[
        restaurant,
        timestamp,
        old_h1["status"],
        old_h1["text"],
        old_h1["visibility"],
        new_h1["status"],
        new_h1["text"],
        new_h1["visibility"],
    ]]


# ----- Append helpers ----------------------------------------------------

def _append(service, spreadsheet_id, sheet_range, rows):
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=sheet_range,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def append_to_report(service, spreadsheet_id, rows):
    _append(service, spreadsheet_id, REPORT_RANGE, rows)


def append_to_sections(service, spreadsheet_id, rows):
    _append(service, spreadsheet_id, SECTIONS_RANGE, rows)


def append_to_seo(service, spreadsheet_id, rows):
    _append(service, spreadsheet_id, SEO_RANGE, rows)


# ----- Main --------------------------------------------------------------

def main():
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        print("ERROR: SPREADSHEET_ID env var is not set", file=sys.stderr)
        sys.exit(1)

    sheets = get_sheets_service()

    print("Reading URL pairs from sheet...", flush=True)
    pairs = read_url_pairs(sheets, spreadsheet_id)
    if not pairs:
        print("No valid URL pairs found in the 'urls' tab.")
        return

    print(f"Found {len(pairs)} URL pair(s) to process.", flush=True)
    ensure_all_headers(sheets, spreadsheet_id)
    print("Clearing previous results from report/sections/seo tabs...", flush=True)
    clear_data_rows(sheets, spreadsheet_id)

    timestamp = datetime.now(REPORT_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    failures = 0

    for i, (old_url, new_url) in enumerate(pairs, start=1):
        print(f"\n[{i}/{len(pairs)}] {old_url}  →  {new_url}", flush=True)
        try:
            old_data = scrape_page(old_url)
            new_data = scrape_page(new_url)
            restaurant = (
                new_data.get("restaurant_name")
                or old_data.get("restaurant_name")
                or "(unknown)"
            )

            # ---- main report tab ----
            comparison_rows = build_validated_rows(old_data, new_data)
            report_rows = build_report_rows(
                timestamp, restaurant, old_url, new_url, comparison_rows
            )
            append_to_report(sheets, spreadsheet_id, report_rows)

            # ---- sections tab ----
            section_pairs = build_section_pairs(old_data, new_data)

            # Optional AI-determined section types. Returns {} if the API
            # key is missing or anything goes wrong — in that case the AI
            # columns will simply stay empty.
            ai_labels = classify_sections_pair(
                restaurant,
                old_data.get("sections", []),
                new_data.get("sections", []),
            )

            sections_rows = build_sections_tab_rows(
                restaurant, timestamp, section_pairs, ai_labels=ai_labels
            )
            append_to_sections(sheets, spreadsheet_id, sections_rows)

            # ---- seo tab ----
            seo_rows = build_seo_tab_rows(
                restaurant, timestamp, old_data, new_data
            )
            append_to_seo(sheets, spreadsheet_id, seo_rows)

            ok = sum(1 for r in comparison_rows if r.get("match") == "OK")
            issues = len(comparison_rows) - ok
            ai_mark = "✓" if ai_labels else "✗"
            print(f"  ✓ report: {len(report_rows)} rows (OK: {ok}, issues: {issues})  "
                  f"sections: {len(sections_rows)} (ai {ai_mark})  seo: {len(seo_rows)}",
                  flush=True)
        except Exception as e:
            failures += 1
            print(f"  ✗ FAILED: {e}", flush=True)
            traceback.print_exc()
            append_to_report(sheets, spreadsheet_id, [[
                timestamp, "(error)", old_url, new_url,
                "error", "", str(e)[:500], "", "", "", "", "", "", "", "", "ERROR",
            ]])

    print(f"\nDone. {len(pairs) - failures} succeeded, {failures} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()