"""
Build a simple side-by-side view of Old and New sections.
No comparison, no validation — just classify each section and list
its content. Validation will be added later.
"""

import re


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


def classify_service(section: dict) -> str:
    """Classify a section using its buttons + unified headings (h1+h2+h3)."""
    button_texts = [b["text"] for b in section.get("buttons", []) if b.get("text")]
    sources = button_texts + section.get("headings", [])
    for source in sources:
        for name, test in SERVICE_RULES:
            if test(source):
                return name
    return "other"


def build_sections_view(old_data: dict, new_data: dict) -> dict:
    """
    Return data shaped for the side-by-side report:
        {
          "old_sections": [ {service, rows: [{tag, text, href}]}, ... ],
          "new_sections": [...same shape...],
          "identity": { old: {...}, new: {...} },
          "restaurant": { old: "name", new: "name" }
        }
    Sections stay in document order on each side. No pairing or comparison.
    """
    return {
        "old_sections": _build_side(old_data["sections"]),
        "new_sections": _build_side(new_data["sections"]),
        "identity": {
            "old": old_data["identity"],
            "new": new_data["identity"],
        },
        "restaurant": {
            "old": old_data.get("restaurant_name", ""),
            "new": new_data.get("restaurant_name", ""),
        },
    }


def _build_side(sections: list) -> list:
    """Turn raw scraper sections into the renderable side-list."""
    out = []
    for s in sections:
        rows = []
        for text in s.get("h1", []):
            rows.append({"tag": "H1", "text": text, "href": ""})
        for text in s.get("h2", []):
            rows.append({"tag": "H2", "text": text, "href": ""})
        for text in s.get("h3", []):
            rows.append({"tag": "H3", "text": text, "href": ""})
        for text in s.get("paragraphs", []):
            rows.append({"tag": "P", "text": text, "href": ""})
        for text in s.get("list_items", []):
            rows.append({"tag": "LI", "text": text, "href": ""})
        for btn in s.get("buttons", []):
            rows.append({
                "tag": "BUTTON",
                "text": btn.get("text", ""),
                "href": btn.get("href", ""),
            })

        out.append({
            "service": classify_service(s),
            "rows": rows,
        })
    return out