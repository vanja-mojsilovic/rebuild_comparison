"""
Read URL pairs from the 'urls' tab of a Google Sheet, run a rebuild
comparison for each row, and append validated comparison rows in
the 'report' tab.

Triggered by the GitHub Actions workflow. Requires two env vars:
    GOOGLE_SERVICE_ACCOUNT_JSON  — full JSON contents of the SA key
    SPREADSHEET_ID                — Google Sheets ID
"""

import json
import os
import sys
import traceback
from datetime import datetime, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from scraper import scrape_page
from comparator import build_validated_rows


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
URLS_TAB = "urls"
REPORT_TAB = "report"

REPORT_HEADERS = [
    "Run timestamp",
    "Restaurant",
    "Old URL",
    "New URL",
    "Service",
    "Section pair",
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

# Column range used for reads/writes — grows with REPORT_HEADERS.
# A1 + 17 columns → A:Q
REPORT_RANGE = f"{REPORT_TAB}!A:Q"
REPORT_HEADER_RANGE = f"{REPORT_TAB}!A1:Q1"


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


def ensure_report_headers(service, spreadsheet_id):
    """
    Write the header row if missing. If the existing header row is the
    OLD shape (no 'Old HTML type' / 'New HTML type'), overwrite it with
    the new headers — existing data rows will just have blank values
    in those columns until the next run, which is fine.
    """
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=REPORT_HEADER_RANGE,
    ).execute()
    existing = result.get("values", [])
    if not existing or existing[0] != REPORT_HEADERS:
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{REPORT_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [REPORT_HEADERS]},
        ).execute()


def build_sheet_rows(timestamp, restaurant, old_url, new_url, comparison_rows):
    """Flatten validated comparison rows into spreadsheet rows."""
    out = []
    for r in comparison_rows:
        out.append([
            timestamp,
            restaurant,
            old_url,
            new_url,
            r.get("service", ""),
            r.get("section_pair", ""),
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


def append_to_report(service, spreadsheet_id, rows):
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=REPORT_RANGE,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


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
    ensure_report_headers(sheets, spreadsheet_id)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
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
            comparison_rows = build_validated_rows(old_data, new_data)
            sheet_rows = build_sheet_rows(
                timestamp, restaurant, old_url, new_url, comparison_rows
            )
            append_to_report(sheets, spreadsheet_id, sheet_rows)

            ok = sum(1 for r in comparison_rows if r.get("match") == "OK")
            issues = len(comparison_rows) - ok
            print(f"  ✓ wrote {len(sheet_rows)} rows  "
                  f"(OK: {ok}, issues: {issues})", flush=True)
        except Exception as e:
            failures += 1
            print(f"  ✗ FAILED: {e}", flush=True)
            traceback.print_exc()
            append_to_report(sheets, spreadsheet_id, [[
                timestamp, "(error)", old_url, new_url,
                "error", "", "", str(e)[:500], "", "", "", "", "", "", "", "", "ERROR",
            ]])

    print(f"\nDone. {len(pairs) - failures} succeeded, {failures} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()