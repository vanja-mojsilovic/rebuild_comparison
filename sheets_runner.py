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

from scraper import scrape_page, find_relevant_page_urls
from comparator import (
    build_validated_rows,
    build_section_pairs,
    build_h1_pairs,
    summarize_h1,
)
from ai_classifier import classify_sections_pair
from content_validator import validate_sections, validate_reviews


# All report timestamps are written in Belgrade local time (Europe/Belgrade,
# CET/CEST with DST). Keeping a single timezone here makes ordering across
# tabs unambiguous and matches the reviewer's working hours.
REPORT_TZ = ZoneInfo("Europe/Belgrade")


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
URLS_TAB = "urls"
REPORT_TAB = "report"
SECTIONS_TAB = "sections"
SEO_TAB = "seo"
CUSTOM_TAB = "custom"
CONTENT_TAB = "content"


# ----- Headers for each tab ----------------------------------------------

REPORT_HEADERS = [
    "Run timestamp",
    "Restaurant",
    "Old URL",
    "New URL",
    "Old site section name",
    "New site section name",
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
# Report tab spans 17 columns → A:Q
REPORT_RANGE = f"{REPORT_TAB}!A:Q"
REPORT_HEADER_RANGE = f"{REPORT_TAB}!A1:Q1"


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


# 'custom' tab — old↔new COMPARISON of the custom pages (press / locations /
# parties / cater / reserve / about), using the same engine as the report tab.
# Same columns as the report plus a leading "Page" column so each row shows
# which custom page it came from.
CUSTOM_HEADERS = [
    "Page",            # press / locations / parties / cater / reserve / about
    "Run timestamp",
    "Restaurant",
    "Old URL",
    "New URL",
    "Old site section name",
    "New site section name",
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
# Custom tab spans 18 columns → A:R
CUSTOM_RANGE = f"{CUSTOM_TAB}!A:R"
CUSTOM_HEADER_RANGE = f"{CUSTOM_TAB}!A1:R1"


# 'content' tab — typo / strange-content + review-rule checks on the NEW
# site's HOME-page sections and reviews (the validation that previously
# lived in the custom tab).
CONTENT_HEADERS = [
    "Restaurant name",
    "Run timestamp",
    "Source",          # "section" or "review"
    "Section / Reviewer",
    "Heading / Review text",
    "Status",          # OK / POTENTIAL ISSUE
    "Detail",
]
# Content tab spans 7 columns → A:G
CONTENT_RANGE = f"{CONTENT_TAB}!A:G"
CONTENT_HEADER_RANGE = f"{CONTENT_TAB}!A1:G1"


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
    """Ensure all result tabs have correct headers in row 1."""
    _ensure_headers(service, spreadsheet_id,
                    REPORT_HEADER_RANGE, f"{REPORT_TAB}!A1", REPORT_HEADERS)
    _ensure_headers(service, spreadsheet_id,
                    SECTIONS_HEADER_RANGE, f"{SECTIONS_TAB}!A1", SECTIONS_HEADERS)
    _ensure_headers(service, spreadsheet_id,
                    SEO_HEADER_RANGE, f"{SEO_TAB}!A1", SEO_HEADERS)
    _ensure_headers(service, spreadsheet_id,
                    CUSTOM_HEADER_RANGE, f"{CUSTOM_TAB}!A1", CUSTOM_HEADERS)
    _ensure_headers(service, spreadsheet_id,
                    CONTENT_HEADER_RANGE, f"{CONTENT_TAB}!A1", CONTENT_HEADERS)


def clear_data_rows(service, spreadsheet_id):
    """
    Wipe every row below the header (row 1) in each result tab BEFORE the
    run appends new data, so each run produces a clean snapshot rather than
    appending to old runs. The header row itself is left untouched.

    Uses values.clear on the tab name with no row 1 — Sheets interprets the
    tab-only reference as "all data in the tab" and clear leaves no choice
    of starting row, so we use an explicit "A2:" range per tab to skip row 1.
    """
    for tab in (REPORT_TAB, SECTIONS_TAB, SEO_TAB, CUSTOM_TAB, CONTENT_TAB):
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
            r.get("old_section_name", r.get("service", "")),
            r.get("new_section_name", r.get("service", "")),
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
    One row per <h1> for the seo tab. Each row pairs the i-th h1 on the old
    site with the i-th h1 on the new site (positional pairing in document
    order). When the two sides have different h1 counts, extra rows show
    "MISSING" on the absent side.

    Columns per row:
      Old H1 status      "text" / "empty" / "MISSING"
      Old H1 text        the h1's text content ("" when status != "text")
      Old H1 visibility  "visible" / "hidden" / ""  (empty when MISSING)
      New H1 status      same shape as Old H1 status
      New H1 text        same
      New H1 visibility  same

    When neither side has any h1, a single placeholder row is emitted so
    reviewers see that the restaurant ran but had no h1 on either side,
    rather than the row silently disappearing.
    """
    pairs = build_h1_pairs(old_data, new_data)

    # Edge case: no h1 anywhere on either side. Emit one explanatory row
    # so the seo tab still records that this restaurant was processed.
    if not pairs:
        return [[
            restaurant,
            timestamp,
            "MISSING", "", "",
            "MISSING", "", "",
        ]]

    out = []
    for p in pairs:
        out.append([
            restaurant,
            timestamp,
            p["old_h1_status"],
            p["old_h1_text"],
            p["old_h1_visibility"],
            p["new_h1_status"],
            p["new_h1_text"],
            p["new_h1_visibility"],
        ])
    return out


# ----- Append helpers ----------------------------------------------------

def build_custom_tab_rows(page, timestamp, restaurant, old_url, new_url, comparison_rows):
    """
    Rows for the 'custom' tab: an old↔new comparison of one custom page
    (press / locations / ...). Same layout as the report tab, prefixed with a
    Page column. comparison_rows comes from build_validated_rows().
    """
    out = []
    for r in comparison_rows:
        out.append([
            page,
            timestamp,
            restaurant,
            old_url,
            new_url,
            r.get("old_section_name", r.get("service", "")),
            r.get("new_section_name", r.get("service", "")),
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


def build_content_tab_rows(restaurant, timestamp, section_results, review_results):
    """
    Rows for the 'content' tab: typo/strange-content findings on the new site's
    home sections, plus review-rule findings. section_results and
    review_results are the lists returned by content_validator.
    """
    out = []
    for r in section_results or []:
        out.append([
            restaurant,
            timestamp,
            "section",
            r.get("service_type", ""),
            (r.get("heading", "") or "")[:300],
            r.get("status", ""),
            r.get("issue", ""),
        ])
    for r in review_results or []:
        out.append([
            restaurant,
            timestamp,
            "review",
            r.get("reviewer", ""),
            (r.get("text", "") or "")[:300],
            r.get("status", ""),
            r.get("issue", ""),
        ])
    return out


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


def append_to_custom(service, spreadsheet_id, rows):
    _append(service, spreadsheet_id, CUSTOM_RANGE, rows)


def append_to_content(service, spreadsheet_id, rows):
    _append(service, spreadsheet_id, CONTENT_RANGE, rows)


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
    print("Clearing previous results from all tabs...", flush=True)
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

            # ---- AI section classification (runs FIRST so its labels feed
            #      both the report tab's Service column and the sections tab's
            #      section-name columns). Returns {} if the API key is missing
            #      or anything goes wrong, in which case the regex classifier
            #      provides the labels and the AI columns just stay empty.
            ai_labels = classify_sections_pair(
                restaurant,
                old_data.get("sections", []),
                new_data.get("sections", []),
            )

            # Inject AI labels onto each section dict so classify_service()
            # in the comparator picks them up automatically — this routes
            # the AI's label through every downstream step (service grouping,
            # section pairing, section name resolution) consistently.
            for idx, sec in enumerate(old_data.get("sections", [])):
                lbl = ai_labels.get(f"old_{idx}")
                if lbl:
                    sec["ai_service"] = lbl
            for idx, sec in enumerate(new_data.get("sections", [])):
                lbl = ai_labels.get(f"new_{idx}")
                if lbl:
                    sec["ai_service"] = lbl

            # ---- main report tab ----
            comparison_rows = build_validated_rows(old_data, new_data)
            report_rows = build_report_rows(
                timestamp, restaurant, old_url, new_url, comparison_rows
            )
            append_to_report(sheets, spreadsheet_id, report_rows)

            # ---- sections tab ----
            section_pairs = build_section_pairs(old_data, new_data)
            sections_rows = build_sections_tab_rows(
                restaurant, timestamp, section_pairs, ai_labels=ai_labels
            )
            append_to_sections(sheets, spreadsheet_id, sections_rows)

            # ---- seo tab ----
            seo_rows = build_seo_tab_rows(
                restaurant, timestamp, old_data, new_data
            )
            append_to_seo(sheets, spreadsheet_id, seo_rows)

            # ---- custom tab: old↔new COMPARISON of the custom pages
            #      (press / locations / parties / cater / reserve / about).
            #      A page is compared only when it exists in BOTH the old and
            #      new navs. Each side's URL comes from that side's own nav.
            old_pages = find_relevant_page_urls(old_data)
            new_pages = find_relevant_page_urls(new_data)
            shared_kinds = [k for k in new_pages if k in old_pages]
            custom_total = 0
            for kind in shared_kinds:
                o_url = old_pages[kind]
                n_url = new_pages[kind]
                try:
                    o_page = scrape_page(o_url)
                    n_page = scrape_page(n_url)
                except Exception as pe:
                    print(f"    custom: failed to scrape {kind} "
                          f"({o_url} / {n_url}): {pe}", flush=True)
                    continue
                # Re-use the AI classifier so the custom-page comparison gets
                # the same ordinal section names as the report tab.
                page_ai = classify_sections_pair(
                    restaurant,
                    o_page.get("sections", []),
                    n_page.get("sections", []),
                )
                for idx, sec in enumerate(o_page.get("sections", [])):
                    lbl = page_ai.get(f"old_{idx}")
                    if lbl:
                        sec["ai_service"] = lbl
                for idx, sec in enumerate(n_page.get("sections", [])):
                    lbl = page_ai.get(f"new_{idx}")
                    if lbl:
                        sec["ai_service"] = lbl
                page_rows = build_validated_rows(o_page, n_page)
                custom_rows = build_custom_tab_rows(
                    kind, timestamp, restaurant, o_url, n_url, page_rows
                )
                append_to_custom(sheets, spreadsheet_id, custom_rows)
                custom_total += len(custom_rows)

            # ---- content tab (NEW site only): typo / strange-content checks on
            #      the HOME-page sections + review-rule checks. Skipped silently
            #      if the OpenAI key/SDK is unavailable (returns []).
            content_section_results = validate_sections(
                restaurant, new_data.get("sections", [])
            )
            content_review_results = validate_reviews(
                restaurant, new_data.get("reviews", [])
            )
            content_rows = build_content_tab_rows(
                restaurant, timestamp, content_section_results, content_review_results
            )
            append_to_content(sheets, spreadsheet_id, content_rows)

            ok = sum(1 for r in comparison_rows if r.get("match") == "OK")
            issues = len(comparison_rows) - ok
            ai_mark = "✓" if ai_labels else "✗"
            print(f"  ✓ report: {len(report_rows)} rows (OK: {ok}, issues: {issues})  "
                  f"sections: {len(sections_rows)} (ai {ai_mark})  seo: {len(seo_rows)}  "
                  f"custom: {custom_total} ({len(shared_kinds)} pages)  "
                  f"content: {len(content_rows)}",
                  flush=True)
        except Exception as e:
            failures += 1
            print(f"  ✗ FAILED: {e}", flush=True)
            traceback.print_exc()
            append_to_report(sheets, spreadsheet_id, [[
                timestamp, "(error)", old_url, new_url,
                "error", "error", "", str(e)[:500], "", "", "", "", "", "", "", "", "ERROR",
            ]])

    print(f"\nDone. {len(pairs) - failures} succeeded, {failures} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()