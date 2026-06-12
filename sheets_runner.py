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
from content_validator import validate_sections, validate_reviews, analyze_all
from jira_reporter import (
    build_comment as build_jira_comment,
    post_comment as post_jira_comment,
    fetch_commented_issue_keys,
    fetch_rebuild_url_pairs,
)


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
SUPPRESSION_TAB = "suppression"


# ----- Headers for each tab ----------------------------------------------

REPORT_HEADERS = [
    "Run timestamp",
    "Restaurant",
    "Old URL",
    "Old site section name",
    "Old element",
    "Old text",
    "Old href",
    "Old hidden",
    "Old HTML type",
    "New URL",
    "New site section name",
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


# 'suppression' tab — snapshot, refreshed each run, of the Jira issues that
# ALREADY have a "Rebuild automation validation" comment (found via JQL). When
# a URL pair's issue key is in this list, the run skips posting another comment
# (it still writes all the sheet tabs). Columns: issue key + run timestamp.
SUPPRESSION_HEADERS = [
    "Issue key",
    "Date stamp",
]
# Suppression tab spans 2 columns → A:B
SUPPRESSION_RANGE = f"{SUPPRESSION_TAB}!A:B"
SUPPRESSION_HEADER_RANGE = f"{SUPPRESSION_TAB}!A1:B1"


# 'archive' tab — a PERMANENT, append-only log (never cleared) of every Jira
# comment this script successfully posts: one row per post, issue key + the
# run timestamp. Distinct from 'suppression', which is a per-run snapshot.
ARCHIVE_TAB = "archive"
ARCHIVE_HEADERS = [
    "Issue key",
    "Time stamp",
]
ARCHIVE_RANGE = f"{ARCHIVE_TAB}!A:B"
ARCHIVE_HEADER_RANGE = f"{ARCHIVE_TAB}!A1:B1"


def get_sheets_service():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_JSON env var is not set", file=sys.stderr)
        sys.exit(1)
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def write_url_pairs(service, spreadsheet_id, pairs):
    """
    Overwrite the urls tab with the given (old, new, issue_key) pairs: clear
    all rows below the header, then write the pairs starting at A2. Used when
    populating the tab from Jira before a run.
    """
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"{URLS_TAB}!A2:Z",
    ).execute()
    if not pairs:
        return
    rows = [[old, new, key] for (old, new, key) in pairs]
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{URLS_TAB}!A2",
        valueInputOption="RAW",
        body={"values": rows},
    ).execute()


def read_url_pairs(service, spreadsheet_id):
    """
    Read the urls tab. Columns: A = old URL, B = new URL, C = Jira issue key
    (optional). Returns a list of (old_url, new_url, issue_key) tuples;
    issue_key is "" when column C is empty.
    """
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{URLS_TAB}!A:C",
    ).execute()
    rows = result.get("values", [])
    if not rows:
        return []

    pairs = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        old, new = (row[0] or "").strip(), (row[1] or "").strip()
        issue_key = (row[2].strip() if len(row) > 2 and row[2] else "")
        if old and new and old.startswith("http") and new.startswith("http"):
            pairs.append((old, new, issue_key))
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
    _ensure_headers(service, spreadsheet_id,
                    SUPPRESSION_HEADER_RANGE, f"{SUPPRESSION_TAB}!A1", SUPPRESSION_HEADERS)
    _ensure_headers(service, spreadsheet_id,
                    ARCHIVE_HEADER_RANGE, f"{ARCHIVE_TAB}!A1", ARCHIVE_HEADERS)


def clear_data_rows(service, spreadsheet_id):
    """
    Wipe every row below the header (row 1) in each result tab BEFORE the
    run appends new data, so each run produces a clean snapshot rather than
    appending to old runs. The header row itself is left untouched.

    Uses values.clear on the tab name with no row 1 — Sheets interprets the
    tab-only reference as "all data in the tab" and clear leaves no choice
    of starting row, so we use an explicit "A2:" range per tab to skip row 1.
    """
    for tab in (REPORT_TAB, SECTIONS_TAB, SEO_TAB, CUSTOM_TAB, CONTENT_TAB, SUPPRESSION_TAB):
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
            r.get("old_section_name", r.get("service", "")),
            r.get("old_element", ""),
            r.get("old_text", ""),
            r.get("old_href", ""),
            r.get("old_hidden", ""),
            r.get("old_html_type", ""),
            new_url,
            r.get("new_section_name", r.get("service", "")),
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


def append_to_suppression(service, spreadsheet_id, rows):
    _append(service, spreadsheet_id, SUPPRESSION_RANGE, rows)


def append_to_archive(service, spreadsheet_id, rows):
    _append(service, spreadsheet_id, ARCHIVE_RANGE, rows)


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

    # When SKIP_JIRA is set (to 1/true/yes), the run writes all sheet tabs but
    # does NOT post any Jira comments — used by the sheet-only workflow.
    _skip_jira = os.environ.get("SKIP_JIRA", "").strip().lower() in ("1", "true", "yes")
    if _skip_jira:
        print("SKIP_JIRA set — Jira comments will not be posted.", flush=True)

    # When FORCE_JIRA_COMMENT is set, the suppression check is bypassed: a
    # comment is posted for every issue key even if the issue already has a
    # validation comment. Used by the manual-URL workflow when you explicitly
    # want to re-comment regardless of history.
    _force_comment = os.environ.get("FORCE_JIRA_COMMENT", "").strip().lower() in ("1", "true", "yes")
    if _force_comment:
        print("FORCE_JIRA_COMMENT set — will comment even if one already exists "
              "(suppression bypassed).", flush=True)

    sheets = get_sheets_service()

    # When URLS_FROM_JIRA is set, populate the urls tab from Jira first: run
    # the rebuild-QA JQL, extract each issue's published old/new URLs from its
    # "Changes published to" comment, and overwrite the urls tab with them.
    # Otherwise the urls tab is used as-is (manually entered).
    _urls_from_jira = os.environ.get("URLS_FROM_JIRA", "").strip().lower() in ("1", "true", "yes")
    if _urls_from_jira:
        print("URLS_FROM_JIRA set — fetching URL pairs from Jira...", flush=True)
        jira_pairs = fetch_rebuild_url_pairs()
        print(f"Jira: found {len(jira_pairs)} rebuild issue(s) with a publish "
              f"comment.", flush=True)
        write_url_pairs(sheets, spreadsheet_id, jira_pairs)
    else:
        print("URLS_FROM_JIRA not set — using urls tab as entered "
              "(no Jira fetch).", flush=True)

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

    # Up front, ask Jira which issues ALREADY have a validation comment (one
    # JQL query, all projects). These keys are skipped for comment posting and
    # written to the suppression tab as this run's snapshot. Skipped entirely
    # when SKIP_JIRA is set (no Jira at all) or FORCE_JIRA_COMMENT is set
    # (comment regardless of history → no suppression).
    already_commented = set()
    if not _skip_jira and not _force_comment:
        already_commented = fetch_commented_issue_keys()
        print(f"Jira: {len(already_commented)} issue(s) already have a "
              f"validation comment (will be skipped).", flush=True)
        if already_commented:
            append_to_suppression(
                sheets, spreadsheet_id,
                [[k, timestamp] for k in sorted(already_commented)],
            )

    for i, (old_url, new_url, issue_key) in enumerate(pairs, start=1):
        print(f"\n[{i}/{len(pairs)}] {old_url}  →  {new_url}", flush=True)
        try:
            old_data = scrape_page(old_url)
            new_data = scrape_page(new_url)
            restaurant = (
                new_data.get("restaurant_name")
                or old_data.get("restaurant_name")
                or "(unknown)"
            )

            # ---- Scrape the custom pages up front (no AI yet) so ALL AI work
            #      for this URL pair can go in a SINGLE OpenAI request.
            old_pages = find_relevant_page_urls(old_data)
            new_pages = find_relevant_page_urls(new_data)
            shared_kinds = [k for k in new_pages if k in old_pages]

            custom_scraped = []  # {kind, o_url, n_url, o_page, n_page}
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
                custom_scraped.append({
                    "kind": kind, "o_url": o_url, "n_url": n_url,
                    "o_page": o_page, "n_page": n_page,
                })

            # ---- ONE AI call: home classification + home section/review
            #      validation + every custom page's classification, all in a
            #      single request. Returns safe empty defaults on any failure.
            ai = analyze_all(
                restaurant,
                old_data.get("sections", []),
                new_data.get("sections", []),
                new_data.get("reviews", []),
                [{"kind": c["kind"],
                  "old_sections": c["o_page"].get("sections", []),
                  "new_sections": c["n_page"].get("sections", [])}
                 for c in custom_scraped],
            )

            ai_labels = ai["home_labels"]

            # Inject home AI labels onto each section dict so classify_service()
            # in the comparator picks them up (routes the label through service
            # grouping, section pairing, and section-name resolution).
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

            # ---- custom tab: old↔new COMPARISON of the custom pages, using the
            #      per-page labels the single AI call already produced.
            custom_total = 0
            custom_results = []  # for the Jira summary: [{kind, rows}]
            custom_labels = ai["custom_labels"]
            for c in custom_scraped:
                kind = c["kind"]
                o_page, n_page = c["o_page"], c["n_page"]
                page_ai = custom_labels.get(kind, {})
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
                    kind, timestamp, restaurant, c["o_url"], c["n_url"], page_rows
                )
                append_to_custom(sheets, spreadsheet_id, custom_rows)
                custom_total += len(custom_rows)
                custom_results.append({"kind": kind, "rows": page_rows})

            # ---- content tab (NEW site only): home-page typo / review issues
            #      from the same single AI call.
            content_section_results = ai["home_section_issues"]
            content_review_results = ai["home_review_issues"]
            content_rows = build_content_tab_rows(
                restaurant, timestamp, content_section_results, content_review_results
            )
            append_to_content(sheets, spreadsheet_id, content_rows)

            # ---- Jira comment: one per URL pair when column C has an issue
            #      key, UNLESS the run opted out via SKIP_JIRA or the issue
            #      ALREADY has a validation comment (suppression).
            if issue_key and _skip_jira:
                print(f"  jira: {issue_key} skipped (SKIP_JIRA set)", flush=True)
            elif issue_key and issue_key in already_commented:
                print(f"  jira: {issue_key} skipped (already has a "
                      f"validation comment)", flush=True)
            elif issue_key:
                comment = build_jira_comment(
                    restaurant,
                    new_data,
                    section_pairs,
                    comparison_rows,
                    content_section_results,
                    content_review_results,
                    custom_results,
                )
                posted = post_jira_comment(issue_key, comment)
                print(f"  jira: {issue_key} {'posted' if posted else 'skipped/failed'}",
                      flush=True)
                # Log every SUCCESSFUL post to the permanent (append-only)
                # archive tab: issue key + this run's timestamp.
                if posted:
                    append_to_archive(sheets, spreadsheet_id,
                                      [[issue_key, timestamp]])

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
                timestamp,            # Run timestamp
                "(error)",            # Restaurant
                old_url,              # Old URL
                "error",              # Old site section name
                "",                   # Old element
                str(e)[:500],         # Old text  (the error message)
                "", "", "",           # Old href / hidden / HTML type
                new_url,              # New URL
                "error",              # New site section name
                "", "", "", "", "",   # New element / text / href / hidden / HTML type
                "ERROR",              # Match
            ]])

    print(f"\nDone. {len(pairs) - failures} succeeded, {failures} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()