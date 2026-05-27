"""
Read URL pairs from the 'urls' tab of a Google Sheet, run a rebuild
comparison for each row, and append the extracted content as rows in
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
from comparator import build_sections_view


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
URLS_TAB = "urls"
REPORT_TAB = "report"

REPORT_HEADERS = [
    "Run timestamp",
    "Restaurant",
    "Old URL",
    "New URL",
    "Side",
    "Section #",
    "Service",
    "Element",
    "Text",
    "Href",
]


def get_sheets_service():
    """Authenticate using the JSON in the GOOGLE_SERVICE_ACCOUNT_JSON env var."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON env var is not set",
              file=sys.stderr)
        sys.exit(1)
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_url_pairs(service, spreadsheet_id):
    """Read the 'urls' tab. First row is the header; following rows are pairs."""
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{URLS_TAB}!A:B",
    ).execute()
    rows = result.get("values", [])

    if not rows:
        return []

    # Skip header row, filter out empty / malformed rows
    pairs = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        old, new = (row[0] or "").strip(), (row[1] or "").strip()
        if old and new and old.startswith("http") and new.startswith("http"):
            pairs.append((old, new))
    return pairs


def ensure_report_headers(service, spreadsheet_id):
    """If the report tab is empty, write the header row first."""
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{REPORT_TAB}!A1:J1",
    ).execute()
    if not result.get("values"):
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{REPORT_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [REPORT_HEADERS]},
        ).execute()


def build_rows_for_comparison(timestamp, old_url, new_url, view):
    """Flatten a comparison view into a list of report rows."""
    restaurant = (
        view.get("restaurant", {}).get("new")
        or view.get("restaurant", {}).get("old")
        or ""
    )

    rows = []
    for side, sections_key in (("old", "old_sections"), ("new", "new_sections")):
        for section_idx, section in enumerate(view.get(sections_key, []), start=1):
            service = section.get("service", "other")
            for item in section.get("rows", []):
                rows.append([
                    timestamp,
                    restaurant,
                    old_url,
                    new_url,
                    side,
                    section_idx,
                    service,
                    item.get("tag", ""),
                    item.get("text", ""),
                    item.get("href", ""),
                ])
    return rows


def append_to_report(service, spreadsheet_id, rows):
    """Append rows to the report tab."""
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{REPORT_TAB}!A:J",
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
        print("No valid URL pairs found in the 'urls' tab. Nothing to do.")
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
            view = build_sections_view(old_data, new_data)
            rows = build_rows_for_comparison(timestamp, old_url, new_url, view)
            append_to_report(sheets, spreadsheet_id, rows)
            print(f"  ✓ wrote {len(rows)} rows", flush=True)
        except Exception as e:
            failures += 1
            print(f"  ✗ FAILED: {e}", flush=True)
            traceback.print_exc()
            # Write a single failure row so the run is visible in the sheet
            append_to_report(sheets, spreadsheet_id, [[
                timestamp, "(error)", old_url, new_url,
                "error", "", "", "", str(e)[:500], "",
            ]])

    print(f"\nDone. {len(pairs) - failures} succeeded, {failures} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()