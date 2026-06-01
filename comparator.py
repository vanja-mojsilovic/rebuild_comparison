"""
Smart comparison of Old vs New restaurant websites.

Validation logic for the rebuild use case:
  - Old's H1 should match New's H2 (heading shift)
  - Old's H2 should match New's H3
  - Old's paragraphs match New's paragraphs
  - Buttons match on visible text AND href
  - Multiple sections of the same service are paired by content similarity,
    not by document order

Each emitted row also carries the HTML element type for both sides
(slideshow / carousel / cover_video / text+image) so the report can
show what kind of block each section is rendered as.

Returns flat row data shaped for writing into the Google Sheet.
"""

import re
from typing import Optional


SERVICE_RULES = [
    ("food menu",    lambda s: bool(re.match(r"^(our menu|menu)$", s, re.I)) or
                               bool(re.search(r"see menu|full menu", s, re.I))),
    ("drink menu",   lambda s: bool(re.search(r"drinks", s, re.I))),
    ("online order", lambda s: bool(re.search(r"\border\b|pick up", s, re.I))),
    # "parties" must come BEFORE "events" so a section labelled "Private Events"
    # / "Private Dining" doesn't collapse into the generic events bucket.
    # SpotHopper templates often rebrand a parties block as "Private Events"
    # or "Private Dining" while still using the /parties URL slug. Treat any
    # of those phrasings as parties.
    ("parties",      lambda s: bool(re.search(
                        r"parties|\bparty\b|private\s*event|private\s*dining|"
                        r"private\s*room|book\s+your\s+event",
                        s, re.I))),
    ("events",       lambda s: bool(re.search(r"events", s, re.I))),
    ("specials",     lambda s: bool(re.search(r"specials", s, re.I))),
    ("catering",     lambda s: bool(re.search(r"catering|cater", s, re.I))),
    ("reservations", lambda s: bool(re.search(r"reserve|reservations|book a table", s, re.I))),
    ("jobs",         lambda s: bool(re.search(r"jobs|for a job", s, re.I))),
    ("about us",     lambda s: bool(re.search(r"about us|our story", s, re.I))),
    ("locations",    lambda s: bool(re.search(r"locations?|visit us|find us|our location", s, re.I))),
    ("gift cards",   lambda s: bool(re.search(r"e?\s*-?\s*gift\s*card|gift\s*certificate", s, re.I))),
    ("reviews",      lambda s: bool(re.search(r"^reviews?$|customer reviews|what our|testimonials?", s, re.I))),
    ("gallery",      lambda s: bool(re.search(r"gallery|photo gallery|photos", s, re.I))),
    ("newsletter",   lambda s: bool(re.search(r"newsletter|sign up|subscribe", s, re.I))),
    ("contact",      lambda s: bool(re.search(r"contact us|contact info|get in touch|hours", s, re.I))),
    ("carousel",     lambda s: bool(re.search(r"^carousel$", s, re.I))),
]


# Element types compared, with the heading shift baked in:
# Each tuple is (old_field, new_field, old_label, new_label).
# old_label / new_label are written to the report's Old element / New element
# columns respectively.
#
# Headings are handled SEPARATELY (see _compare_headings) because a rebuild
# may or may not shift heading levels (H1->H2->H3). Rather than assume a fixed
# shift, we pool all headings on each side and match by text at ANY level —
# so "HOMESTYLE CATERING" as an old H2 matches the same text whether it's an
# H2 or H3 on the new side. This avoids false EXTRA/MISSING/DIFFERS rows when
# the levels happen to line up (e.g. identical old and new URLs).
#
# Only the non-heading element types are driven by this table now.
COMPARISON_FIELDS = [
    ("paragraphs", "paragraphs", "P", "P"),
    ("list_items", "list_items", "LI", "LI"),
]

# Heading levels pooled together for the level-agnostic comparison.
HEADING_FIELDS = ("h1", "h2", "h3")


# Default when a section dict pre-dates the html_element_type field
DEFAULT_HTML_TYPE = "text+image"


# ------------------------------------------------------------
# Classification
# ------------------------------------------------------------
def classify_service(section: dict) -> str:
    """
    Classify a section using, in priority order:
      0. an AI-supplied label written onto the section as section["ai_service"]
         (this overrides everything else because the AI sees headings + buttons
         + paragraphs + hrefs together in context, and is by definition the
         single source of truth across the report and sections tabs)
      1. button visible text + the section's unified headings (h1+h2+h3)
      2. button hrefs (URL slugs like /parties, /catering, /private-events
         catch rebranded sections where the heading text doesn't contain
         the keyword anymore)
      3. paragraph text as a last resort (a "party" mention in copy)

    Returns the first matching service name, or "other" if nothing matches.
    The AI label is normalized to underscore-free lowercase so it lines up
    with the regex-based label set (e.g. "Cover Video" -> "cover video").
    """
    # 0. AI label takes precedence when present
    ai = (section.get("ai_service") or "").strip().lower()
    if ai:
        return ai

    button_texts = [b["text"] for b in section.get("buttons", []) if b.get("text")]
    headings = section.get("headings", [])
    # Primary signals: things the visitor actually sees as labels
    for source in button_texts + headings:
        for name, test in SERVICE_RULES:
            if test(source):
                return name

    # Secondary signal: button hrefs. URL slugs survive rebrands
    # (e.g. "Private Events" heading but /parties href).
    button_hrefs = [b.get("href", "") for b in section.get("buttons", []) if b.get("href")]
    for href in button_hrefs:
        # Pull the last non-empty path segment, normalize dashes/underscores to spaces
        slug = href.rstrip("/").split("?", 1)[0].split("#", 1)[0].split("/")[-1]
        slug_text = slug.replace("-", " ").replace("_", " ")
        if not slug_text:
            continue
        for name, test in SERVICE_RULES:
            if test(slug_text):
                return name

    # Tertiary signal: paragraph text. Used only when nothing more prominent
    # matched — copy can mention "party" / "events" incidentally without the
    # section being about that service, so this is intentionally last.
    for p in section.get("paragraphs", []):
        for name, test in SERVICE_RULES:
            if test(p):
                return name

    return "other"


def group_by_service(sections: list) -> dict:
    """Group raw scraper sections by classified service."""
    out: dict = {}
    for s in sections:
        svc = classify_service(s)
        out.setdefault(svc, []).append(s)
    return out


def _html_type(section: Optional[dict]) -> str:
    """Return the section's html_element_type, falling back to default."""
    if not section:
        return ""
    return section.get("html_element_type") or DEFAULT_HTML_TYPE


# ------------------------------------------------------------
# Section pairing (when there are multiple of the same service)
# ------------------------------------------------------------
def _section_signature(section: dict) -> set:
    """Build a bag of normalized tokens representing the section's content."""
    tokens = set()
    for field in ("h1", "h2", "h3", "paragraphs", "list_items"):
        for text in section.get(field, []):
            tokens.update(_normalize(text).split())
    for btn in section.get("buttons", []):
        tokens.update(_normalize(btn.get("text", "")).split())
    return tokens


def _similarity(a: set, b: set) -> float:
    """Jaccard overlap on token sets. 0.0 to 1.0."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def pair_sections(old_list: list, new_list: list) -> list:
    """
    Pair sections from Old and New of the same service by content overlap.
    Returns a list of (old_section_or_None, new_section_or_None) tuples.
    Unpaired sections show up with None on one side.
    """
    pairs = []
    used_new = set()

    # For each Old section, find the best-matching New section
    old_sigs = [(idx, sec, _section_signature(sec)) for idx, sec in enumerate(old_list)]
    new_sigs = [(idx, sec, _section_signature(sec)) for idx, sec in enumerate(new_list)]

    for o_idx, o_sec, o_sig in old_sigs:
        best_score = -1.0
        best_n_idx: Optional[int] = None
        for n_idx, n_sec, n_sig in new_sigs:
            if n_idx in used_new:
                continue
            score = _similarity(o_sig, n_sig)
            if score > best_score:
                best_score = score
                best_n_idx = n_idx
        if best_n_idx is not None:
            used_new.add(best_n_idx)
            pairs.append((o_sec, new_list[best_n_idx]))
        else:
            pairs.append((o_sec, None))

    # New sections that were never paired up
    for n_idx, n_sec, _ in new_sigs:
        if n_idx not in used_new:
            pairs.append((None, n_sec))

    return pairs


# ------------------------------------------------------------
# Section / SEO summaries for the auxiliary sheet tabs
# ------------------------------------------------------------

# Descriptive labels used in the sections tab, derived from a section's
# classified service AND its HTML element type. The HTML type wins when the
# service classifier returns "other" — e.g. a hero cover_video block doesn't
# have a service keyword, but its visual identity is clearly "cover video".
_HTML_TYPE_LABELS = {
    "slideshow":   "slideshow",
    "carousel":    "carousel",
    "cover_video": "cover video",
    "text+image":  "text+image",
}


def _section_label(section: dict) -> str:
    """
    A short, human-readable name for one section. Priority:
      1. Classified service name when it's not 'other' ("about us", "catering",
         "reviews", "locations", "events", etc.) — derived from heading/button
         text via SERVICE_RULES
      2. Scraper-provided 'section_kind' hint when set (e.g. "gallery", "map",
         "cover video" — useful when a section has no heading text the service
         classifier can read)
      3. HTML element type when neither of the above resolves
         ("cover video", "slideshow", "carousel", "text+image")
      4. Fallback "other"
    """
    if section is None:
        return ""
    svc = classify_service(section)
    if svc and svc != "other":
        return svc
    kind_hint = section.get("section_kind") or ""
    if kind_hint:
        return kind_hint
    html_type = section.get("html_element_type") or DEFAULT_HTML_TYPE
    return _HTML_TYPE_LABELS.get(html_type, "other")


def build_section_pairs(old_data: dict, new_data: dict) -> list:
    """
    One row per section, for the 'sections' tab. Pairs old and new
    sections positionally in document order, so missing sections on
    either side surface clearly.

    Each row:
        {
          "old_section_name":  classified service name or kind hint, e.g.
                               "about us", "reviews", "cover video", "gallery",
                               or "MISSING",
          "old_html_type":     "slideshow" / "carousel" / "cover_video" /
                               "text+image" / "",
          "old_heading_text":  the most prominent heading found in the section
                               (H1 if present, else H2, else H3, else ""),
          "new_section_name":  same as old_section_name, for the new side,
          "new_html_type":     same,
          "new_heading_text":  same,
        }

    Unlike build_validated_rows (which groups by service for content
    comparison), this listing aims to be exhaustive: every visual block
    on either side gets a row, including cover videos, custom HTML
    sections, galleries, reviews, locations, etc.
    """
    old_sections = list(old_data.get("sections", []))
    new_sections = list(new_data.get("sections", []))

    out = []
    max_len = max(len(old_sections), len(new_sections))
    for i in range(max_len):
        old_sec = old_sections[i] if i < len(old_sections) else None
        new_sec = new_sections[i] if i < len(new_sections) else None

        out.append({
            # Original index into each side's section list when present, or
            # None when that side has no section at this row. The runner uses
            # these to look up AI labels (keyed by "old_{idx}" / "new_{idx}").
            "old_index":        i if old_sec is not None else None,
            "new_index":        i if new_sec is not None else None,

            "old_section_name": _label_for(old_sec) if old_sec is not None else "MISSING",
            "old_html_type":    _html_type(old_sec) if old_sec is not None else "",
            "old_heading_text": _primary_heading(old_sec) if old_sec is not None else "",
            "new_section_name": _label_for(new_sec) if new_sec is not None else "MISSING",
            "new_html_type":    _html_type(new_sec) if new_sec is not None else "",
            "new_heading_text": _primary_heading(new_sec) if new_sec is not None else "",
        })
    return out


def _primary_heading(section: dict) -> str:
    """
    The most prominent heading text in a section. Returns the joined H1s if
    any exist, else H2s, else H3s, else "". Multiple headings of the same
    level are joined with " | " so reviewers can spot multi-h1 sections (e.g.
    a contact block with both "Location" and "Find us on") at a glance.
    """
    for level in ("h1", "h2", "h3"):
        values = [v.strip() for v in section.get(level, []) if v and v.strip()]
        if values:
            return " | ".join(values)
    return ""


def _label_for(section: Optional[dict]) -> str:
    """Return a short section name, or '' if section is None."""
    if section is None:
        return ""
    return _section_label(section)


def build_h1_pairs(old_data: dict, new_data: dict) -> list:
    """
    Pair every <h1> on the old site with every <h1> on the new site,
    positionally in document order. Used by the 'seo' tab to give each
    H1 its own row for side-by-side SEO review.

    Each row dict contains:
        {
          "old_h1_text":       the H1's text content ("" when missing or empty)
          "old_h1_status":     "text" / "empty" / "MISSING"
                               text    = h1 exists and has text content
                               empty   = h1 element exists but is text-empty
                               MISSING = no h1 at this slot on the old side
          "old_h1_visibility": "visible" / "hidden" / ""
                               (empty when MISSING)
          "new_h1_text":       same shape as old_h1_text, for the new side
          "new_h1_status":     same
          "new_h1_visibility": same
        }

    Pairing strategy: positional. The i-th h1 on the old side is paired
    with the i-th h1 on the new side. When the two sides have different
    counts, the extra rows show MISSING on the absent side. Rebuilds
    typically preserve section order, so positional pairing makes
    misalignment easy to spot.
    """
    old_h1s = list(old_data.get("page_h1s", []))
    new_h1s = list(new_data.get("page_h1s", []))

    out = []
    max_len = max(len(old_h1s), len(new_h1s))
    for i in range(max_len):
        old_h = old_h1s[i] if i < len(old_h1s) else None
        new_h = new_h1s[i] if i < len(new_h1s) else None

        out.append({
            "old_h1_text":       _h1_text(old_h),
            "old_h1_status":     _h1_status(old_h),
            "old_h1_visibility": _h1_visibility(old_h),
            "new_h1_text":       _h1_text(new_h),
            "new_h1_status":     _h1_status(new_h),
            "new_h1_visibility": _h1_visibility(new_h),
        })

    return out


def _h1_text(h: Optional[dict]) -> str:
    """Return the H1's text, or '' when the slot is empty/missing."""
    if not h:
        return ""
    return h.get("text", "") or ""


def _h1_status(h: Optional[dict]) -> str:
    """Per-H1 status: 'text' / 'empty' / 'MISSING'."""
    if not h:
        return "MISSING"
    return "empty" if h.get("empty") else "text"


def _h1_visibility(h: Optional[dict]) -> str:
    """Per-H1 visibility: 'visible' / 'hidden' / ''."""
    if not h:
        return ""
    return "visible" if h.get("visible") else "hidden"


def summarize_h1(data: dict) -> dict:
    """
    SEO summary of a page's <h1> usage. Looks at every <h1> on the page,
    regardless of section membership or visibility, and returns:

        {
          "status":     "text" / "empty" / "missing"
                        text    = at least one <h1> with non-empty text
                        empty   = at least one <h1> tag exists but all are
                                  text-empty
                        missing = no <h1> tag found anywhere on the page
          "text":       joined text content of all non-empty H1s; "" if
                        status is "empty" or "missing"
          "visibility": "visible" / "hidden" / "mixed" / ""
                        visible = all H1s are visible
                        hidden  = all H1s are hidden via class / inline style
                                  / aria-hidden / hidden attribute
                        mixed   = some visible, some hidden
                        ""      = status is "missing"
        }
    """
    h1s = data.get("page_h1s", [])

    if not h1s:
        return {"status": "missing", "text": "", "visibility": ""}

    with_text = [h for h in h1s if not h.get("empty")]

    if not with_text:
        # tags exist but all text-empty
        any_visible = any(h.get("visible") for h in h1s)
        all_visible = all(h.get("visible") for h in h1s)
        if all_visible:
            vis = "visible"
        elif any_visible:
            vis = "mixed"
        else:
            vis = "hidden"
        return {"status": "empty", "text": "", "visibility": vis}

    # At least one H1 with text — visibility describes the H1s with text only,
    # since that's what matters for SEO ranking.
    any_visible = any(h.get("visible") for h in with_text)
    all_visible = all(h.get("visible") for h in with_text)
    if all_visible:
        vis = "visible"
    elif any_visible:
        vis = "mixed"
    else:
        vis = "hidden"

    return {
        "status": "text",
        "text": "; ".join(h["text"] for h in with_text),
        "visibility": vis,
    }


# ------------------------------------------------------------
# Build the flat row data for the main report tab
# ------------------------------------------------------------
def build_validated_rows(old_data: dict, new_data: dict) -> list:
    """
    Produce a flat list of comparison rows for the report tab.

    Each row dict has:
        service           — classified service name
        section_pair      — pair index within service
        old_element       — H1, H2, H3, P, LI, BUTTON, REVIEW (or empty)
        new_element       — H1, H2, H3, P, LI, BUTTON, REVIEW (or empty)
        old_text, old_href, old_hidden, old_html_type
        new_text, new_href, new_hidden, new_html_type
        match             — "OK" / "MISSING on new" / "EXTRA on new" / "DIFFERS"

    Heading shifts (H1→H2, H2→H3) are represented by giving the row different
    values in old_element and new_element columns. There is no arrow syntax.
    """
    old_by_svc = group_by_service(old_data["sections"])
    new_by_svc = group_by_service(new_data["sections"])

    all_services = sorted(set(list(old_by_svc.keys()) + list(new_by_svc.keys())))
    rows = []

    for service in all_services:
        old_sections = old_by_svc.get(service, [])
        new_sections = new_by_svc.get(service, [])

        pairs = pair_sections(old_sections, new_sections)
        for pair_idx, (old_sec, new_sec) in enumerate(pairs, start=1):
            rows.extend(_compare_section_pair(service, pair_idx, old_sec, new_sec))

    # Reviews are compared once across the page (not per-section)
    rows.extend(_compare_reviews(old_data.get("reviews", []), new_data.get("reviews", [])))

    return rows


def _headings_with_levels(section: dict) -> list:
    """
    Return a section's headings as (text, level_label) tuples in H1, H2, H3
    order. level_label is "H1" / "H2" / "H3".
    """
    out = []
    for field, label in (("h1", "H1"), ("h2", "H2"), ("h3", "H3")):
        for text in section.get(field, []):
            if text and text.strip():
                out.append((text, label))
    return out


def _compare_headings(service, pair_idx, old_sec, new_sec,
                      old_type, new_type) -> list:
    """
    Compare headings between two sections WITHOUT assuming a fixed level shift.

    An old heading matches a new heading purely on normalized text, regardless
    of whether it sits at H1/H2/H3 on either side. This handles both rebuild
    styles: ones that shift levels (old H1 -> new H2) and ones that keep them
    (old H2 -> new H2, e.g. identical pages). The element-label columns still
    record the actual level each heading was found at, so a level change is
    visible in the report even though it's reported as OK on text.

    Match outcomes:
      - text present on both sides  -> OK
      - old heading text not on new -> MISSING on new
      - new heading text not on old -> EXTRA on new
    """
    rows = []
    old_headings = _headings_with_levels(old_sec)
    new_headings = _headings_with_levels(new_sec)

    new_remaining = list(new_headings)

    for o_text, o_label in old_headings:
        o_norm = _normalize(o_text)
        matched_idx = None
        for idx, (n_text, n_label) in enumerate(new_remaining):
            if _normalize(n_text) == o_norm:
                matched_idx = idx
                break
        if matched_idx is not None:
            n_text, n_label = new_remaining.pop(matched_idx)
            rows.append(_row(service, pair_idx, o_label, n_label,
                             o_text, "", "", old_type,
                             n_text, "", "", new_type,
                             "OK"))
        else:
            rows.append(_row(service, pair_idx, o_label, "",
                             o_text, "", "", old_type,
                             "", "", "", new_type,
                             "MISSING on new"))

    for n_text, n_label in new_remaining:
        rows.append(_row(service, pair_idx, "", n_label,
                         "", "", "", old_type,
                         n_text, "", "", new_type,
                         "EXTRA on new"))

    return rows


def _compare_section_pair(service, pair_idx, old_sec, new_sec) -> list:
    """Walk a paired (Old, New) section pair and emit comparison rows."""
    out = []

    old_type = _html_type(old_sec)
    new_type = _html_type(new_sec)

    # If one side is missing the whole section, emit one row per element
    # that exists on the present side, marking the other side empty.
    if old_sec is None or new_sec is None:
        present = old_sec or new_sec
        side_missing = "old" if old_sec is None else "new"

        # Headings on the present side (each at its real level)
        for text, label in _headings_with_levels(present):
            if side_missing == "old":
                out.append(_row(service, pair_idx, "", label,
                                "", "", "", old_type,
                                text, "", "", new_type,
                                "EXTRA on new"))
            else:
                out.append(_row(service, pair_idx, label, "",
                                text, "", "", old_type,
                                "", "", "", new_type,
                                "MISSING on new"))

        # Paragraphs / list items on the present side
        for (o_field, n_field, o_label, n_label) in COMPARISON_FIELDS:
            field = o_field if old_sec is None else n_field  # use whichever side exists
            for text in present.get(field, []):
                if side_missing == "old":
                    out.append(_row(service, pair_idx, "", n_label,
                                    "", "", "", old_type,
                                    text, "", "", new_type,
                                    "EXTRA on new"))
                else:
                    out.append(_row(service, pair_idx, o_label, "",
                                    text, "", "", old_type,
                                    "", "", "", new_type,
                                    "MISSING on new"))
        for btn in present.get("buttons", []):
            v = btn.get("visible_text", "") or btn.get("text", "")
            h = btn.get("hidden_text", "")
            href = btn.get("href", "")
            if side_missing == "old":
                out.append(_row(service, pair_idx, "", "BUTTON",
                                "", "", "", old_type,
                                v, href, h, new_type,
                                "EXTRA on new"))
            else:
                out.append(_row(service, pair_idx, "BUTTON", "",
                                v, href, h, old_type,
                                "", "", "", new_type,
                                "MISSING on new"))
        return out

    # Both sections exist.
    # Headings: level-agnostic text match (handles shifted and unshifted rebuilds)
    out.extend(_compare_headings(service, pair_idx, old_sec, new_sec, old_type, new_type))

    # Paragraphs and list items: straight level-to-level comparison
    for (o_field, n_field, o_label, n_label) in COMPARISON_FIELDS:
        old_texts = old_sec.get(o_field, [])
        new_texts = new_sec.get(n_field, [])
        out.extend(_compare_text_lists(
            service, pair_idx, o_label, n_label, old_texts, new_texts,
            old_type, new_type,
        ))

    # Buttons compared with text AND href
    out.extend(_compare_buttons(
        service, pair_idx,
        old_sec.get("buttons", []),
        new_sec.get("buttons", []),
        old_type, new_type,
    ))

    return out


def _compare_text_lists(service, pair_idx, old_label, new_label,
                        old_texts, new_texts,
                        old_type, new_type) -> list:
    """
    Side-by-side comparison of two lists of text strings.
    Output is "one row per Old element + the matching New element".
    Unmatched New items get their own EXTRA rows at the end.
    """
    rows = []
    new_remaining = list(new_texts)

    for o_text in old_texts:
        o_norm = _normalize(o_text)
        matched_n: Optional[str] = None
        # Find an exact normalized match in new_remaining
        for n_text in new_remaining:
            if _normalize(n_text) == o_norm:
                matched_n = n_text
                break
        if matched_n is not None:
            new_remaining.remove(matched_n)
            rows.append(_row(service, pair_idx, old_label, new_label,
                             o_text, "", "", old_type,
                             matched_n, "", "", new_type,
                             "OK"))
        else:
            # Old has text that doesn't appear on New in any form
            # If New has at least one unmatched item, pair them visually with DIFFERS
            if new_remaining:
                n_text = new_remaining.pop(0)
                rows.append(_row(service, pair_idx, old_label, new_label,
                                 o_text, "", "", old_type,
                                 n_text, "", "", new_type,
                                 "DIFFERS"))
            else:
                rows.append(_row(service, pair_idx, old_label, "",
                                 o_text, "", "", old_type,
                                 "", "", "", new_type,
                                 "MISSING on new"))

    for n_text in new_remaining:
        rows.append(_row(service, pair_idx, "", new_label,
                         "", "", "", old_type,
                         n_text, "", "", new_type,
                         "EXTRA on new"))

    return rows


def _compare_buttons(service, pair_idx, old_btns, new_btns,
                     old_type, new_type) -> list:
    """Compare button lists by (visible_text, href) — both must match for OK."""
    rows = []
    new_remaining = list(new_btns)

    for o in old_btns:
        o_visible = (o.get("visible_text") or o.get("text") or "")
        o_hidden = o.get("hidden_text", "")
        o_href = o.get("href", "")
        o_norm = (_normalize(o_visible), _normalize(o_href))

        matched: Optional[dict] = None
        for n in new_remaining:
            n_visible = (n.get("visible_text") or n.get("text") or "")
            n_href = n.get("href", "")
            if (_normalize(n_visible), _normalize(n_href)) == o_norm:
                matched = n
                break

        if matched is not None:
            new_remaining.remove(matched)
            rows.append(_row(service, pair_idx, "BUTTON", "BUTTON",
                             o_visible, o_href, o_hidden, old_type,
                             matched.get("visible_text", "") or matched.get("text", ""),
                             matched.get("href", ""),
                             matched.get("hidden_text", ""),
                             new_type,
                             "OK"))
        else:
            # Look for visible-text match with different href → still differs but pair them
            partial: Optional[dict] = None
            for n in new_remaining:
                n_visible = (n.get("visible_text") or n.get("text") or "")
                if _normalize(n_visible) == _normalize(o_visible):
                    partial = n
                    break
            if partial is not None:
                new_remaining.remove(partial)
                rows.append(_row(service, pair_idx, "BUTTON", "BUTTON",
                                 o_visible, o_href, o_hidden, old_type,
                                 partial.get("visible_text", "") or partial.get("text", ""),
                                 partial.get("href", ""),
                                 partial.get("hidden_text", ""),
                                 new_type,
                                 "DIFFERS"))
            elif new_remaining:
                n = new_remaining.pop(0)
                rows.append(_row(service, pair_idx, "BUTTON", "BUTTON",
                                 o_visible, o_href, o_hidden, old_type,
                                 n.get("visible_text", "") or n.get("text", ""),
                                 n.get("href", ""),
                                 n.get("hidden_text", ""),
                                 new_type,
                                 "DIFFERS"))
            else:
                rows.append(_row(service, pair_idx, "BUTTON", "",
                                 o_visible, o_href, o_hidden, old_type,
                                 "", "", "", new_type,
                                 "MISSING on new"))

    for n in new_remaining:
        rows.append(_row(service, pair_idx, "", "BUTTON",
                         "", "", "", old_type,
                         n.get("visible_text", "") or n.get("text", ""),
                         n.get("href", ""),
                         n.get("hidden_text", ""),
                         new_type,
                         "EXTRA on new"))

    return rows


def _compare_reviews(old_reviews, new_reviews) -> list:
    """
    Compare reviews across the two sites. Each review is its own row.
    In a rebuild the same set of reviews should appear on both sides.
    Reviews are rendered as a carousel on both old and new sites.
    """
    rows = []
    new_keys = {_review_key(r): r for r in new_reviews}
    seen_new = set()

    # Reviews are always rendered as a carousel widget
    review_type = "carousel"

    for o in old_reviews:
        k = _review_key(o)
        n = new_keys.get(k)
        if n is not None:
            seen_new.add(k)
            rows.append(_row("reviews", 1, "REVIEW", "REVIEW",
                             o.get("text", ""), "", o.get("reviewer", ""), review_type,
                             n.get("text", ""), "", n.get("reviewer", ""), review_type,
                             "OK"))
        else:
            rows.append(_row("reviews", 1, "REVIEW", "",
                             o.get("text", ""), "", o.get("reviewer", ""), review_type,
                             "", "", "", review_type,
                             "MISSING on new"))

    for n in new_reviews:
        if _review_key(n) not in seen_new:
            rows.append(_row("reviews", 1, "", "REVIEW",
                             "", "", "", review_type,
                             n.get("text", ""), "", n.get("reviewer", ""), review_type,
                             "EXTRA on new"))

    return rows


def _review_key(review: dict) -> str:
    """Normalize a review for set membership."""
    text = review.get("text") or ""
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _row(service, pair_idx, old_element, new_element,
         old_text, old_href, old_hidden, old_html_type,
         new_text, new_href, new_hidden, new_html_type,
         match) -> dict:
    """Construct a single comparison row in the canonical shape."""
    return {
        "service": service,
        "section_pair": pair_idx,
        "old_element": old_element,
        "new_element": new_element,
        "old_text": old_text,
        "old_href": old_href,
        "old_hidden": old_hidden,
        "old_html_type": old_html_type,
        "new_text": new_text,
        "new_href": new_href,
        "new_hidden": new_hidden,
        "new_html_type": new_html_type,
        "match": match,
    }


def _normalize(s: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace, strip."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())