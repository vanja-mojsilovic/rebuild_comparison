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

Your job: read every signal in a section together — heading, subheading,
paragraph copy, button visible text, button hidden (screen-reader) text, and
button hrefs — then return ONE label that best describes the section's
PURPOSE on the page.

Allowed labels (return exactly one of these):
[{allowed}]

# What each label means

- "cover video"   full-bleed background video, usually at the top of the page
- "slideshow"     one slide visible at a time with auto-advance / dot nav
                  (hero slideshow). Each slide may arrive as a separate section.
- "carousel"      multi-item horizontal scroll/swipe (review row, image strip)
- "about us"      brand narrative: who we are, our story, restaurant intro,
                  cuisine description, hours/availability text. Often the
                  first text block under the cover and often combines a
                  heading naming the restaurant or its cuisine with
                  paragraphs describing the dining experience. The block's
                  buttons may link to /menu, /giftcards, /order, etc. — that
                  doesn't make it those things; the block's PURPOSE is the
                  brand intro.
- "parties"       private events, private dining, group bookings, hosting
                  a private party / celebration / corporate event. Signals
                  include: words "party"/"parties"/"private event(s)"/
                  "private dining"/"group reservations"/"celebrations"/
                  "host your event"/"book your event", and hrefs that
                  CONTAIN any of: /parties, /private-events, /private-event,
                  /private-dining, /private-party, /events-private,
                  /host-your-event, /book-your-event, /celebrate. The slug
                  may have trailing words ("/private-events-page",
                  "/private-dining-rooms") — match by substring, not
                  exact string. Paragraph copy like "Need space for a
                  private party? Host it here." is a clear parties signal.
- "events"        PUBLIC upcoming events the venue hosts (live music,
                  comedy nights, watch parties, holiday programming). Often
                  a calendar link or event listing. Distinguish from
                  "parties" by audience: parties = customer hosts their
                  own private gathering; events = the venue puts on
                  something open to the public.
- "catering"      off-site dining, drop-off catering, delivery for groups.
                  Hrefs containing /catering or /cater.
- "reservations"  table booking. Signals: "Reserve" / "Reservations" /
                  "Book a table" / "Make a reservation"; hrefs containing
                  opentable.com, resy.com, /reserve, /reservations,
                  yelp.com/reservations, tock.com, sevenrooms.com. A
                  section with a heading "Reservations" and a paragraph
                  "Book a table through OpenTable" is clearly reservations
                  even if no button is captured (OpenTable widgets render
                  inside iframes).
- "food menu"     food menu listing or CTA. Hrefs containing /menu,
                  /food-menu, qrco.de/...menu, or PDF menus.
- "drink menu"    drinks/cocktails/wine list specifically. /drinks,
                  /cocktails, /wine-list, /beverage-menu.
- "online order"  pickup / delivery / order online. /order, /pickup,
                  /delivery, toasttab.com/online, doordash.com,
                  ubereats.com, grubhub.com, chownow.com, postmates.com,
                  seamless.com, caviar.com.
- "specials"      daily / weekly specials, happy hour
- "jobs"          careers, hiring, "Apply Now", "Now Hiring". /jobs,
                  /careers, /hiring, /apply, /work-with-us.
- "gallery"       photo grid of the restaurant / food / atmosphere.
                  Multiple images is the strongest signal even when no
                  heading is present.
- "reviews"       customer reviews / testimonials / quoted feedback
- "locations"     multi-location list with addresses, "Visit us", "Find a
                  location near you", or a single address block on its own.
                  Distinguish from "contact" by purpose: locations = which
                  address(es) the business operates from; contact = how
                  to reach them.
- "map"           an embedded map (Google Maps, OpenStreetMap, Apple Maps)
                  as the section's primary content
- "newsletter"    email signup form. "Sign up for our newsletter",
                  "Subscribe", email input field.
- "contact"       contact information: phone numbers, email addresses,
                  hours of operation, social media links. Hrefs starting
                  with tel:, mailto:, or pointing at facebook.com /
                  instagram.com / twitter.com / x.com / google.com/maps.
- "gift cards"    gift card purchase CTA. /gift-cards, /giftcards,
                  toasttab.com/giftcards, /e-gift-cards.
- "footer"        bottom-of-page footer (copyright, powered-by lines)
- "header"        top-of-page navigation
- "hero"          hero banner without video — a large headline-and-CTA
                  block at the top of the page. Use this only when no
                  video is present and no more-specific label fits.
- "text+image"    a generic info block whose purpose doesn't clearly map
                  to any of the above
- "other"         truly doesn't fit anything above

# How to decide

1. Read the heading first — it's the section's title.
2. Read the paragraph copy — it often contains the giveaway phrase
   ("host a private party", "book a table", "sign up for our newsletter").
3. Read the button hrefs — URL slugs survive rebrands and tell you what
   the operator THINKS this button does. Match by SUBSTRING, not exact
   string: "/private-events-page" matches the /private-events pattern.
4. Read button visible text + hidden text as the screen-reader would —
   "Inquire Now" + hidden " about events" reads as "Inquire Now about
   events", strongly implying parties.
5. Integrate: pick the label whose PURPOSE matches the combined evidence.

# Conflict resolution

- Heading and href disagree: the href usually wins for service intent
  (heading "Private Events" + href /parties → "parties"). Exception: a
  block whose heading names the brand/cuisine and whose paragraphs
  describe the dining experience is "about us" even if the buttons link
  to /menu, /giftcards, /order — the buttons are CTAs out of the intro
  block, not the block's purpose.
- Paragraph mentions "party" in passing: only weight this as a parties
  signal when the paragraph is ABOUT hosting/booking a party, not when
  it's an incidental mention ("we throw a party for our regulars" is
  not a parties section).
- Multiple plausible labels: prefer the more specific one. A "Catering"
  section with a /catering href and copy about catering is "catering",
  not "text+image".

# Worked examples

Example A — heading rebrand:
  heading: "Private Events at Parma Trattoria"
  paragraph: "Parties feel natural in a room that is casual, energetic..."
  buttons: [{{"visible":"Book your Event","href":"https://tmt.spotapps.co/private-parties?..."}}, {{"visible":"Menus","href":"/parties"}}]
  → "parties"  (two /parties-pattern hrefs + "private events" wording +
                "Book your Event")

Example B — URL with trailing words:
  heading: "unforgettable celebrations"
  subheading: "group reservations and parties"
  paragraph: "Whether you're hosting an intimate gathering for 20 or a
              grand event for 200, a/stir offers versatile spaces..."
  buttons: [{{"visible":"Inquire Now","hidden":" about events","href":"/private-events-page"}}]
  → "parties"  ("/private-events-page" contains /private-events;
                "celebrations"/"hosting" copy; "Inquire Now about events" CTA)

Example C — paragraph signal only:
  heading: "downstairs"
  subheading: "for local art and drink scene..."
  paragraph: "Discover your next favorite local musician. Need space for
              a private party? Host it here."
  buttons: [{{"visible":"Calendar","href":"/calendar"}}]
  → "parties"  (paragraph EXPLICITLY says "Need space for a private
                party? Host it here." — that's the section telling the
                visitor it's a parties block, even though the button is
                a calendar link to programming. The host-a-party copy
                is the dominant signal here.)

  Note: this example is ambiguous and could also be argued as "events"
  (because of the calendar link) — when truly torn between two
  plausible labels, prefer the one the paragraph copy supports most
  directly with action verbs ("Host it here" → parties).

Example D — reservations with an iframe widget:
  heading: "Reservations"
  paragraph: "Book a table through Open Table:"
  buttons: []   (the OpenTable booking is in an iframe, not captured as a button)
  → "reservations"  (heading + paragraph alone are conclusive)

Example E — brand intro with diverse CTAs:
  heading: "a/stir - Northern Mediterranean Cuisine in Seattle"
  paragraph: "Our dining room at a/stir serves contemporary, gluten-free
              Northern Mediterranean fare..."
  buttons: [{{"visible":"See Our Menu","href":"https://qrco.de/astirmenu"}}, {{"visible":"Gift Cards","href":"https://www.toasttab.com/astir/giftcards"}}, {{"visible":"Order Take Out","href":"https://order.toasttab.com/online/astir"}}]
  → "about us"  (the block's PURPOSE is the brand intro; the three
                 buttons are exit CTAs to different services but the
                 block itself isn't any of those services)

# Sections to classify

{payload_json}

Respond ONLY with a JSON array (no markdown fences, no prose). Each
element:
  - id: the section id, matching the input id verbatim
  - label: one label from the allowed list above

Example response:
[{{"id": "old_0", "label": "cover video"}}, {{"id": "new_3", "label": "parties"}}]
"""


def _section_payload(section: dict) -> dict:
    """
    Reduce a scraper section dict to the fields the model needs. Keep it
    small to control token usage but include the key signals: headings,
    paragraphs, and button text/href/hidden.

    Paragraphs are truncated per-paragraph and capped at 6 paragraphs per
    section — enough to catch giveaway sentences like "Need space for a
    private party? Host it here." even when they appear several paragraphs
    deep into a long block.
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

    paragraphs = [p[:500] for p in section.get("paragraphs", []) if p and p.strip()][:6]

    return {
        "headings":   headings[:8],
        "paragraphs": paragraphs,
        "buttons":    buttons[:10],
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