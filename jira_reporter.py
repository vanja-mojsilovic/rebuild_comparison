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
import re
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

# Phrases that can precede the NEW (rebuilt) site URL in a publish comment.
# A comment qualifies if it contains ANY of these; the new URL is the first
# URL appearing after whichever phrase matches earliest.
_PUBLISH_PHRASES = (
    "Changes published to",
    "New test location:",
    "Test location:",
)

# JQL selecting the rebuild issues in QA not yet picked up by the QA team.
_REBUILD_JQL = (
    'project = web AND summary ~ "rebuild" AND NOT summary ~ "ADA" '
    'AND NOT summary ~ "redesign" AND status = QA '
    'AND assignee NOT IN membersOf("QA") ORDER BY key'
)

_URL_RE = re.compile(r'https?://[^\s<>"\)\]]+')


def _adf_collect_text_and_links(node, out_text, out_links):
    """
    Recursively walk an ADF node, accumulating plain text into out_text and
    link-mark hrefs into out_links. Each link is recorded as (char_offset,
    href) where char_offset is the length of text collected so far — i.e. the
    link's position in the flattened text. This lets callers tell whether a
    link sits before or after a marker phrase even when the URL only exists as
    a link mark and not as visible text.
    """
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type == "text":
            offset = sum(len(t) for t in out_text)
            for mark in node.get("marks", []) or []:
                if mark.get("type") == "link":
                    href = (mark.get("attrs") or {}).get("href")
                    if href:
                        out_links.append((offset, href))
            out_text.append(node.get("text", ""))
        elif node_type == "hardBreak":
            # Explicit line break inside a paragraph.
            out_text.append("\n")
        for child in node.get("content", []) or []:
            _adf_collect_text_and_links(child, out_text, out_links)
        # Block-level containers (paragraphs, headings, list items, etc.) have
        # no text node between them in the flattened output, which would glue
        # the end of one block to the start of the next — e.g. a URL ending a
        # paragraph running into the first word of the next ("...com/New").
        # Append a newline after each block so those boundaries survive.
        if node_type in ("paragraph", "heading", "listItem", "blockquote",
                          "tableCell", "tableHeader", "codeBlock"):
            out_text.append("\n")
    elif isinstance(node, list):
        for child in node:
            _adf_collect_text_and_links(child, out_text, out_links)


def _extract_url_pair_from_comment(adf_body) -> tuple:
    """
    From one ADF comment body, return (old_url, new_url) when it contains the
    publish phrase, else (None, None).

    NEW = the first URL appearing AFTER the publish phrase ("Changes
          published to" or "New test location:"), whether the URL is visible
          text or a link-mark href.
    OLD = the first OTHER http(s) URL anywhere in the same comment.
    """
    text_parts, links = [], []  # links: [(offset, href), ...]
    _adf_collect_text_and_links(adf_body, text_parts, links)
    full_text = "".join(text_parts)
    lower = full_text.lower()

    # Find whichever publish phrase appears earliest in the comment.
    phrase_hit = None  # (index, phrase)
    for ph in _PUBLISH_PHRASES:
        i = lower.find(ph.lower())
        if i != -1 and (phrase_hit is None or i < phrase_hit[0]):
            phrase_hit = (i, ph)
    if phrase_hit is None:
        return None, None

    phrase_idx, phrase = phrase_hit
    after_start = phrase_idx + len(phrase)

    def _clean(u):
        return u.rstrip('.,);]')

    # Candidate NEW urls after the phrase: from visible text and from link
    # marks positioned after the phrase. Pick the earliest.
    new_candidates = []  # (position, url)
    for m in _URL_RE.finditer(full_text):
        if m.start() >= after_start:
            new_candidates.append((m.start(), _clean(m.group(0))))
    for offset, href in links:
        if offset >= after_start:
            new_candidates.append((offset, _clean(href)))
    if not new_candidates:
        return None, None
    new_candidates.sort()
    new_url = new_candidates[0][1]

    # All urls in the comment (text + links), in order, deduped.
    all_urls = []
    for m in _URL_RE.finditer(full_text):
        u = _clean(m.group(0))
        if u not in all_urls:
            all_urls.append(u)
    for _off, href in links:
        u = _clean(href)
        if u not in all_urls:
            all_urls.append(u)

    # OLD = first OTHER url.
    old_url = next((u for u in all_urls if u != new_url), None)
    if not old_url:
        return None, None
    return old_url, new_url


def fetch_rebuild_url_pairs() -> list:
    """
    Run the rebuild-QA JQL, then for each issue scan its comments for the
    "Changes published to <new-url>" publish comment and extract (old, new)
    URLs. Returns a list of (old_url, new_url, issue_key) tuples — the same
    shape read_url_pairs produces — for issues with a usable comment. Issues
    without one are skipped silently (logged to stderr).

    Best-effort: missing config or errors log and return [].
    """
    base, email, token = _jira_config()
    if not (base and email and token):
        print("[jira] config not set; cannot fetch rebuild issues.", file=sys.stderr)
        return []

    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # 1) JQL → issue keys. Jira removed the old /search endpoint (HTTP 410),
    #    so use /search/jql with nextPageToken pagination.
    keys = []
    next_token = None
    search_url = f"{base}/rest/api/3/search/jql"
    print(f"[jira] rebuild JQL: {_REBUILD_JQL}", flush=True)
    for _ in range(20):
        body = {"jql": _REBUILD_JQL, "fields": ["key"], "maxResults": 100}
        if next_token:
            body["nextPageToken"] = next_token
        req = urllib.request.Request(search_url, data=json.dumps(body).encode("utf-8"),
                                     method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            print(f"[jira] rebuild JQL HTTP {e.code}: {detail}", file=sys.stderr)
            return []
        except Exception as e:
            print(f"[jira] rebuild JQL search failed: {e}", file=sys.stderr)
            return []
        issues = payload.get("issues", [])
        page_keys = [issue["key"] for issue in issues if issue.get("key")]
        keys.extend(page_keys)
        print(f"[jira]   JQL page: {len(page_keys)} key(s) "
              f"(isLast={payload.get('isLast')})", flush=True)
        next_token = payload.get("nextPageToken")
        if not next_token or payload.get("isLast"):
            break

    print(f"[jira] JQL matched {len(keys)} issue(s): {', '.join(keys) if keys else '(none)'}",
          flush=True)

    # 2) Per issue, fetch comments and extract the publish URL pair.
    pairs = []
    for key in keys:
        c_url = f"{base}/rest/api/3/issue/{key}/comment"
        req = urllib.request.Request(c_url, method="GET")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                cpayload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"[jira] failed to read comments for {key}: {e}", file=sys.stderr)
            continue
        comments = cpayload.get("comments", []) or []
        found = None
        for c in reversed(comments):  # newest-first
            old_url, new_url = _extract_url_pair_from_comment(c.get("body"))
            if old_url and new_url:
                found = (old_url, new_url, key)
                break
        if found:
            print(f"[jira]   {key}: OLD={found[0]}  NEW={found[1]}", flush=True)
            pairs.append(found)
        else:
            print(f"[jira]   {key}: {len(comments)} comment(s), no usable publish "
                  f"comment (no 'Changes published to' / 'New test location:'); skipped.",
                  flush=True)
    print(f"[jira] extracted {len(pairs)} URL pair(s) from comments.", flush=True)
    return pairs


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


def fetch_issue_summary_assignee(issue_key: str) -> tuple:
    """
    Fetch (summary, assignee) for a single Jira issue.

    - summary: the issue's summary text (or "").
    - assignee: the assignee's display name, falling back to their email if
      there's no display name, or "Unassigned" when the issue has no assignee.

    Best-effort: missing config or any error returns ("", "") so logging never
    aborts the run.
    """
    if not issue_key:
        return "", ""
    base, email, token = _jira_config()
    if not (base and email and token):
        return "", ""

    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    url = f"{base}/rest/api/3/issue/{issue_key}?fields=summary,assignee"
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[jira] failed to fetch summary/assignee for {issue_key}: {e}",
              file=sys.stderr)
        return "", ""

    fields = payload.get("fields", {}) or {}
    summary = (fields.get("summary") or "").strip()

    assignee_obj = fields.get("assignee")
    if not assignee_obj:
        assignee = "Unassigned"
    else:
        assignee = (assignee_obj.get("displayName")
                    or assignee_obj.get("emailAddress")
                    or "Unassigned").strip()

    return summary, assignee


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