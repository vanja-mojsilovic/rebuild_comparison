"""
Smart comparison of Old vs New restaurant websites.

Validation logic for the rebuild use case:
  - Old's H1 should match New's H2 (heading shift)
  - Old's H2 should match New's H3
  - Old's paragraphs match New's paragraphs
  - Buttons match on visible text AND href
  - Multiple sections of the same service are paired by content similarity,
    not by document order

Returns flat row data shaped for writing into the Google Sheet.
"""

import re
from typing import Optional


SERVICE_RULES = [
    ("food menu",    lambda s: bool(re.match(r"^(our menu|menu)$", s, re.I)) or
                               bool(re.search(r"see menu|full menu", s, re.I))),
    ("drink menu",   lambda s: bool(re.search(r"drinks", s, re.I))),
    ("online order", lambda s: bool(re.search(r"\border\b|pick up", s, re.I))),
    ("events",       lambda s: bool(re.search(r"events", s, re.I))),
    ("specials",     lambda s: bool(re.search(r"specials", s, re.I))),
    ("catering",     lambda s: bool(re.search(r"catering", s, re.I))),
    ("parties",      lambda s: bool(re.search(r"parties|private party|a party", s, re.I))),
    ("reservations", lambda s: bool(re.search(r"reserve|reservations|book a table", s, re.I))),
    ("jobs",         lambda s: bool(re.search(r"jobs|for a job", s, re.I))),
    ("about us",     lambda s: bool(re.search(r"about us|our story", s, re.I))),
    ("locations",    lambda s: bool(re.search(r"locations?|visit us", s, re.I))),
    ("carousel",     lambda s: bool(re.search(r"carousel", s, re.I))),
]


# Element types compared, with the heading shift baked in:
# Each tuple is (old_field, new_field, label_in_report).
COMPARISON_FIELDS = [
    ("h1", "h2", "H1→H2"),  # Old H1 should appear as New H2
    ("h2", "h3", "H2→H3"),  # Old H2 should appear as New H3
    ("h3", "h3", "H3"),     # H3 stays where it is (rebuild without H1 case)
    ("paragraphs", "paragraphs", "P"),
    ("list_items", "list_items", "LI"),
]


# ------------------------------------------------------------
# Classification
# ------------------------------------------------------------
def classify_service(section: dict) -> str:
    """Classify a section using its buttons + unified headings (h1+h2+h3)."""
    button_texts = [b["text"] for b in section.get("buttons", []) if b.get("text")]
    sources = button_texts + section.get("headings", [])
    for source in sources:
        for name, test in SERVICE_RULES:
            if test(source):
                return name
    return "other"


def group_by_service(sections: list) -> dict:
    """Group raw scraper sections by classified service."""
    out: dict = {}
    for s in sections:
        svc = classify_service(s)
        out.setdefault(svc, []).append(s)
    return out


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
# Build the flat row data for the sheet
# ------------------------------------------------------------
def build_validated_rows(old_data: dict, new_data: dict) -> list:
    """
    Produce a flat list of comparison rows for the report tab.

    Each row dict has:
        service           — classified service name
        element           — H1, H2, H3, P, LI, BUTTON, REVIEW
        comparison_field  — "H1→H2", "H2→H3", "P", "LI", "BUTTON", "REVIEW"
        old_text, old_href, old_hidden
        new_text, new_href, new_hidden
        match             — "OK" / "MISSING on new" / "EXTRA on new" / "DIFFERS"
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


def _compare_section_pair(service, pair_idx, old_sec, new_sec) -> list:
    """Walk a paired (Old, New) section pair and emit comparison rows."""
    out = []

    # If one side is missing the whole section, emit one row per element
    # that exists on the present side, marking the other side empty.
    if old_sec is None or new_sec is None:
        present = old_sec or new_sec
        side_missing = "old" if old_sec is None else "new"
        for (o_field, n_field, label) in COMPARISON_FIELDS:
            field = o_field if old_sec is None else n_field  # use whichever side exists
            for text in present.get(field, []):
                if side_missing == "old":
                    out.append(_row(service, pair_idx, label,
                                    "", "", "",
                                    text, "", "",
                                    "EXTRA on new"))
                else:
                    out.append(_row(service, pair_idx, label,
                                    text, "", "",
                                    "", "", "",
                                    "MISSING on new"))
        for btn in present.get("buttons", []):
            v = btn.get("visible_text", "") or btn.get("text", "")
            h = btn.get("hidden_text", "")
            href = btn.get("href", "")
            if side_missing == "old":
                out.append(_row(service, pair_idx, "BUTTON",
                                "", "", "",
                                v, href, h,
                                "EXTRA on new"))
            else:
                out.append(_row(service, pair_idx, "BUTTON",
                                v, href, h,
                                "", "", "",
                                "MISSING on new"))
        return out

    # Both sections exist — compare per element type with heading shift
    for (o_field, n_field, label) in COMPARISON_FIELDS:
        old_texts = old_sec.get(o_field, [])
        new_texts = new_sec.get(n_field, [])
        out.extend(_compare_text_lists(service, pair_idx, label, old_texts, new_texts))

    # Buttons compared with text AND href
    out.extend(_compare_buttons(service, pair_idx,
                                old_sec.get("buttons", []),
                                new_sec.get("buttons", [])))

    return out


def _compare_text_lists(service, pair_idx, label, old_texts, new_texts) -> list:
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
            rows.append(_row(service, pair_idx, label,
                             o_text, "", "",
                             matched_n, "", "",
                             "OK"))
        else:
            # Old has text that doesn't appear on New in any form
            # If New has at least one unmatched item, pair them visually with DIFFERS
            if new_remaining:
                n_text = new_remaining.pop(0)
                rows.append(_row(service, pair_idx, label,
                                 o_text, "", "",
                                 n_text, "", "",
                                 "DIFFERS"))
            else:
                rows.append(_row(service, pair_idx, label,
                                 o_text, "", "",
                                 "", "", "",
                                 "MISSING on new"))

    for n_text in new_remaining:
        rows.append(_row(service, pair_idx, label,
                         "", "", "",
                         n_text, "", "",
                         "EXTRA on new"))

    return rows


def _compare_buttons(service, pair_idx, old_btns, new_btns) -> list:
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
            rows.append(_row(service, pair_idx, "BUTTON",
                             o_visible, o_href, o_hidden,
                             matched.get("visible_text", "") or matched.get("text", ""),
                             matched.get("href", ""),
                             matched.get("hidden_text", ""),
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
                rows.append(_row(service, pair_idx, "BUTTON",
                                 o_visible, o_href, o_hidden,
                                 partial.get("visible_text", "") or partial.get("text", ""),
                                 partial.get("href", ""),
                                 partial.get("hidden_text", ""),
                                 "DIFFERS"))
            elif new_remaining:
                n = new_remaining.pop(0)
                rows.append(_row(service, pair_idx, "BUTTON",
                                 o_visible, o_href, o_hidden,
                                 n.get("visible_text", "") or n.get("text", ""),
                                 n.get("href", ""),
                                 n.get("hidden_text", ""),
                                 "DIFFERS"))
            else:
                rows.append(_row(service, pair_idx, "BUTTON",
                                 o_visible, o_href, o_hidden,
                                 "", "", "",
                                 "MISSING on new"))

    for n in new_remaining:
        rows.append(_row(service, pair_idx, "BUTTON",
                         "", "", "",
                         n.get("visible_text", "") or n.get("text", ""),
                         n.get("href", ""),
                         n.get("hidden_text", ""),
                         "EXTRA on new"))

    return rows


def _compare_reviews(old_reviews, new_reviews) -> list:
    """
    Compare reviews across the two sites. Each review is its own row.
    In a rebuild the same set of reviews should appear on both sides.
    """
    rows = []
    new_keys = {_review_key(r): r for r in new_reviews}
    seen_new = set()

    for o in old_reviews:
        k = _review_key(o)
        n = new_keys.get(k)
        if n is not None:
            seen_new.add(k)
            rows.append(_row("reviews", 1, "REVIEW",
                             o.get("text", ""), "", o.get("reviewer", ""),
                             n.get("text", ""), "", n.get("reviewer", ""),
                             "OK"))
        else:
            rows.append(_row("reviews", 1, "REVIEW",
                             o.get("text", ""), "", o.get("reviewer", ""),
                             "", "", "",
                             "MISSING on new"))

    for n in new_reviews:
        if _review_key(n) not in seen_new:
            rows.append(_row("reviews", 1, "REVIEW",
                             "", "", "",
                             n.get("text", ""), "", n.get("reviewer", ""),
                             "EXTRA on new"))

    return rows


def _review_key(review: dict) -> str:
    """Normalize a review for set membership."""
    text = review.get("text") or ""
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _row(service, pair_idx, element,
         old_text, old_href, old_hidden,
         new_text, new_href, new_hidden,
         match) -> dict:
    """Construct a single comparison row in the canonical shape."""
    return {
        "service": service,
        "section_pair": pair_idx,
        "element": element,
        "old_text": old_text,
        "old_href": old_href,
        "old_hidden": old_hidden,
        "new_text": new_text,
        "new_href": new_href,
        "new_hidden": new_hidden,
        "match": match,
    }


def _normalize(s: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace, strip."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())