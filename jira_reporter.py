"""
Build and post a Jira comment summarizing one URL pair's rebuild validation.

Five summary lines, rendered as a fixed-width aligned plain-text table inside
a Jira `{code}` block (so the columns line up in the comment):

    Check               | Status
    SEO validation       | OK
    Sections order       | FULLY MATCH
    Elements in sections | OK
    Content validation   | OK
    Custom pages         | NONE

Each status expands to detail lines when there's something to report.

Auth: Jira Cloud REST API, Basic auth with email + API token. Reads:
    JIRA_BASE_URL   e.g. https://yourcompany.atlassian.net
    JIRA_EMAIL      the account email
    JIRA_API_TOKEN  an API token from id.atlassian.com
Posting is best-effort: missing config or a failed request logs a warning and
returns False, never aborting the run.
"""

import base64
import json
import os
import sys
import urllib.request
import urllib.error


# ──────────────────────────────────────────────────────────────────────────
# Summary building
# ──────────────────────────────────────────────────────────────────────────

# Statuses the comparator emits that are acceptable (no action needed).
_OK_STATUSES = ("OK", "EXPECTED")

import re as _re_jr

# Trailing " N" ordinal on a section name ("Reviews 1", "Hero 2").
_ORDINAL_TAIL_RE = _re_jr.compile(r"\s+\d+\s*$")


def _section_display_name(name: str) -> str:
    """
    Turn an ordinal section name into a human label for the Jira summary:
    "Reviews 1" -> "Reviews section", "Hero 2" -> "Hero section". Names
    without a trailing number get " section" appended too ("Footer" ->
    "Footer section"). "MISSING"/"?" pass through unchanged.
    """
    n = (name or "").strip()
    if not n or n in ("MISSING", "?"):
        return n or "?"
    base = _ORDINAL_TAIL_RE.sub("", n).strip()
    if not base:
        base = n
    return f"{base} section"


def _snippet(text: str, limit: int = 50) -> str:
    """First `limit` characters of `text`, with an ellipsis when truncated."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit].rstrip() + "..."


def _seo_line(new_data: dict) -> tuple:
    """
    SEO: OK when the NEW site has exactly one H1 with text content.

    page_h1s is a list of {"text", "visible", "empty"} dicts (from the scraper),
    so we count the entries that actually carry text. Returns (status, details).
    """
    h1s = new_data.get("page_h1s") or []
    with_text = [h for h in h1s
                 if isinstance(h, dict) and not h.get("empty")
                 and (h.get("text") or "").strip()]
    n = len(with_text)
    if n == 1:
        return "OK", []
    if n == 0:
        return "ISSUE", ["no H1 element with text on the new site"]
    return "ISSUE", [f"{n} H1 elements on the new site (expected exactly 1)"]


def _sections_order_line(section_pairs: list) -> tuple:
    """
    Sections order: FULLY MATCH when every pair has both sides present and in
    the same order. Otherwise note extras (new-only) and missing (old-only).
    section_pairs: list of dicts with old_section_name / new_section_name,
    where "MISSING" marks an absent side.
    """
    extra_on_new = []   # present new, absent old
    missing_on_new = []  # present old, absent new
    for p in section_pairs or []:
        old_name = (p.get("old_section_name") or "").strip()
        new_name = (p.get("new_section_name") or "").strip()
        if old_name == "MISSING" and new_name and new_name != "MISSING":
            extra_on_new.append(new_name)
        elif new_name == "MISSING" and old_name and old_name != "MISSING":
            missing_on_new.append(old_name)

    if not extra_on_new and not missing_on_new:
        return "FULLY MATCH", []

    details = []
    if missing_on_new:
        details.append("missing on new: " + ", ".join(missing_on_new))
    if extra_on_new:
        details.append("extra on new: " + ", ".join(extra_on_new))
    return "MISMATCH", details


def _elements_line(comparison_rows: list) -> tuple:
    """
    Elements in the sections: OK when every element row is OK/EXPECTED.
    Otherwise enumerate each issue as 'Section: element type — what changed'.
    """
    issues = []
    for r in comparison_rows or []:
        match = (r.get("match") or "").strip()
        if match in _OK_STATUSES:
            continue
        raw_section = (r.get("old_section_name") or r.get("new_section_name")
                       or r.get("service") or "?").strip()
        section = _section_display_name(raw_section)
        oe = (r.get("old_element") or "").strip()
        ne = (r.get("new_element") or "").strip()
        elem = oe or ne or "element"
        o_text = _snippet(r.get("old_text"))
        n_text = _snippet(r.get("new_text"))
        # Phrase by match kind, including the relevant text snippet.
        if match.startswith("MISSING"):
            issues.append(f"{section}: {oe or elem} missing on new — \"{o_text}\"")
        elif match.startswith("EXTRA"):
            issues.append(f"{section}: {ne or elem} extra on new — \"{n_text}\"")
        elif match == "DIFFERS":
            label = f"{oe}->{ne}" if oe and ne and oe != ne else elem
            issues.append(f"{section}: {label} text differs — \"{o_text}\" -> \"{n_text}\"")
        else:
            issues.append(f"{section}: {elem} {match} — \"{o_text or n_text}\"")
    if not issues:
        return "OK", []
    return "ISSUES", issues


def _content_line(content_section_results: list, content_review_results: list) -> tuple:
    """
    Content validation: OK when no POTENTIAL ISSUE in section or review checks.
    Otherwise list each issue.
    """
    issues = []
    for r in content_section_results or []:
        if r.get("status") == "POTENTIAL ISSUE":
            head = (r.get("heading") or r.get("service_type") or "section").strip()
            issues.append(f"{head}: {r.get('issue', '')}")
    for r in content_review_results or []:
        if r.get("status") == "POTENTIAL ISSUE":
            who = (r.get("reviewer") or "review").strip()
            issues.append(f"{who}: {r.get('issue', '')}")
    if not issues:
        return "OK", []
    return "ISSUES", issues


def _custom_pages_line(custom_results: list) -> tuple:
    """
    Custom pages: NONE when no shared custom pages were compared. Otherwise one
    entry per page: 'slug: OK' or 'slug: N issues'.
    custom_results: list of {"kind": slug, "rows": [comparison_row, ...]}.
    """
    if not custom_results:
        return "NONE", []
    parts = []
    for page in custom_results:
        kind = page.get("kind", "?")
        bad = sum(1 for r in page.get("rows", [])
                  if (r.get("match") or "") not in _OK_STATUSES)
        parts.append(f"{kind}: OK" if bad == 0 else f"{kind}: {bad} issues")
    return ("OK" if all(p.endswith(": OK") for p in parts) else "ISSUES"), parts


def build_comment(restaurant: str,
                  new_data: dict,
                  section_pairs: list,
                  comparison_rows: list,
                  content_section_results: list,
                  content_review_results: list,
                  custom_results: list) -> str:
    """
    Assemble the plain-text aligned summary table (wrapped in a Jira {code}
    block so the alignment survives). Detail lines are appended under the
    table for any check that isn't fully clean.
    """
    seo = _seo_line(new_data)
    order = _sections_order_line(section_pairs)
    elements = _elements_line(comparison_rows)
    content = _content_line(content_section_results, content_review_results)
    custom = _custom_pages_line(custom_results)

    table = [
        ("SEO validation",        seo[0]),
        ("Sections order",        order[0]),
        ("Elements in sections",  elements[0]),
        ("Content validation",    content[0]),
        ("Custom pages",          custom[0]),
    ]
    label_w = max(len(label) for label, _ in table)

    lines = [f"{VALIDATION_MARKER}: {restaurant}", ""]
    lines.append(f"{'Check'.ljust(label_w)} | Status")
    lines.append(f"{'-' * label_w}-+-{'-' * 12}")
    for label, status in table:
        lines.append(f"{label.ljust(label_w)} | {status}")

    # Detail sections (only when non-empty), as indented plain text so the
    # whole thing renders cleanly inside one monospace code block.
    def _detail_block(title, detail_list):
        if not detail_list:
            return
        lines.append("")
        lines.append(f"{title}:")
        for d in detail_list:
            lines.append(f"  - {d}")

    _detail_block("Sections order", order[1])
    _detail_block("Element issues", elements[1])
    _detail_block("Content issues", content[1])
    _detail_block("Custom pages", custom[1] if custom[0] != "NONE" else [])
    _detail_block("SEO", seo[1])

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Posting
# ──────────────────────────────────────────────────────────────────────────

def _jira_config() -> tuple:
    base = (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL") or ""
    token = os.environ.get("JIRA_API_TOKEN") or ""
    return base, email, token


# The marker phrase every automation comment starts with. Used both as the
# comment heading and as the JQL needle for finding issues already commented.
VALIDATION_MARKER = "Rebuild automation validation"


def fetch_commented_issue_keys() -> set:
    """
    Query Jira for every issue that already has a comment containing the
    validation marker phrase, across ALL projects, and return the set of
    issue keys. Used to skip re-posting on issues already validated.

    Best-effort: missing config or any error logs a warning and returns an
    empty set (so nothing is suppressed and the run proceeds normally).
    Paginates through results in case there are many.
    """
    base, email, token = _jira_config()
    if not (base and email and token):
        print("[jira] config not set; cannot fetch commented issues — "
              "no suppression this run.", file=sys.stderr)
        return set()

    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    jql = f'comment ~ "{VALIDATION_MARKER}" ORDER BY key'
    url = f"{base}/rest/api/3/search/jql"
    keys = set()
    next_token = None

    for _ in range(20):  # safety cap on pagination
        body = {"jql": jql, "fields": ["key"], "maxResults": 100}
        if next_token:
            body["nextPageToken"] = next_token
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Basic {auth}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            print(f"[jira] JQL search HTTP {e.code}: {detail}", file=sys.stderr)
            return keys
        except Exception as e:
            print(f"[jira] JQL search failed: {e}", file=sys.stderr)
            return keys

        for issue in payload.get("issues", []):
            k = issue.get("key")
            if k:
                keys.add(k)

        next_token = payload.get("nextPageToken")
        if not next_token or payload.get("isLast"):
            break

    return keys


def post_comment(issue_key: str, body: str) -> bool:
    """
    Post `body` as a comment on the Jira issue `issue_key`. Returns True on
    success. Best-effort: missing config or any error logs and returns False
    without raising, so the pipeline run is never aborted by Jira problems.
    """
    if not issue_key:
        return False
    base, email, token = _jira_config()
    if not (base and email and token):
        print("[jira] JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN not set; "
              "skipping Jira comment.", file=sys.stderr)
        return False

    url = f"{base}/rest/api/3/issue/{issue_key}/comment"
    # Jira Cloud v3 expects an Atlassian Document Format (ADF) body. Send the
    # whole summary as a single code-block-free text node split on newlines;
    # ADF paragraphs separate naturally by hard breaks.
    adf = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "codeBlock",
                "content": [{"type": "text", "text": body}],
            }],
        }
    }
    data = json.dumps(adf).encode("utf-8")
    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status in (200, 201):
                return True
            print(f"[jira] unexpected status {resp.status} for {issue_key}",
                  file=sys.stderr)
            return False
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print(f"[jira] HTTP {e.code} posting to {issue_key}: {detail}",
              file=sys.stderr)
        return False
    except Exception as e:
        print(f"[jira] failed to post to {issue_key}: {e}", file=sys.stderr)
        return False