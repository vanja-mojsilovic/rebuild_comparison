"""
Optional AI-based section-type classification for the 'sections' tab.

Sends each restaurant's sections (both old- and new-site) to the OpenAI API
in a single batched call and returns a short type label per section. The
output is written alongside the rule-based section name so reviewers can
spot disagreements between the regex classifier and the model.

Behavior is defensive:
  - If OPENAI_API_KEY (or OPEN_AI_KEY) is not set, classification is skipped
    and the AI columns in the report stay empty. The workflow does not fail.
  - If the API call errors out, the same fallback applies.
  - If the model's response can't be parsed as JSON, empty labels are returned.

Model choice
------------
OPENAI_MODEL is the single knob to turn when upgrading. The token-limit
parameter name differs between legacy (gpt-4o, gpt-4.1, gpt-3.x) and
gpt-5.x / o-series models, so _token_limit_kwarg picks the right one.
"""

import json
import os
import sys
import time
from typing import Optional


# Single knob — change here to upgrade everywhere
OPENAI_MODEL = "gpt-5.5"


# GPT-5.x and o-series models use `max_completion_tokens` instead of the
# legacy `max_tokens`. Keep this helper so a model change only requires
# updating OPENAI_MODEL above.
def _token_limit_kwarg(n: int) -> dict:
    """Return the correct token-limit kwarg for the active model."""
    legacy = OPENAI_MODEL.startswith(("gpt-3", "gpt-4o", "gpt-4.1", "gpt-4-"))
    return {"max_tokens": n} if legacy else {"max_completion_tokens": n}


def _strip_fences(text: str) -> str:
    """Strip markdown code fences from model output."""
    return (
        (text or "").strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )


def _get_api_key() -> Optional[str]:
    """
    Read the API key from environment. Accepts both names so the GitHub
    secret can be called either OPENAI_API_KEY (the upstream-standard name)
    or OPEN_AI_KEY (what the reference project uses).
    """
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("OPEN_AI_KEY")


def _get_client():
    """
    Lazily build the OpenAI client. Returns None if the SDK isn't installed
    or no key is configured — callers should treat None as "skip AI step".
    """
    key = _get_api_key()
    if not key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("[ai_classifier] openai SDK not installed; skipping AI classification.",
              file=sys.stderr)
        return None
    return OpenAI(api_key=key)


# Allowed section-type labels. We constrain the model to this set so the
# new column has consistent, comparable values rather than free-form text.
ALLOWED_LABELS = [
    "cover video",
    "slideshow",
    "carousel",
    "about us",
    "parties",
    "events",
    "catering",
    "reservations",
    "food menu",
    "drink menu",
    "online order",
    "specials",
    "jobs",
    "gallery",
    "reviews",
    "locations",
    "map",
    "newsletter",
    "contact",
    "gift cards",
    "footer",
    "header",
    "hero",
    "text+image",
    "other",
]


def _build_prompt(restaurant: str, payload_json: str) -> str:
    """Build the prompt sent to the OpenAI API."""
    allowed = ", ".join(f'"{lbl}"' for lbl in ALLOWED_LABELS)
    return f"""You are classifying sections of a restaurant website for "{restaurant}".

For each section described below, decide what kind of section it is and return
ONE short label from this exact allowed list:

[{allowed}]

Use these definitions:
- "cover video"   full-bleed background video at the top of the page
- "slideshow"     one slide at a time, auto-advance / dot nav (UIKit slideshow,
                  hero slideshows). Each slide may arrive as a separate section.
- "carousel"      multi-item horizontal scroll/swipe (review row, image strip)
- "about us"      "Our Story", brand narrative, history
- "parties"       private events, private dining, group bookings
- "events"        public upcoming events (live music, comedy nights)
- "catering"      off-site dining, drop-off catering
- "reservations"  table booking ("Reserve", "Book a table")
- "food menu"     menu CTA or menu listing
- "drink menu"    drinks menu CTA or listing
- "online order"  pickup / delivery / order online
- "specials"      daily / weekly specials
- "jobs"          careers, hiring
- "gallery"       photo grid of the restaurant / food
- "reviews"       customer reviews / testimonials
- "locations"     "Visit us", multi-location list, single address block
- "map"           an embedded map (Google / OpenStreetMap)
- "newsletter"    email signup form
- "contact"       contact info, phone, email, hours
- "gift cards"    gift card purchase CTA
- "footer"        bottom-of-page footer
- "header"        top-of-page navigation
- "hero"          hero banner without video
- "text+image"    a generic info block with no specific service intent

Each section is described by its heading text, paragraph text, and the visible
plus hidden text of any buttons or links inside it, ALONG WITH each button's
href attribute.

IMPORTANT — use the button hrefs as a strong signal. URL slugs survive rebrands
even when the visible labels change. Examples:
  - /parties, /private-events, /private-dining     → "parties"
  - /catering, /catering-inquiry                    → "catering"
  - /reserve, /reservations, opentable.com/...      → "reservations"
  - /menu, qrco.de/...menu                          → "food menu"
  - /drinks, /cocktails, /wine-list                 → "drink menu"
  - /order, /pickup, /delivery, toasttab.com/online → "online order"
  - /events, /calendar, /live-music                 → "events"
  - /jobs, /careers, /hiring                        → "jobs"
  - /about, /our-story                              → "about us"
  - /gift-cards, /giftcards, toasttab.com/giftcards → "gift cards"
  - /contact, mailto:, tel:                         → "contact"
  - facebook.com, instagram.com, google.com/maps    → "contact"

When the heading and button hrefs disagree, the hrefs usually win — a heading
"Private Events" with a button to /parties is the "parties" section, and a
heading "a/stir Restaurant" with buttons to /menu, /giftcards, /order is an
"about us" section even though the buttons cover several services (because
the BLOCK's primary purpose is the brand intro — see the heading).

Pick the label that best describes the section's PURPOSE, not its styling.

Sections:
{payload_json}

Respond ONLY with a JSON array (no markdown fences). Each element:
- id: the section id (matching the input id verbatim)
- label: one label from the allowed list above

Example: [{{"id": "old_0", "label": "cover video"}}, {{"id": "new_3", "label": "parties"}}]
"""


def _section_payload(section: dict) -> dict:
    """
    Reduce a scraper section dict to the fields the model needs. Keep it
    small to control token usage but include the key signals: headings,
    paragraphs, and button text/href/hidden.
    """
    headings = []
    for level in ("h1", "h2", "h3"):
        headings.extend(section.get(level, []))

    buttons = []
    for b in section.get("buttons", []):
        buttons.append({
            "visible": (b.get("visible_text") or b.get("text") or "")[:200],
            "hidden":  (b.get("hidden_text") or "")[:200],
            "href":    (b.get("href") or "")[:300],
        })

    paragraphs = [p[:400] for p in section.get("paragraphs", [])][:3]

    return {
        "headings":   headings[:6],
        "paragraphs": paragraphs,
        "buttons":    buttons[:8],
        "html_type":  section.get("html_element_type", ""),
    }


def classify_sections_pair(restaurant: str, old_sections: list, new_sections: list,
                           client=None) -> dict:
    """
    Classify both sides' sections in a SINGLE OpenAI call.

    Returns a dict mapping section ids to label strings:
        {"old_0": "cover video", "old_1": "parties", ..., "new_2": "gallery", ...}

    If anything goes wrong (no API key, SDK missing, network error, parse
    failure) returns an empty dict so the caller can fall back to empty
    cells in the sheet.
    """
    if client is None:
        client = _get_client()
    if client is None:
        return {}

    payload = []
    for i, sec in enumerate(old_sections):
        payload.append({"id": f"old_{i}", **_section_payload(sec)})
    for i, sec in enumerate(new_sections):
        payload.append({"id": f"new_{i}", **_section_payload(sec)})

    if not payload:
        return {}

    prompt = _build_prompt(restaurant, json.dumps(payload, indent=2))

    try:
        # Brief pause is courteous to the rate limiter when many runs queue up
        time.sleep(0.5)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            **_token_limit_kwarg(2000),
        )
    except Exception as e:
        print(f"[ai_classifier] OpenAI call failed: {e}", file=sys.stderr)
        return {}

    raw = response.choices[0].message.content or ""
    try:
        parsed = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        print(f"[ai_classifier] could not parse model JSON: {e}", file=sys.stderr)
        return {}

    if not isinstance(parsed, list):
        return {}

    allowed_set = set(ALLOWED_LABELS)
    out: dict = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sid = item.get("id")
        lbl = item.get("label")
        if not isinstance(sid, str) or not isinstance(lbl, str):
            continue
        lbl = lbl.strip().lower()
        # Be lenient: snap to allowed labels if the model returned a close variant
        if lbl in allowed_set:
            out[sid] = lbl
        else:
            # Try simple normalization, e.g. underscore vs space
            normalized = lbl.replace("_", " ").replace("-", " ")
            if normalized in allowed_set:
                out[sid] = normalized
            else:
                out[sid] = "other"
    return out