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


# Heading + paragraph text is compared by _compare_text_elements (the SEO
# demotion model). The only element type still compared straight
# level-to-level here is list_items, which isn't part of the heading ladder.
COMPARISON_FIELDS = [
    ("list_items", "list_items", "LI", "LI"),
]

# Heading levels pooled together for the level-agnostic comparison.
HEADING_FIELDS = ("h1", "h2", "h3", "h4")


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
    for field in ("h1", "h2", "h3", "h4", "paragraphs", "list_items"):
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


def assign_section_ordinals(sections: list) -> dict:
    """
    Walk a page's sections in document order and assign each an ordinal name
    within its label, so duplicate section types are distinguishable by their
    position on the page:

        1st "catering"  -> "Catering 1"
        2nd "catering"  -> "Catering 2"
        1st "slideshow" -> "Slideshow 1"
        ...

    Returns {id(section): "Label N"}. The base label is _section_label(); the
    ordinal counts occurrences of that same base label top to bottom. The name
    is Title-cased for display. These names are used both in the 'sections' tab
    and as the primary section-pairing key (old "Catering 1" pairs with new
    "Catering 1").
    """
    counts = {}
    names = {}
    for sec in sections:
        if sec is None:
            continue
        base = _section_label(sec) or "other"
        counts[base] = counts.get(base, 0) + 1
        names[id(sec)] = f"{base[:1].upper()}{base[1:]} {counts[base]}"
    return names


def _ordinal_key(name: str) -> str:
    """Normalize an ordinal section name for matching (lowercase, single space)."""
    return _normalize(name)


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

    # Ordinal names ("Catering 1", "Catering 2", ...) per side, in doc order.
    old_names = assign_section_ordinals(old_sections)
    new_names = assign_section_ordinals(new_sections)

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

            "old_section_name": old_names.get(id(old_sec), "") if old_sec is not None else "MISSING",
            "old_html_type":    _html_type(old_sec) if old_sec is not None else "",
            "old_heading_text": _primary_heading(old_sec) if old_sec is not None else "",
            "new_section_name": new_names.get(id(new_sec), "") if new_sec is not None else "MISSING",
            "new_html_type":    _html_type(new_sec) if new_sec is not None else "",
            "new_heading_text": _primary_heading(new_sec) if new_sec is not None else "",
        })
    return out


def _section_header_key(section: dict) -> str:
    """
    Normalized text of a section's HEADER heading — the single most prominent
    heading, used as a strong identity signal when pairing sections across the
    rebuild. Returns the first heading found scanning H1->H4 (after the rebuild
    a section header is typically demoted H1->H2 but keeps its text), or "".

    Distinct from _primary_heading, which joins ALL headings of a level; here
    we want just the lead heading so "Specials" matches "Specials" even when
    the new side added extra headings like "We are updating our specials".
    """
    for level in ("h1", "h2", "h3", "h4"):
        for v in section.get(level, []):
            if v and v.strip():
                return _normalize(v)
    return ""


def _primary_heading(section: dict) -> str:
    """
    The most prominent heading text in a section. Returns the joined H1s if
    any exist, else H2s, else H3s, else "". Multiple headings of the same
    level are joined with " | " so reviewers can spot multi-h1 sections (e.g.
    a contact block with both "Location" and "Find us on") at a glance.
    """
    for level in ("h1", "h2", "h3", "h4"):
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

    Pairing strategy
    ----------------
    Sections are paired by CONTENT SIMILARITY across the whole page, NOT by
    grouping on the service label first. This matters because the same logical
    section can get different labels on the old vs new site (e.g. the AI calls
    the old one "about us" and the new one "food menu"). Label-first grouping
    would then drop them into separate buckets and emit a spurious
    MISSING-on-new plus EXTRA-on-new pair for what is really one matched
    section. Content-based pairing matches them by their shared headings /
    paragraphs / button text instead, so they line up correctly.

    Each row dict has:
        service           — the pair's service label (old side's label
                            preferred; the non-"other" side wins when they
                            disagree)
        section_pair      — running pair index
        old_element / new_element — H1, H2, H3, P, LI, BUTTON, REVIEW (or "")
        old_text, old_href, old_hidden, old_html_type
        new_text, new_href, new_hidden, new_html_type
        match             — "OK" / "MISSING on new" / "EXTRA on new" / "DIFFERS"
    """
    old_sections = list(old_data.get("sections", []))
    new_sections = list(new_data.get("sections", []))

    # Ordinal names ("Catering 1", "Catering 2", ...) per side, so the report's
    # section-name column matches the 'sections' tab and distinguishes repeated
    # section types.
    old_names = assign_section_ordinals(old_sections)
    new_names = assign_section_ordinals(new_sections)

    pairs = _pair_sections_global(old_sections, new_sections)
    rows = []
    for pair_idx, (old_sec, new_sec) in enumerate(pairs, start=1):
        # Each side carries its OWN ordinal AI label, so the report can show
        # "Old site section name" and "New site section name" separately — they
        # can differ when the AI labeled the two sides differently.
        old_name = old_names.get(id(old_sec), "") if old_sec is not None else ""
        new_name = new_names.get(id(new_sec), "") if new_sec is not None else ""
        # Fall back to the resolved pair label if a side has no ordinal name.
        if not old_name and not new_name:
            fallback = _pair_service_label(old_sec, new_sec)
            old_name = new_name = fallback
        service = (old_name, new_name)
        rows.extend(_compare_section_pair(service, pair_idx, old_sec, new_sec))

    # Reviews are compared once across the page (not per-section)
    rows.extend(_compare_reviews(old_data.get("reviews", []), new_data.get("reviews", [])))

    return rows


# Minimum content overlap (Jaccard on token sets) for two sections to be
# considered the same logical section across old/new. Below this, sections
# are treated as genuinely unmatched (one MISSING, the other EXTRA).
_PAIR_MATCH_THRESHOLD = 0.12


def _pair_service_label(old_sec, new_sec) -> str:
    """
    Decide the service label to display for a matched pair. Prefer the old
    side's label; if it's empty/"other", use the new side's; if both are
    set but disagree, prefer the more specific (non-"other") one, defaulting
    to the old side.
    """
    old_lbl = classify_service(old_sec) if old_sec is not None else ""
    new_lbl = classify_service(new_sec) if new_sec is not None else ""
    if old_lbl and old_lbl != "other":
        return old_lbl
    if new_lbl and new_lbl != "other":
        return new_lbl
    return old_lbl or new_lbl or "other"


def _pair_sections_global(old_list: list, new_list: list) -> list:
    """
    Pair old and new sections across the WHOLE page.

    Two-pass strategy:
      Pass 1 — ORDINAL NAME match. Each section is named by its type plus its
        position among same-type sections in document order ("Catering 1",
        "Catering 2", ...). Old "Catering 1" pairs with new "Catering 1", old
        "Catering 2" with new "Catering 2", and so on. This reliably aligns
        repeated section types (multiple locations, an Order section that
        appears both standalone and inside a slideshow) that pure content
        similarity would cross-pair.
      Pass 2 — CONTENT SIMILARITY fallback for whatever's left. Sections with
        no ordinal counterpart (a type that exists a different number of times
        on each side, or only on one side) are matched greedily by Jaccard
        similarity with the shared-header boost.

    Leftover sections on either side become unpaired (None on the other side).
    Document order is preserved as much as possible by sorting on the old
    section's original index.
    """
    old_names = assign_section_ordinals(old_list)
    new_names = assign_section_ordinals(new_list)

    used_old = set()
    used_new = set()
    matched = []  # (old_idx, new_idx)

    # --- Pass 1: ordinal-name match ("Catering 1" <-> "Catering 1") ---
    new_by_name = {}
    for j, sec in enumerate(new_list):
        new_by_name.setdefault(_ordinal_key(new_names[id(sec)]), []).append(j)
    for i, sec in enumerate(old_list):
        key = _ordinal_key(old_names[id(sec)])
        bucket = new_by_name.get(key)
        if bucket:
            j = bucket.pop(0)
            used_old.add(i)
            used_new.add(j)
            matched.append((i, j))

    # --- Pass 2: content-similarity fallback for the leftovers ---
    old_sigs = [(i, sec, _section_signature(sec)) for i, sec in enumerate(old_list)
                if i not in used_old]
    new_sigs = [(j, sec, _section_signature(sec)) for j, sec in enumerate(new_list)
                if j not in used_new]

    candidates = []  # (score, old_idx, new_idx)
    for i, o_sec, o_sig in old_sigs:
        o_header = _section_header_key(o_sec)
        for j, n_sec, n_sig in new_sigs:
            score = _similarity(o_sig, n_sig)
            n_header = _section_header_key(n_sec)
            if o_header and n_header and o_header == n_header:
                score = max(score, 0.95)
            if score >= _PAIR_MATCH_THRESHOLD:
                candidates.append((score, i, j))
    candidates.sort(reverse=True)
    for score, i, j in candidates:
        if i in used_old or j in used_new:
            continue
        used_old.add(i)
        used_new.add(j)
        matched.append((i, j))

    pairs = []
    for i, j in matched:
        pairs.append((i, j, old_list[i], new_list[j]))
    # Unmatched old → MISSING on new
    for i, o_sec in enumerate(old_list):
        if i not in used_old:
            pairs.append((i, -1, o_sec, None))
    # Unmatched new → EXTRA on new
    for j, n_sec in enumerate(new_list):
        if j not in used_new:
            pairs.append((10_000 + j, j, None, n_sec))

    # Keep document order roughly intact: sort by old index, then new index
    pairs.sort(key=lambda t: (t[0], t[1]))
    return [(o, n) for (_, _, o, n) in pairs]


def _headings_with_levels(section: dict) -> list:
    """
    Return a section's headings as (text, level_label) tuples in H1..H4
    order. level_label is "H1" / "H2" / "H3" / "H4". (Kept for the
    one-side-missing branch and any external callers.)
    """
    out = []
    for field, label in (("h1", "H1"), ("h2", "H2"), ("h3", "H3"), ("h4", "H4")):
        for text in section.get(field, []):
            if text and text.strip():
                out.append((text, label))
    return out


# Rank ladder for the SEO heading-demotion model. Lower number = more
# prominent. A rebuild aimed at SEO keeps a single H1 and pushes the rest
# DOWN the ladder (H1->H2->H3->H4, and sometimes a heading all the way to a
# paragraph). Moving DOWN with the same text is the EXPECTED, intended change.
# Moving UP, or staying put, still counts as the text having survived (OK).
_RANK = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "P": 5}


def _text_elements_with_rank(section: dict) -> list:
    """
    Pool a section's heading + paragraph text into a single ordered list of
    (text, label, rank) tuples, where label is "H1".."H4" or "P" and rank is
    the position on the demotion ladder (H1=1 .. P=5).

    Headings and paragraphs are pooled TOGETHER so we can detect a heading
    that was demoted into a paragraph during the rebuild (e.g. old H2 whose
    text now appears as a new <p>).
    """
    out = []
    for field, label in (("h1", "H1"), ("h2", "H2"), ("h3", "H3"), ("h4", "H4"),
                         ("paragraphs", "P")):
        for text in section.get(field, []):
            if text and text.strip():
                out.append((text, label, _RANK[label]))
    return out


def _move_match_status(old_rank: int, new_rank: int) -> str:
    """
    Classify a same-text match by how the element's rank changed:
      - same rank          -> "OK"        (no change)
      - moved DOWN (new>old)-> "EXPECTED" (intended SEO demotion)
      - moved UP   (new<old)-> "OK"       (text survived; unusual but fine)
    """
    if new_rank == old_rank:
        return "OK"
    if new_rank > old_rank:
        return "EXPECTED"
    return "OK"


_REVIEW_PLATFORMS = (
    "google", "yelp", "facebook", "tripadvisor", "trip advisor", "opentable",
    "open table", "zomato", "foursquare", "grubhub", "doordash", "ubereats",
    "uber eats", "instagram", "apple", "apple maps", "bing", "trustpilot",
)


def _review_source_platform(text: str) -> str:
    """
    If `text` is a review-SOURCE heading, return the normalized platform name
    it names; otherwise "".

    Matches both the full template form ("Review by - Google", "Reviews by -
    Yelp") and the shortened rebuild form where only the platform survives
    ("Google", "Yelp"). The bare-platform case only counts when the whole
    heading IS just a known platform name, so ordinary headings that merely
    mention a brand aren't swept in.
    """
    norm = _normalize(text)
    if not norm:
        return ""
    # Full "review(s) by - <platform>" form: take the tail after "by".
    m = re.match(r"^reviews?\s+by\b[\s\-:|]*(.+)$", norm)
    if m:
        tail = m.group(1).strip()
        for p in _REVIEW_PLATFORMS:
            if p in tail:
                return p.replace(" ", "")
        return tail.replace(" ", "") if tail else ""
    # Bare platform name as the entire heading ("Google", "Yelp").
    for p in _REVIEW_PLATFORMS:
        if norm == p:
            return p.replace(" ", "")
    return ""


def _token_set_ratio(a: str, b: str) -> float:
    """
    Jaccard overlap of the word-token sets of two strings (0..1). Robust to
    word insertions/reorderings: "2955 jamacha road el cajon ca 92019" vs
    "2955 jamacha road el cajon la mesa ca 92019" share most tokens, so the
    ratio is high even though the strings aren't equal.
    """
    ta = set(_normalize(a).split())
    tb = set(_normalize(b).split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _common_prefix_ratio(a: str, b: str) -> float:
    """
    Length of the shared leading substring divided by the shorter string's
    length (0..1). Two address headings that start identically
    ("2955 Jamacha Road ...") score high here even if they diverge later.
    """
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    n = min(len(na), len(nb))
    i = 0
    while i < n and na[i] == nb[i]:
        i += 1
    return i / min(len(na), len(nb))


# Thresholds for the fuzzy element-matching fallback. A pair is considered the
# "same" element (text changed during the rebuild) when either the token-set
# overlap or the common-prefix ratio clears its bar.
_FUZZY_TOKEN_RATIO = 0.5
_FUZZY_PREFIX_RATIO = 0.6


def _compare_text_elements(service, pair_idx, old_sec, new_sec,
                           old_type, new_type) -> list:
    """
    Unified comparison of all heading + paragraph text across two sections,
    using the SEO heading-demotion model.

    Both sides' H1-H4 and P text are pooled and matched by normalized text.
    For a matched pair the status reflects the rank movement:
        same level                 -> OK
        moved down (incl. ->P)     -> EXPECTED   (intended SEO change)
        moved up                   -> OK
    Unmatched old text             -> MISSING on new
    Unmatched new text             -> EXTRA on new

    The Old element / New element columns record the actual levels (e.g.
    "H2" and "P") so a reviewer can see exactly how a heading moved.
    """
    rows = []
    old_items = _text_elements_with_rank(old_sec)
    new_items = _text_elements_with_rank(new_sec)
    new_remaining = list(new_items)

    old_hidden_map = old_sec.get("heading_hidden", {}) or {}
    new_hidden_map = new_sec.get("heading_hidden", {}) or {}

    # Pass 1: exact / plural-tolerant text match.
    leftover_old = []
    for o_text, o_label, o_rank in old_items:
        o_hidden = old_hidden_map.get(o_text, "")
        matched_idx = None
        match_status = None
        for idx, (n_text, n_label, n_rank) in enumerate(new_remaining):
            if _text_equiv(n_text, o_text):
                matched_idx = idx
                break
        # Review-SOURCE heading match: "Review by - Google" (old) vs "Google"
        # (new) name the same platform — the rebuild often drops the
        # "Review by -" prefix and keeps just the source. Pair them by the
        # extracted platform name as an EXPECTED template change.
        if matched_idx is None:
            o_src = _review_source_platform(o_text)
            if o_src:
                for idx, (n_text, n_label, n_rank) in enumerate(new_remaining):
                    n_src = _review_source_platform(n_text)
                    if n_src and n_src == o_src:
                        matched_idx = idx
                        match_status = "EXPECTED"
                        break
        if matched_idx is not None:
            n_text, n_label, n_rank = new_remaining.pop(matched_idx)
            n_hidden = new_hidden_map.get(n_text, "")
            if match_status is not None:
                status = match_status
            else:
                status = _move_match_status(o_rank, n_rank)
                # Visible text matches but the visually-hidden text differs
                # (e.g. the new side added a "Visit us at" sr-only prefix) ->
                # EXPECTED accessibility change rather than a plain OK.
                if status == "OK" and _normalize(o_hidden) != _normalize(n_hidden):
                    status = "EXPECTED"
            rows.append(_row(service, pair_idx, o_label, n_label,
                             o_text, "", o_hidden, old_type,
                             n_text, "", n_hidden, new_type,
                             status))
        else:
            leftover_old.append((o_text, o_label, o_rank))

    # Pass 2: fuzzy match on the leftovers. For each unmatched old element,
    # score every unmatched new element by token-set ratio (primary) and
    # common-prefix ratio (secondary); a candidate qualifies if either clears
    # its threshold. Ties are broken by preferring the closest RANK (a demoted
    # heading keeps a near rank), then the highest combined score. A fuzzy
    # match means "same element, text edited during the rebuild" -> EXPECTED.
    for o_text, o_label, o_rank in leftover_old:
        o_hidden = old_hidden_map.get(o_text, "")
        best = None  # (rank_distance, -score, idx)
        for idx, (n_text, n_label, n_rank) in enumerate(new_remaining):
            tr = _token_set_ratio(o_text, n_text)
            pr = _common_prefix_ratio(o_text, n_text)
            if tr >= _FUZZY_TOKEN_RATIO or pr >= _FUZZY_PREFIX_RATIO:
                score = max(tr, pr)
                rank_dist = abs(o_rank - n_rank)
                cand = (rank_dist, -score, idx)
                if best is None or cand < best:
                    best = cand
        if best is not None:
            idx = best[2]
            n_text, n_label, n_rank = new_remaining.pop(idx)
            n_hidden = new_hidden_map.get(n_text, "")
            # Fuzzy pair: the element is the same but its text was edited
            # (e.g. an address that gained "La Mesa") -> EXPECTED.
            rows.append(_row(service, pair_idx, o_label, n_label,
                             o_text, "", o_hidden, old_type,
                             n_text, "", n_hidden, new_type,
                             "EXPECTED"))
        else:
            rows.append(_row(service, pair_idx, o_label, "",
                             o_text, "", o_hidden, old_type,
                             "", "", "", new_type,
                             "MISSING on new"))

    for n_text, n_label, n_rank in new_remaining:
        n_hidden = new_hidden_map.get(n_text, "")
        rows.append(_row(service, pair_idx, "", n_label,
                         "", "", "", old_type,
                         n_text, "", n_hidden, new_type,
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

        # Heading + paragraph text on the present side (each at its real level)
        for text, label, _rank in _text_elements_with_rank(present):
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

        # List items on the present side
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
        # Phone numbers present on the existing side (clickable or plain text)
        for ph in _extract_phones(present):
            if side_missing == "old":
                out.append(_row(service, pair_idx, "", "PHONE",
                                "", "", "", old_type,
                                ph["display"], "", "", new_type,
                                "EXTRA on new"))
            else:
                out.append(_row(service, pair_idx, "PHONE", "",
                                ph["display"], "", "", old_type,
                                "", "", "", new_type,
                                "MISSING on new"))

        for btn in present.get("buttons", []):
            # tel: links are reported as PHONE rows above, not as buttons
            if (btn.get("href", "") or "").lower().startswith("tel:"):
                continue
            v = btn.get("visible_text", "") or btn.get("text", "")
            h = btn.get("hidden_text", "")
            href = btn.get("href", "")
            if side_missing == "old":
                status = "EXPECTED" if _is_expected_affordance(btn) else "EXTRA on new"
                out.append(_row(service, pair_idx, "", "BUTTON",
                                "", "", "", old_type,
                                v, href, h, new_type,
                                status))
            else:
                out.append(_row(service, pair_idx, "BUTTON", "",
                                v, href, h, old_type,
                                "", "", "", new_type,
                                "MISSING on new"))
        return out

    # Both sections exist.
    # Headings + paragraphs: unified comparison with the SEO demotion model
    # (same level = OK, moved down / to paragraph = EXPECTED, moved up = OK).
    out.extend(_compare_text_elements(service, pair_idx, old_sec, new_sec, old_type, new_type))

    # List items: straight level-to-level comparison (not part of the
    # heading demotion ladder).
    for (o_field, n_field, o_label, n_label) in COMPARISON_FIELDS:
        old_texts = old_sec.get(o_field, [])
        new_texts = new_sec.get(n_field, [])
        out.extend(_compare_text_lists(
            service, pair_idx, o_label, n_label, old_texts, new_texts,
            old_type, new_type,
        ))

    # Phone numbers: compared by digits across BOTH clickable tel: links and
    # plain text, so a number that became plain text (or vice versa) still
    # pairs as OK. Handled before buttons so tel: links aren't double-counted.
    out.extend(_compare_phones(service, pair_idx, old_sec, new_sec, old_type, new_type))

    # Buttons compared with text AND href. tel: links are excluded here because
    # phone numbers are handled by _compare_phones above (otherwise an old tel:
    # link with no clickable counterpart on the new side would also show up as
    # a spurious MISSING button).
    out.extend(_compare_buttons(
        service, pair_idx,
        [b for b in old_sec.get("buttons", []) if not (b.get("href", "") or "").lower().startswith("tel:")],
        [b for b in new_sec.get("buttons", []) if not (b.get("href", "") or "").lower().startswith("tel:")],
        old_type, new_type,
    ))

    return out


def _phone_digits(s: str) -> str:
    """
    Extract a normalized phone-number key from a string: the trailing 10 digits
    (US numbers), or all digits if fewer than 10. Returns "" if there aren't
    enough digits to look like a phone number. Using the last 10 digits makes
    "+1 (843) 640-3986", "843-640-3986", and "tel:+18436403986" all collapse to
    "8436403986".
    """
    digits = re.sub(r"\D", "", s or "")
    if len(digits) < 10:
        return ""
    return digits[-10:]


# A phone number appearing in free text: matches (843)-640-3986,
# 843-640-3986, 843.640.3986, +1 843 640 3986, etc.
_PHONE_TEXT_RE = re.compile(
    r"(?:\+?\d[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}"
)


def _extract_phones(section: dict) -> list:
    """
    Find every phone number in a section, whether it's a clickable tel: link
    (a button) or plain text (in a heading/paragraph/list item). Returns a list
    of dicts: {"key": <last-10-digits>, "display": <as shown>, "clickable": bool}.

    De-duplicated by key so the same number listed twice in one section counts
    once per side.
    """
    found = {}

    def add(display, key, clickable):
        if not key:
            return
        # Prefer a clickable record over a non-clickable one for the same number
        if key not in found or (clickable and not found[key]["clickable"]):
            found[key] = {"key": key, "display": display.strip(), "clickable": clickable}

    # 1. tel: link buttons
    for b in section.get("buttons", []):
        href = b.get("href", "") or ""
        if href.lower().startswith("tel:"):
            key = _phone_digits(href)
            disp = (b.get("visible_text") or b.get("text") or href[4:]).strip()
            add(disp, key, clickable=True)

    # 2. plain-text phone numbers in headings / paragraphs / list items
    for field in ("h1", "h2", "h3", "h4", "paragraphs", "list_items"):
        for text in section.get(field, []):
            for m in _PHONE_TEXT_RE.finditer(text or ""):
                snippet = m.group(0)
                add(snippet, _phone_digits(snippet), clickable=False)

    # 3. plain-text phone numbers anywhere else in the section's text content.
    #    SpotHopper hours blocks often put the number in a bare <div> (e.g.
    #    "Phone: 843-640-3986"), which isn't captured as a paragraph/heading,
    #    so we also scan the section's full raw_text. Numbers already found
    #    above are de-duplicated by key.
    for m in _PHONE_TEXT_RE.finditer(section.get("raw_text", "") or ""):
        snippet = m.group(0)
        add(snippet, _phone_digits(snippet), clickable=False)

    return list(found.values())


def _compare_phones(service, pair_idx, old_sec, new_sec, old_type, new_type) -> list:
    """
    Compare phone numbers between two sections by their digits, regardless of
    whether each side renders the number as a clickable tel: link or as plain
    text. Same digits on both sides -> OK (the display columns show each
    side's form, so a link-became-text change is still visible). A number on
    only one side -> MISSING on new / EXTRA on new.
    """
    rows = []
    old_phones = {p["key"]: p for p in _extract_phones(old_sec)}
    new_phones = {p["key"]: p for p in _extract_phones(new_sec)}

    for key, o in old_phones.items():
        n = new_phones.get(key)
        if n is not None:
            rows.append(_row(service, pair_idx, "PHONE", "PHONE",
                             o["display"], "", "", old_type,
                             n["display"], "", "", new_type,
                             "OK"))
        else:
            rows.append(_row(service, pair_idx, "PHONE", "",
                             o["display"], "", "", old_type,
                             "", "", "", new_type,
                             "MISSING on new"))

    for key, n in new_phones.items():
        if key not in old_phones:
            rows.append(_row(service, pair_idx, "", "PHONE",
                             "", "", "", old_type,
                             n["display"], "", "", new_type,
                             "EXTRA on new"))

    return rows


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


_SOCIAL_HOSTS = [
    ("twitter",   ("twitter.com", "x.com")),
    ("facebook",  ("facebook.com", "fb.com", "fb.me")),
    ("instagram", ("instagram.com",)),
    ("yelp",      ("yelp.com",)),
    ("google",    ("google.com/maps", "maps.google.com", "g.page",
                   "google.com/search", "business.google.com")),
    ("apple",     ("apps.apple.com", "maps.apple.com")),
    ("tiktok",    ("tiktok.com",)),
    ("youtube",   ("youtube.com", "youtu.be")),
    ("linkedin",  ("linkedin.com",)),
    ("pinterest", ("pinterest.com",)),
]


def _social_network(btn: dict) -> Optional[str]:
    """
    Identify which social network a button points at, based on its href host.
    Returns a network key ("twitter", "facebook", ...) or None.

    Matching on the host (not the visible text) lets us pair the same network
    across a rebrand even when the label changed ("Twitter page" vs
    "Twitter/ X page") or the buttons were reordered. The Google entry is
    host+path aware because google.com is also used for plain search links.
    """
    href = (btn.get("href") or "").lower()
    if not href:
        return None
    h = re.sub(r"^https?://", "", href)
    h = re.sub(r"^www\.", "", h)
    for network, needles in _SOCIAL_HOSTS:
        if any(needle in h for needle in needles):
            return network
    return None


def _compare_social_buttons(service, pair_idx, old_socials, new_socials,
                            old_type, new_type) -> list:
    """
    Pair social-media buttons by network (twitter<->twitter, etc.) regardless
    of label wording, link order, or cosmetic href differences. Same network
    on both sides -> OK. A network present on only one side is reported
    (MISSING/EXTRA) so a genuinely added/removed social link still surfaces.
    """
    rows = []
    old_by_net = {}
    new_by_net = {}
    for b in old_socials:
        old_by_net.setdefault(_social_network(b), []).append(b)
    for b in new_socials:
        new_by_net.setdefault(_social_network(b), []).append(b)

    networks = list(old_by_net.keys())
    for net in new_by_net:
        if net not in networks:
            networks.append(net)

    for net in networks:
        o_list = old_by_net.get(net, [])
        n_list = new_by_net.get(net, [])
        paired = min(len(o_list), len(n_list))
        for i in range(paired):
            o, n = o_list[i], n_list[i]
            # Same network → OK, regardless of label wording or href format
            rows.append(_row(service, pair_idx, "BUTTON", "BUTTON",
                             o.get("visible_text", "") or o.get("text", ""),
                             o.get("href", ""), o.get("hidden_text", ""), old_type,
                             n.get("visible_text", "") or n.get("text", ""),
                             n.get("href", ""), n.get("hidden_text", ""), new_type,
                             "OK"))
        for o in o_list[paired:]:
            rows.append(_row(service, pair_idx, "BUTTON", "",
                             o.get("visible_text", "") or o.get("text", ""),
                             o.get("href", ""), o.get("hidden_text", ""), old_type,
                             "", "", "", new_type,
                             "MISSING on new"))
        for n in n_list[paired:]:
            rows.append(_row(service, pair_idx, "", "BUTTON",
                             "", "", "", old_type,
                             n.get("visible_text", "") or n.get("text", ""),
                             n.get("href", ""), n.get("hidden_text", ""), new_type,
                             "EXTRA on new"))
    return rows


def _is_expected_affordance(btn: dict) -> bool:
    """
    True if a button is a template-added accessibility / widget affordance that
    the new SpotHopper template introduces and the old site lacked — e.g. a
    "Skip Photo Gallery" skip-link or a "Reset zoom" map control. When such a
    button appears only on the new side it's an EXPECTED template addition, not
    a real content difference, so it should be marked EXPECTED rather than
    EXTRA on new.

    Matched on combined visible + hidden text, lowercased.
    """
    text = _normalize(
        " ".join(x for x in [
            btn.get("visible_text") or btn.get("text") or "",
            btn.get("hidden_text") or "",
        ] if x)
    )
    if not text:
        return False
    patterns = (
        r"\bskip\b.*\b(photo\s*gallery|gallery|slideshow|carousel|content|to main)\b",
        r"\breset\s*zoom\b",
        r"\bzoom\s*(in|out)\b",
        r"\bskip to\b",
        r"\bback to top\b",
        r"\benable\s*(accessibility|high contrast)\b",
    )
    return any(re.search(p, text) for p in patterns)


def _carousel_control_kind(btn: dict) -> Optional[str]:
    """
    Identify carousel widget chrome (not content) by its button text.
    Returns a sub-kind so we only pair like with like:
      - "play_pause"  the autoplay toggle: "Play reviews carousel",
                      "Stop reviews carousel", "Start stop reviews carousel",
                      "pause", "play slideshow", etc.
      - "dot_nav"     the slide dots: "dot navigation slide 3", "Review 2",
                      "slide 4", "go to slide 1", etc.
      - "arrow_nav"   prev/next arrows: the bare glyphs/words used for
                      stepping the carousel ("previous", "next", "‹", "›")
      - None          not a carousel control

    Each candidate string (the visible text, the hidden text, and the two
    combined) is tested, because the meaningful label sometimes lives only in
    the screen-reader (hidden) span and the visible+hidden combo can duplicate
    words (e.g. "Review 1 Review 1") which would break a fullmatch.
    """
    candidates = []
    vis = _normalize(btn.get("visible_text") or btn.get("text") or "")
    hid = _normalize(btn.get("hidden_text") or "")
    if vis:
        candidates.append(vis)
    if hid:
        candidates.append(hid)
    if vis and hid and vis != hid:
        candidates.append(_normalize(f"{vis} {hid}"))
    if not candidates:
        return None

    def any_match(pred) -> bool:
        return any(pred(c) for c in candidates)

    # play / pause / stop / start-stop toggle for a carousel or slideshow
    if any_match(lambda t: bool(
        re.search(r"\b(play|pause|stop|start stop|autoplay)\b.*\b(carousel|slideshow|slider|reviews?)\b", t) or
        re.search(r"\b(carousel|slideshow|slider|reviews?)\b.*\b(play|pause|stop|start stop|autoplay)\b", t)
    )):
        return "play_pause"

    # dot navigation: "dot navigation slide N", "go to slide N", "slide N",
    # or the SpotHopper review-dot label "Review N"
    if any_match(lambda t: bool(
        re.search(r"\bdot navigation\b", t) or
        re.search(r"\bslide\s+\d+\b", t) or
        re.search(r"\bgo to slide\b", t) or
        re.fullmatch(r"review\s+\d+", t)
    )):
        return "dot_nav"

    # prev / next arrows (bare glyphs or words)
    if any_match(lambda t: (
        t in {"‹", "›", "<", ">", "«", "»", "previous", "next", "prev"} or
        bool(re.search(r"\b(previous|next)\s+(slide|review|item)\b", t))
    )):
        return "arrow_nav"

    return None


def _compare_carousel_controls(service, pair_idx, old_ctrls, new_ctrls,
                               old_type, new_type) -> list:
    """
    Pair carousel widget chrome (play/pause, dot-nav, arrows) by sub-kind and
    mark every pairing OK regardless of the exact label wording — the controls
    are functionally equivalent even when the rebuild renames them (e.g. old
    "Play reviews carousel" + "Stop reviews carousel" vs new "Start stop
    reviews carousel"; old "dot navigation slide N" vs new "Review N").

    Within each sub-kind we pair positionally up to the shorter list, marking
    those OK. Any surplus on one side is reported (MISSING/EXTRA) so a genuine
    change in control count is still visible.
    """
    rows = []

    def bucketize(ctrls):
        buckets = {"play_pause": [], "dot_nav": [], "arrow_nav": []}
        for b in ctrls:
            kind = _carousel_control_kind(b)
            if kind:
                buckets[kind].append(b)
        return buckets

    old_b = bucketize(old_ctrls)
    new_b = bucketize(new_ctrls)

    for kind in ("play_pause", "dot_nav", "arrow_nav"):
        o_list = old_b[kind]
        n_list = new_b[kind]
        paired = min(len(o_list), len(n_list))
        for i in range(paired):
            o = o_list[i]
            n = n_list[i]
            rows.append(_row(service, pair_idx, "BUTTON", "BUTTON",
                             o.get("visible_text", "") or o.get("text", ""),
                             o.get("href", ""), o.get("hidden_text", ""), old_type,
                             n.get("visible_text", "") or n.get("text", ""),
                             n.get("href", ""), n.get("hidden_text", ""), new_type,
                             "OK"))
        # Surplus controls of the SAME kind are still functionally part of the
        # same widget chrome (e.g. the old site splits the autoplay toggle into
        # two buttons "Play" + "Stop" while the new site uses one combined
        # "Start stop"; or the dot counts differ by one). The user asked for
        # these to read OK regardless of wording or count, so we pair each
        # surplus against an empty counterpart and still mark OK rather than
        # MISSING/EXTRA.
        for o in o_list[paired:]:
            rows.append(_row(service, pair_idx, "BUTTON", "",
                             o.get("visible_text", "") or o.get("text", ""),
                             o.get("href", ""), o.get("hidden_text", ""), old_type,
                             "", "", "", new_type,
                             "OK"))
        for n in n_list[paired:]:
            rows.append(_row(service, pair_idx, "", "BUTTON",
                             "", "", "", old_type,
                             n.get("visible_text", "") or n.get("text", ""),
                             n.get("href", ""), n.get("hidden_text", ""), new_type,
                             "OK"))

    return rows


def _compare_buttons(service, pair_idx, old_btns, new_btns,
                     old_type, new_type) -> list:
    """Compare button lists by (visible_text, href) — both must match for OK.

    Carousel widget chrome (play/pause, dot-nav, arrows) is split out first
    and paired by sub-kind via _compare_carousel_controls, so renamed-but-
    equivalent controls ("Play reviews carousel" vs "Review 1") don't produce
    DIFFERS/EXTRA noise. The remaining real buttons go through normal
    content-based matching.
    """
    rows = []

    # --- Pre-pass: carousel controls, paired by kind and marked OK ---
    old_controls = [b for b in old_btns if _carousel_control_kind(b)]
    new_controls = [b for b in new_btns if _carousel_control_kind(b)]
    if old_controls or new_controls:
        rows.extend(_compare_carousel_controls(
            service, pair_idx, old_controls, new_controls, old_type, new_type))

    # Remove carousel chrome from further consideration
    old_btns = [b for b in old_btns if not _carousel_control_kind(b)]
    new_btns = [b for b in new_btns if not _carousel_control_kind(b)]

    # --- Pre-pass: social-media buttons, paired by network ---
    old_socials = [b for b in old_btns if _social_network(b)]
    new_socials = [b for b in new_btns if _social_network(b)]
    if old_socials or new_socials:
        rows.extend(_compare_social_buttons(
            service, pair_idx, old_socials, new_socials, old_type, new_type))

    # Real (non-chrome, non-social) buttons go through normal comparison
    old_btns = [b for b in old_btns if not _social_network(b)]
    new_btns = [b for b in new_btns if not _social_network(b)]

    # ----------------------------------------------------------------
    # Match remaining real buttons in ORDERED WAVES so the strongest
    # signal wins before weaker fallbacks consume a button. Processing one
    # old button at a time (full-match-then-partial) let an early old button
    # grab a loose visible-text match that a later old button would have
    # matched exactly, scrambling the pairings — especially on messy contact
    # sections where the new template mislabels phone/email buttons.
    #
    # Wave 1: exact match on (visible text AND href)         -> OK / EXPECTED
    # Wave 2: href matches (visible text changed)            -> OK / EXPECTED
    # Wave 3: visible text matches (href changed or empty)   -> DIFFERS
    # Leftover old -> MISSING; leftover new -> EXTRA/EXPECTED
    # ----------------------------------------------------------------
    old_remaining = list(old_btns)
    new_remaining = list(new_btns)
    matched_pairs = []  # (old_btn, new_btn, wave)

    def _take_match(o, predicate):
        for n in new_remaining:
            if predicate(o, n):
                return n
        return None

    # Wave 1: exact (visible, href)
    for o in list(old_remaining):
        o_vis = _normalize(o.get("visible_text") or o.get("text") or "")
        o_hrefn = _normalize_href(o.get("href", ""))
        n = _take_match(o, lambda o, n:
                        _normalize(n.get("visible_text") or n.get("text") or "") == o_vis and
                        _normalize_href(n.get("href", "")) == o_hrefn)
        if n is not None:
            old_remaining.remove(o)
            new_remaining.remove(n)
            matched_pairs.append((o, n, 1))

    # Wave 2: visible text matches (href may differ or be empty). Visible text
    # is the more trustworthy identity for contact buttons, because rebuilt
    # templates sometimes scramble hrefs (a phone-labeled button linking to the
    # email, an email button with no href). Matching the labels first keeps
    # email<->email and phone<->phone aligned.
    for o in list(old_remaining):
        o_vis = _normalize(o.get("visible_text") or o.get("text") or "")
        if not o_vis:
            continue
        n = _take_match(o, lambda o, n:
                        _normalize(n.get("visible_text") or n.get("text") or "") == o_vis)
        if n is not None:
            old_remaining.remove(o)
            new_remaining.remove(n)
            matched_pairs.append((o, n, 2))

    # Wave 3: href matches (non-empty), visible text differs
    for o in list(old_remaining):
        o_hrefn = _normalize_href(o.get("href", ""))
        if not o_hrefn:
            continue
        n = _take_match(o, lambda o, n:
                        _normalize_href(n.get("href", "")) == o_hrefn)
        if n is not None:
            old_remaining.remove(o)
            new_remaining.remove(n)
            matched_pairs.append((o, n, 3))

    # Emit matched pairs (preserve original old order for readability)
    order = {id(o): i for i, o in enumerate(old_btns)}
    matched_pairs.sort(key=lambda t: order.get(id(t[0]), 0))
    for o, n, wave in matched_pairs:
        o_vis = o.get("visible_text") or o.get("text") or ""
        o_href = o.get("href", "")
        o_hidden = o.get("hidden_text", "")
        n_vis = n.get("visible_text") or n.get("text") or ""
        n_href = n.get("href", "")
        n_hidden = n.get("hidden_text", "")

        if wave == 1:
            # Exact visible+href (after normalization). Identical raw href -> OK;
            # matched only after href normalization (trailing #, www., slash) -> EXPECTED.
            status = "OK" if (o_href or "").strip() == (n_href or "").strip() else "EXPECTED"
        elif wave == 2:
            # Same visible text, href may differ. If hrefs are equal (or differ
            # only cosmetically) it's OK; if they differ only by an EXPECTED
            # rebuild transformation (slug shortening, site version) -> EXPECTED;
            # otherwise a genuinely different href -> DIFFERS.
            if _normalize_href(o_href) == _normalize_href(n_href):
                status = "OK" if (o_href or "").strip() == (n_href or "").strip() else "EXPECTED"
            elif _hrefs_expected_equivalent(o_href, n_href):
                status = "EXPECTED"
            else:
                status = "DIFFERS"
        else:
            # Wave 3: same href, visible label changed (e.g. "Twitter page" ->
            # "Twitter/ X page"). The link is the identity, so a label-only
            # change reads OK.
            status = "OK"
        rows.append(_row(service, pair_idx, "BUTTON", "BUTTON",
                         o_vis, o_href, o_hidden, old_type,
                         n_vis, n_href, n_hidden, new_type,
                         status))

    # Leftover old buttons -> MISSING on new
    for o in old_remaining:
        rows.append(_row(service, pair_idx, "BUTTON", "",
                         o.get("visible_text", "") or o.get("text", ""),
                         o.get("href", ""), o.get("hidden_text", ""), old_type,
                         "", "", "", new_type,
                         "MISSING on new"))

    # Leftover new buttons -> EXTRA, unless they're template affordances -> EXPECTED
    for n in new_remaining:
        status = "EXPECTED" if _is_expected_affordance(n) else "EXTRA on new"
        rows.append(_row(service, pair_idx, "", "BUTTON",
                         "", "", "", old_type,
                         n.get("visible_text", "") or n.get("text", ""),
                         n.get("href", ""),
                         n.get("hidden_text", ""),
                         new_type,
                         status))

    return rows


def _compare_reviews(old_reviews, new_reviews, service="Reviews 1") -> list:
    """
    Compare reviews across the two sites. Each review is its own row.
    In a rebuild the same set of reviews should appear on both sides.
    Reviews are rendered as a carousel on both old and new sites.

    `service` is the section name written to the row's section-name columns;
    it defaults to "Reviews 1" so the REVIEW rows match the ordinal name used
    by the reviews section's header/control rows instead of a bare "reviews".
    """
    rows = []
    new_keys = {_review_key(r): r for r in new_reviews}
    seen_new = set()

    # Reviews are always rendered as a carousel widget
    review_type = "carousel"

    def _vis(rv):
        """Visible review text = reviewer name + quote (name is visible on the
        page; only the 'five star review by' prefix is sr-only)."""
        name = (rv.get("reviewer") or "").strip()
        quote = (rv.get("text") or "").strip()
        return f"{name} — {quote}" if name else quote

    for o in old_reviews:
        k = _review_key(o)
        n = new_keys.get(k)
        if n is not None:
            seen_new.add(k)
            rows.append(_row(service, 1, "REVIEW", "REVIEW",
                             _vis(o), "", o.get("reviewer_hidden", ""), review_type,
                             _vis(n), "", n.get("reviewer_hidden", ""), review_type,
                             "OK"))
        else:
            rows.append(_row(service, 1, "REVIEW", "",
                             _vis(o), "", o.get("reviewer_hidden", ""), review_type,
                             "", "", "", review_type,
                             "MISSING on new"))

    for n in new_reviews:
        if _review_key(n) not in seen_new:
            rows.append(_row(service, 1, "", "REVIEW",
                             "", "", "", review_type,
                             _vis(n), "", n.get("reviewer_hidden", ""), review_type,
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
    """Construct a single comparison row in the canonical shape.

    `service` may be either a single string (used for both sides) or a
    (old_section_name, new_section_name) tuple when the two sides carry
    different ordinal AI labels. The row exposes both names separately as
    `old_section_name` / `new_section_name`, and keeps `service` as the old
    name for backward compatibility.
    """
    if isinstance(service, (tuple, list)):
        old_name = service[0] if len(service) > 0 else ""
        new_name = service[1] if len(service) > 1 else ""
    else:
        old_name = new_name = service
    return {
        "service": old_name,
        "old_section_name": old_name,
        "new_section_name": new_name,
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


def _normalize_loose(s: str) -> str:
    """
    A looser normalization for fuzzy text equivalence. On top of _normalize
    (lowercase, collapse whitespace, strip space-before-punctuation), this:
      - drops trailing punctuation
      - collapses a trailing plural 's' on each word

    Used only as a FALLBACK after exact normalized matching fails, so that
    near-identical headings differing only by singular/plural or a trailing
    colon — e.g. "Location" vs "Locations", "Review" vs "Reviews:" — still
    pair instead of showing up as MISSING + EXTRA. Kept conservative (only
    a trailing 's') to avoid over-matching unrelated words.
    """
    base = _normalize(s)
    if not base:
        return ""
    base = base.rstrip(".,;:!?")
    words = []
    for w in base.split():
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        words.append(w)
    return " ".join(words)


def _text_equiv(a: str, b: str) -> bool:
    """
    True if two text strings are equivalent for comparison purposes.
    Tries exact normalized equality first, then a singular/plural- and
    trailing-punctuation-tolerant comparison.
    """
    if _normalize(a) == _normalize(b):
        return True
    return _normalize_loose(a) == _normalize_loose(b)


def _normalize(s: str) -> str:
    """
    Normalize text for comparison: lowercase, collapse whitespace, strip,
    and remove spaces sitting directly before punctuation.

    The punctuation-spacing step makes cosmetic typographic differences
    compare equal, e.g. "Mt. Pleasant , SC" (space before the comma) vs
    "Mt. Pleasant, SC" — common when an address is re-typed during a
    rebuild. Without this, such a pair would fail to match and show up as a
    spurious MISSING + EXTRA instead of a clean pairing.
    """
    if not s:
        return ""
    out = re.sub(r"\s+", " ", str(s).strip().lower())
    # Drop a space that sits immediately before , . ; : ! ?
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    return out


def _href_path_and_query(href: str) -> tuple:
    """Split a normalized href into (path, query) without scheme/host/fragment."""
    h = _normalize_href(href)
    # Strip fragment
    h = h.split("#", 1)[0]
    path, _, query = h.partition("?")
    return path, query


def _hrefs_expected_equivalent(old_href: str, new_href: str) -> bool:
    """
    True if two hrefs differ only in ways that are EXPECTED for a SpotHopper
    rebuild, namely:

      A) Slug shortening — the rebuild drops a long restaurant-name/location
         prefix from the path, keeping the meaningful tail. The new slug's
         last path segment equals the TAIL of the old slug's last segment:
            /charleston-saveurs-du-monde-cafe-food-menu  ->  /food-menu
            /charleston-...-location-westedge            ->  /location-westedge
         (and the reverse direction, in case old is the short one).

      B) Site-version difference — the URLs are identical except for a
         "vN" version token, e.g. spot-sample-74405-website-v1 vs -v2, or a
         callback_url that only differs by website-v1 vs website-v2.

    Used to mark such pairs EXPECTED rather than DIFFERS.
    """
    o = _normalize_href(old_href)
    n = _normalize_href(new_href)
    if not o or not n:
        return False
    if o == n:
        return False  # identical handled elsewhere (OK / cosmetic-EXPECTED)

    # --- Rule B: differ only by a website-vN version token, OR only by the
    # host carried inside a callback_url / redirect param ---
    # Replace any "website-vN" / "-vN-" / "vN" version markers with a constant
    # and see if the URLs become equal.
    def _strip_version(s: str) -> str:
        s = re.sub(r"website-v\d+", "website-vX", s)
        s = re.sub(r"(spot-sample-\d+-)v\d+", r"\1vX", s)
        s = re.sub(r"[-_/]v\d+\b", "/vX", s)
        return s

    # Many SpotHopper widgets embed the site's OWN base URL in a query
    # parameter (callback_url, return_url, redirect, etc.) so the widget can
    # send the visitor back. On a rebuild that parameter changes from the old
    # domain to the new domain — e.g.
    #   ...&callback_url=http://omakaselv.com/
    #   ...&callback_url=http://spot-sample-99698-website-v2.spotapps.co/
    # That's an EXPECTED base-URL swap, not a real link change. Neutralize the
    # value of those params to a constant before comparing.
    #
    # The old site also often serves pages through a CACHE host
    # (wcache.spotapps.co/<long-restaurant-slug>-<page>?domain=<old-domain>),
    # while the new site uses a clean slug (/about). The "?domain=" routing
    # param is not real query content, so neutralize it too; the slug tail
    # ("...-about" vs "/about") is then matched by Rule A below.
    def _strip_callback(s: str) -> str:
        s = re.sub(
            r"((?:callback_url|return_url|redirect_url|redirect|return_to|continue)=)[^&]*",
            r"\1CALLBACK",
            s,
            flags=re.I,
        )
        # Drop the cache-routing "domain=" param entirely (it only appears on
        # the old cache host and has no counterpart on the new clean URL), so
        # the two query strings compare equal. Handle it whether it's preceded
        # by ? / & or is the whole bare query string.
        s = re.sub(r"(?:[?&])domain=[^&]*", "", s, flags=re.I)
        s = re.sub(r"^domain=[^&]*&?", "", s, flags=re.I)
        return s

    def _canon(s: str) -> str:
        return _strip_callback(_strip_version(s))

    if _canon(o) == _canon(n):
        return True

    # --- Rule A: slug shortening on the path tail ---
    o_path, o_query = _href_path_and_query(old_href)
    n_path, n_query = _href_path_and_query(new_href)
    # Only treat as slug-shortening when the query strings agree (ignoring
    # the version + callback host handled above) — a different query is a
    # real change.
    if _canon(o_query) == _canon(n_query):
        o_seg = o_path.rstrip("/").split("/")[-1]
        n_seg = n_path.rstrip("/").split("/")[-1]
        if o_seg and n_seg and o_seg != n_seg:
            # The shorter segment must be a meaningful tail of the longer,
            # aligned on a hyphen boundary (so "food-menu" matches
            # "...-cafe-food-menu" but not "enu").
            longer, shorter = (o_seg, n_seg) if len(o_seg) >= len(n_seg) else (n_seg, o_seg)
            if longer.endswith(shorter) and len(shorter) >= 4:
                boundary = longer[: -len(shorter)]
                if boundary == "" or boundary.endswith("-"):
                    return True

    return False


def _normalize_href(href: str) -> str:
    """
    Normalize a URL so cosmetic differences don't trigger false DIFFERS:
      - lowercase
      - drop the scheme (http:// or https://)
      - drop a leading www.
      - drop a trailing slash
      - drop a trailing empty fragment ("#" with nothing after it)
    So "https://www.facebook.com/cafeluna" and "https://facebook.com/cafeluna/"
    both normalize to "facebook.com/cafeluna" and compare equal; and
    ".../saveurs-du-monde-cafe" vs ".../saveurs-du-monde-cafe#" compare equal.

    tel: and mailto: links are left essentially as-is (just lowercased and
    stripped) since they have no scheme/host to normalize.
    """
    if not href:
        return ""
    h = href.strip().lower()
    # Leave tel:/mailto: (and other non-http schemes) mostly alone
    if h.startswith(("tel:", "mailto:")):
        return h.rstrip("/")
    h = re.sub(r"^https?://", "", h)   # drop scheme
    h = re.sub(r"^www\.", "", h)        # drop leading www.
    h = re.sub(r"#$", "", h)            # drop a trailing empty fragment
    h = h.rstrip("/")                   # drop trailing slash
    return h