"""
Content validation via OpenAI — typo / strange-content checks for sections,
and rule-based checks for reviews.

Two consumers:
  * the 'custom' tab — validates the NEW site's home-page sections + reviews
    (the same scraped data the comparison uses).
  * the 'content' tab — validates sections scraped from the NEW site's other
    relevant pages (/about, /cater, /parties, /reserve, /locations).

Both call the same engine here; only the input sections differ.

Conventions match ai_classifier.py:
  * OPENAI_MODEL is the single knob (gpt-5.5).
  * Reasoning models (gpt-5.x / o-series) need reasoning_effort='low' and a
    generous token budget, or the JSON answer comes back empty.
  * The API key is read from OPENAI_API_KEY, then OPEN_AI_KEY.
  * Everything is defensive: missing key, SDK, or unparseable JSON → empty
    results and the workflow keeps going.
"""

import json
import os
import sys
import time
from typing import Optional


# Single knob — change here to upgrade everywhere.
OPENAI_MODEL = "gpt-5.5"


def _token_limit_kwarg(n: int) -> dict:
    """Return the correct token-limit kwarg for the active model."""
    legacy = OPENAI_MODEL.startswith(("gpt-3", "gpt-4o", "gpt-4.1", "gpt-4-"))
    return {"max_tokens": n} if legacy else {"max_completion_tokens": n}


def _gpt5_extra_kwargs() -> dict:
    """reasoning_effort='low' for reasoning models; empty for legacy chat models."""
    if OPENAI_MODEL.startswith(("gpt-5", "o1", "o3", "o4")):
        return {"reasoning_effort": "low"}
    return {}


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
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("OPEN_AI_KEY")


def _get_client():
    """Lazily build the OpenAI client. None means 'skip the AI step'."""
    key = _get_api_key()
    if not key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        print("[content_validator] openai SDK not installed; skipping validation.",
              file=sys.stderr)
        return None
    return OpenAI(api_key=key)


def _call_openai(client, prompt: str, budget: int = 16000) -> str:
    """Make one chat completion call; return raw content ('' on any failure)."""
    try:
        time.sleep(0.5)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            **_token_limit_kwarg(budget),
            **_gpt5_extra_kwargs(),
        )
    except Exception as e:
        print(f"[content_validator] OpenAI call failed: {e}", file=sys.stderr)
        return ""
    try:
        choice = response.choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        raw = choice.message.content or ""
        if not raw.strip():
            print(f"[content_validator] empty response (finish_reason={finish_reason!r})",
                  file=sys.stderr)
        return raw
    except Exception as e:
        print(f"[content_validator] could not read response: {e}", file=sys.stderr)
        return ""


# ──────────────────────────────────────────────────────────────────────────
# Shared suppression rules (kept verbatim from the reference project so the
# model's behavior matches what the team already tuned).
# ──────────────────────────────────────────────────────────────────────────

_ENTITY_SUPPRESSION_RULE = (
    "- DO NOT flag HTML entities such as &apos; &quot; &amp; &#39; &#34; "
    "or similar encoded apostrophes, quotes, or ampersands. These are "
    "normal and expected — treat them as if they render correctly "
    "(e.g. treat \"I&apos;m\" as \"I'm\" and \"Smith &amp; Co\" as "
    "\"Smith & Co\"). Do NOT mention them in any explanation, not even "
    "as a borderline note. The ONLY encoding issue to flag is the U+FFFD "
    "replacement character (\ufffd)."
)

_SOCIAL_LABEL_SUPPRESSION_RULE = (
    "- DO NOT flag social media link labels that combine platform names, "
    "such as \"Twitter/ X page\", \"Twitter / X\", \"X (Twitter)\", or "
    "\"X (formerly Twitter) page\". These read acceptably to a screen "
    "reader — treat them as CORRECT and do NOT suggest rewordings."
)

_REVIEWER_NAME_RULE = (
    "- The REVIEWER'S OWN DISPLAYED NAME (the \"reviewer\" field in the data) "
    "must have the last name abbreviated to a single initial. The accepted "
    "format is a first name followed by a last-name initial, e.g. \"Sarah M.\" "
    "or \"John D.\". Flag a POTENTIAL ISSUE if the reviewer field contains a "
    "FULL last name (e.g. \"Sarah Miller\" should be \"Sarah M.\", "
    "\"John Davis\" should be \"John D.\"). A bare first name with no last name "
    "at all (e.g. \"Sarah\") is acceptable — do NOT flag that."
)


# ──────────────────────────────────────────────────────────────────────────
# Prompts (JSON-output API variants of the reference prompts)
# ──────────────────────────────────────────────────────────────────────────

def _build_reviews_prompt(restaurant_name: str, reviews_json: str) -> str:
    return f"""Check each review of the restaurant "{restaurant_name}" according to these rules:

Rules:
- The review must not have a specific price/pricetag (e.g., $20)
- It must not mention any other business BY NAME. Generic references like
  "the other locations" or "another spot" are FINE. Only flag NAMED competitors.
- It must not have the first name, last name, or nickname of a worker/any person
  (this rule is about people mentioned INSIDE the review text)
{_REVIEWER_NAME_RULE}
- No negative connotation; the review should be positive
- No swearing or obscene words
- Must not say that it is expensive
- Must not have U+FFFD character
- If the reviewer names are Tina, Carlos or Cris, or the text contains a sample
  like 'This is n-th example', flag it as a potential issue

DO NOT FLAG (these are normal and expected):
{_ENTITY_SUPPRESSION_RULE}

Analyze each review individually. Even when a review passes all rules, provide
a short explanation of WHY it passes and note anything borderline worth a
human's attention.

Reviews data:
{reviews_json}

Respond ONLY with a JSON array (no markdown fences) where each element has:
- id: the review id (matching the input)
- issue: "OK: <short explanation and any borderline notes>" or
         "POTENTIAL ISSUE: <which rule, the triggering text, and nuance>"

Example: [{{"id": "r0", "issue": "OK: positive tone, reviewer name 'Sarah M.' correctly abbreviated, no flagged content."}}, {{"id": "r1", "issue": "POTENTIAL ISSUE: reviewer name 'John Davis' shows a full last name — should be 'John D.'"}}]"""


def _build_sections_prompt(restaurant_name: str, sections_json: str) -> str:
    return f"""You are reviewing the website of the restaurant "{restaurant_name}".
For each section you have TWO responsibilities of EQUAL weight:

  (A) SPELLING & GRAMMAR — every word in `heading` and `body_text` must be
      a correctly-spelled, correctly-used English word.
  (B) ACCESSIBILITY — interactive elements (buttons and links) must read
      sensibly to a screen reader.

═══════════════════════════════════════════════════════════════════════════
(A) SPELLING & GRAMMAR — RUN THIS CHECK FIRST, ON EVERY SECTION
═══════════════════════════════════════════════════════════════════════════

Procedure: read the `heading` field word-by-word, then the `body_text` field
word-by-word. Treat each word as suspect until you confirm it is a real
English word used correctly. CAPITALIZED words in headings are NOT exempt —
restaurants do not intentionally misspell common English words in titles
like "Tialored Catering Options" or "Welcom to Our Menu".

Flag a POTENTIAL ISSUE if you find any of these typo patterns:

1. LETTER TRANSPOSITION (two adjacent letters swapped):
     "Tialored" → "Tailored"; "freind" → "friend"; "recieve" → "receive";
     "thier" → "their"; "wich" → "which"
2. MISSING / EXTRA LETTERS:
     "untill" → "until"; "definately" → "definitely"; "occured" → "occurred";
     "seperate" → "separate"; "accomodate" → "accommodate"; "tomorow" → "tomorrow"
3. WRONG WORD / HOMOPHONE (a real word, but the wrong one):
     "bellow" → "below"; "their/there/they're"; "your/you're"; "its/it's";
     "to/too/two"; "then/than"; "lose/loose"
4. DOUBLED WORDS: "the the", "is is", "and and", "we we"
5. MISSING SPACES: "clickhere", "menubelow", verb "signup" (should be "sign up")
6. U+FFFD replacement character (\ufffd) anywhere in the text.

When you find a typo, quote the exact word as it appears and state the correction.

Also flag misspelled BRAND names: DoorDash, Uber Eats, Grubhub, Postmates,
Toast, ChowNow, Seamless, Caviar, OpenTable, Resy, Yelp, Facebook, Instagram.

Restaurant name mismatch (flag only if clearly wrong, not minor variations).

═══════════════════════════════════════════════════════════════════════════
(B) ACCESSIBILITY — INTERACTIVE ELEMENTS
═══════════════════════════════════════════════════════════════════════════

KEY CONCEPT — VISIBLE + HIDDEN TEXT:
A button/link can have visible text AND visually-hidden (sr-only) text.
The `screen_reader_reads` field already reflects DOM order — treat it as
the actual screen reader output. Do NOT infer order from `visible`/`hidden`.

Each interactive element falls into one of:
1. CORRECT — `screen_reader_reads` is a clear, sensible action label.
2. INSUFFICIENT — visible text is a content-free phrase ("Click Here",
   "Learn More", "Submit", "Go", bare arrow) AND no hidden text clarifies it.
   Do NOT flag labels that name a real action: "Reserve Now", "Book Now",
   "Order Online", "Catering", "Apply Now", "Get Directions", etc.
   EXCEPTION: skip this check for service_type "map", "newsletter", "contact".
3. REDUNDANT — hidden text repeats words already in the visible text.
   EXCEPTION: skip for "contact" or "newsletter".

Also flag unnatural combined readings (wrong preposition, etc.).

═══════════════════════════════════════════════════════════════════════════
DO NOT FLAG
═══════════════════════════════════════════════════════════════════════════
- Missing headings
- Hidden text adding new concepts
- Repeated CTAs across a section
- Minor restaurant name variations
- Apostrophes, ampersands, trailing dots
- <br> used as a word separator
- Stylistic choices (Title Case, ALL CAPS) when the words themselves are spelled correctly
{_ENTITY_SUPPRESSION_RULE}
{_SOCIAL_LABEL_SUPPRESSION_RULE}

═══════════════════════════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════════════════════════

Sections data:
{sections_json}

Respond ONLY with a JSON array (no markdown fences) where each element has:
- id: the section id (matching the input)
- issue: "OK: <short explanation; note anything borderline>" or
         "POTENTIAL ISSUE: <category, field, exact quoted text, correction or nuance>"

If a section has MULTIPLE problems, list all of them in the same `issue`
string, separated by " | ".

Examples:
[
  {{"id": "s0", "issue": "OK: heading and body text spelled correctly; button reads naturally."}},
  {{"id": "s1", "issue": "POTENTIAL ISSUE: TYPO in heading — 'Tialored' should be 'Tailored'."}},
  {{"id": "s2", "issue": "POTENTIAL ISSUE: TYPO in body_text — 'bellow' should be 'below' in 'clicking the button bellow'."}}
]"""


# ──────────────────────────────────────────────────────────────────────────
# Mapping: my scraper's section dict → the prompt's expected shape
# ──────────────────────────────────────────────────────────────────────────

def _section_to_payload(section: dict, vid: str) -> dict:
    """
    Convert one scraper section into the {id, service_type, heading, body_text,
    interactive[]} shape the sections prompt expects.

    heading   = all h1–h4 joined with " | "
    body_text = all paragraphs + list items joined with " | "
    interactive = buttons mapped to {visible, hidden, screen_reader_reads},
                  where screen_reader_reads is visible+hidden in DOM order
                  (my scraper stores them separately; hidden follows visible).
    """
    heading_parts = []
    for h in ("h1", "h2", "h3", "h4"):
        heading_parts.extend(section.get(h, []) or [])
    # Drop template boilerplate headings that aren't author-written content and
    # would draw spurious "awkward punctuation" / wording flags. The review
    # SOURCE label ("Review by - Google", "Reviews by - Yelp") is SpotHopper's
    # standard markup, identical on every reviews section — not a typo or a
    # content choice to validate.
    heading_parts = [p for p in heading_parts if not _is_template_boilerplate(p)]
    heading_text = " | ".join(p for p in heading_parts if p)

    body_parts = list(section.get("paragraphs", []) or [])
    body_parts.extend(section.get("list_items", []) or [])
    body_text = " | ".join(p for p in body_parts if p)

    interactive = []
    for b in section.get("buttons", []) or []:
        visible = (b.get("visible_text") or b.get("text") or "").strip()
        hidden = (b.get("hidden_text") or "").strip()
        if not (visible or hidden):
            continue
        # Skip decorative media controls (play/pause for a background video,
        # slideshow, or reviews carousel). Their sr-only text intentionally
        # restates the control state/label, which the validator would
        # otherwise flag as REDUNDANT — a false positive, since these are
        # accessibility affordances rather than content links. Detect them by
        # the characteristic control phrasing in the visible OR hidden text.
        combined = f"{visible} {hidden}".lower()
        if _is_decorative_media_control(combined, section.get("ai_service") or ""):
            continue
        # Prefer the scraper's DOM-ordered reading (what a screen reader
        # actually announces) so the validator judges the real order. Fall
        # back to visible+hidden only when the scraper didn't supply it.
        sr = (b.get("screen_reader_text") or "").strip() or (visible + " " + hidden).strip()
        interactive.append({
            "visible": visible,
            "hidden": hidden,
            "screen_reader_reads": sr,
        })

    return {
        "id": vid,
        "service_type": section.get("ai_service") or "other",
        "heading": heading_text,
        "body_text": body_text,
        "interactive": interactive,
    }


# Phrases that mark a button as a decorative / functional widget control
# (background-video, slideshow, carousel, gallery, or map) rather than a
# content link. Validation skips these so their intentionally-restated sr-only
# text isn't flagged as REDUNDANT and their terse glyphs aren't flagged as
# unclear labels — they're widget affordances, not content CTAs.
_MEDIA_CONTROL_PHRASES = (
    "decorative video",
    "video is currently",
    "play the video", "pause the video",
    "slideshow is currently", "play the slideshow", "pause the slideshow",
    "carousel is currently", "play the carousel", "pause the carousel",
    "start stop", "reviews carousel",
    "previous slide", "next slide",
    "previous review", "next review",
    "previous image", "next image",
    "previous photo", "next photo",
    "slide content",
    # Map controls
    "zoom in", "zoom out", "reset zoom", "zoom map",
    "rotate the view", "rotate clockwise", "rotate counterclockwise",
)

# Bare glyphs used as widget controls with no descriptive text. When a control
# is just the glyph, match it directly: carousel/gallery prev-next arrows and
# map zoom +/− buttons.
_MEDIA_CONTROL_GLYPHS = {
    # prev / next arrows
    "‹", "›", "«", "»", "<", ">", "‹‹", "››",
    "&lsaquo;", "&rsaquo;", "&laquo;", "&raquo;",
    "←", "→", "◄", "►", "▲", "▼",
    # map zoom controls
    "+", "−", "-", "–", "—",
}

# Dot-navigation labels like "Review 1", "Slide 3", "Go to slide 2" — the
# carousel's positional dots, not content.
import re as _re
_DOTNAV_RE = _re.compile(
    r"^(?:go to\s+)?(?:review|slide|item|page|image|photo)\s*\d+(?:\s+content)?$",
    _re.I,
)

# Form-submit / generic widget buttons whose label is functionally complete on
# its own (a newsletter "Submit", a search "Go"). These belong to map /
# newsletter / contact widgets the prompt already exempts; skip them here too
# so they aren't flagged as "content-free".
_WIDGET_FORM_LABELS = {
    "submit", "go", "search", "subscribe", "sign up", "send",
}

# Map attribution / credit links injected by map libraries (Leaflet + the tile
# providers). They're standard copyright links, not content CTAs, so a11y
# validation should ignore them.
_MAP_ATTRIBUTION_LABELS = {
    "leaflet", "openstreetmap", "open street map", "cartodb", "carto",
    "mapbox", "google", "google maps", "improve this map",
    "osm", "©", "© openstreetmap contributors", "openstreetmap contributors",
    "stamen", "stamen design", "maptiler", "here", "tomtom", "esri",
}


# Template-generated headings that are boilerplate, not author-written content.
# The review SOURCE label ("Review by - Google", "Reviews by - Yelp") is
# SpotHopper markup that's identical on every reviews section; its hyphen
# punctuation is template output, not a content choice to flag.
import re as __re_tb
_REVIEW_SOURCE_HEADING_RE = __re_tb.compile(
    r"^\s*reviews?\s+by\b", __re_tb.I
)


def _is_template_boilerplate(text: str) -> bool:
    """True if `text` is a template boilerplate heading to exclude from
    content/grammar validation."""
    if not text:
        return False
    return bool(_REVIEW_SOURCE_HEADING_RE.match(text.strip()))


def _is_decorative_media_control(text: str, service_type: str = "") -> bool:
    """
    True if `text` (lowercased visible+hidden) reads like a widget control that
    validation should skip.

    Map zoom glyphs ("+", "−") and bare single-word form-submit labels
    ("Submit", "Subscribe", "Go", …) are treated as controls EVERYWHERE, not
    just on map/newsletter sections — these tokens are essentially never a
    meaningful content CTA on a restaurant site, and the AI's section label
    isn't reliable enough to gate on (a combined map+newsletter block may be
    labeled "map", "contact", "other", etc.). Everything else (play/pause,
    dot-nav, prev/next arrows, gallery nav) is also always a control.
    """
    stripped = text.strip()

    # Dot navigation ("Review 1", "Slide 2 content", incl. doubled forms).
    if _DOTNAV_RE.match(stripped):
        return True
    words = stripped.split()
    if len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:] and _DOTNAV_RE.match(" ".join(words[:half])):
            return True

    # Media/widget control phrases (play/pause, prev/next, zoom in/out, …).
    if any(phrase in text for phrase in _MEDIA_CONTROL_PHRASES):
        return True

    # Bare control glyphs: carousel/gallery arrows AND map zoom +/−.
    if stripped in _MEDIA_CONTROL_GLYPHS:
        return True

    # Bare single-word form-submit labels ("submit", "subscribe", "go", …),
    # including the doubled visible+hidden form ("submit submit").
    half_text = stripped
    if len(words) % 2 == 0 and words[:len(words) // 2] == words[len(words) // 2:]:
        half_text = " ".join(words[:len(words) // 2])
    if half_text in _WIDGET_FORM_LABELS:
        return True

    # Map attribution / credit links (Leaflet, OpenStreetMap, CartoDB, …).
    if stripped in _MAP_ATTRIBUTION_LABELS or half_text in _MAP_ATTRIBUTION_LABELS:
        return True

    return False


# ──────────────────────────────────────────────────────────────────────────
# Public entry points
# ──────────────────────────────────────────────────────────────────────────

def validate_sections(restaurant: str, sections: list) -> list:
    """
    Validate a list of scraper sections for typos / strange content / a11y.

    Returns a list of result dicts:
        {section_index, service_type, heading, status, issue}
    where status is "OK" or "POTENTIAL ISSUE". Returns [] if the AI step is
    skipped (no key / no SDK / no usable sections).
    """
    client = _get_client()
    if client is None or not sections:
        return []

    payloads = []
    tracked = []  # (vid, original_index, section)
    for idx, section in enumerate(sections):
        if section is None:
            continue
        # Skip structural/no-content types
        if (section.get("ai_service") or "") in ("footer", "header", "other"):
            continue
        vid = f"s{idx}"
        payload = _section_to_payload(section, vid)
        if payload["heading"] or payload["body_text"] or payload["interactive"]:
            payloads.append(payload)
            tracked.append((vid, idx, section))

    if not payloads:
        return []

    prompt = _build_sections_prompt(restaurant, json.dumps(payloads, indent=2))
    raw = _call_openai(client, prompt)
    try:
        results = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        print("[content_validator] could not parse section validation JSON.", file=sys.stderr)
        results = []

    result_map = {r.get("id"): r.get("issue", "OK: no detailed response")
                  for r in results if isinstance(r, dict)}

    out = []
    for vid, idx, section in tracked:
        issue = result_map.get(vid, "OK: no detailed response")
        status = "POTENTIAL ISSUE" if "POTENTIAL ISSUE" in issue else "OK"
        payload = _section_to_payload(section, vid)
        out.append({
            "section_index": idx,
            "service_type": payload["service_type"],
            "heading": payload["heading"],
            "status": status,
            "issue": issue,
        })
    return out


def validate_reviews(restaurant: str, reviews: list) -> list:
    """
    Validate a list of reviews (each {text, reviewer}) against the review rules.

    Returns a list of result dicts:
        {review_index, reviewer, text, status, issue}
    Returns [] if the AI step is skipped or there are no reviews with text.
    """
    client = _get_client()
    if client is None or not reviews:
        return []

    payloads = []
    tracked = []  # (vid, original_index, review)
    for idx, rv in enumerate(reviews):
        text = (rv.get("text") or "").strip()
        if not text:
            continue
        vid = f"r{idx}"
        payloads.append({
            "id": vid,
            "reviewer": (rv.get("reviewer") or "Unknown").strip() or "Unknown",
            "text": text,
        })
        tracked.append((vid, idx, rv))

    if not payloads:
        return []

    prompt = _build_reviews_prompt(restaurant, json.dumps(payloads, indent=2))
    raw = _call_openai(client, prompt, budget=8000)
    try:
        results = json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        print("[content_validator] could not parse review validation JSON.", file=sys.stderr)
        results = []

    result_map = {r.get("id"): r.get("issue", "OK: no detailed response")
                  for r in results if isinstance(r, dict)}

    out = []
    for vid, idx, rv in tracked:
        issue = result_map.get(vid, "OK: no detailed response")
        status = "POTENTIAL ISSUE" if "POTENTIAL ISSUE" in issue else "OK"
        out.append({
            "review_index": idx,
            "reviewer": (rv.get("reviewer") or "Unknown"),
            "text": (rv.get("text") or ""),
            "status": status,
            "issue": issue,
        })
    return out


# ══════════════════════════════════════════════════════════════════════════
# UNIFIED SINGLE-CALL ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════
# Collapses all per-pair AI work into ONE OpenAI request:
#   * classify the home page's sections (both sites)
#   * validate the new home page's sections (typo / a11y)
#   * validate the new home page's reviews (review rules)
#   * classify each custom page's sections (both sites)
# The model returns ONE JSON object with four keys; we split it back out.

# Pulled from ai_classifier so the classification half uses the exact same
# label set and per-section payload the standalone classifier used.
from ai_classifier import (
    ALLOWED_LABELS as _CLS_LABELS,
    _section_payload as _cls_section_payload,
)


def _build_unified_prompt(restaurant: str,
                          home_classify_json: str,
                          home_sections_json: str,
                          home_reviews_json: str,
                          custom_classify_json: str) -> str:
    """
    Build ONE prompt covering four tasks. Each task keeps the wording of its
    standalone prompt so behavior matches the per-call versions; they're just
    delivered together and answered in one combined JSON object.
    """
    allowed = ", ".join(f'"{lbl}"' for lbl in _CLS_LABELS)
    return f"""You are analyzing the website of the restaurant "{restaurant}".
You have FOUR independent tasks. Do every task. Return ONE JSON object (no
markdown fences) with EXACTLY these four keys: "home_labels",
"home_section_issues", "home_review_issues", "custom_labels". Each value is a
JSON array as described in its task. If a task's input list is empty, return
an empty array for that key.

═══════════════════════════════════════════════════════════════════════════
TASK 1 — CLASSIFY HOME-PAGE SECTIONS  →  key "home_labels"
═══════════════════════════════════════════════════════════════════════════
For each section, return ONE label that best describes its PURPOSE, chosen
from exactly this list:
[{allowed}]
Read all signals together (heading, paragraphs, button visible + hidden text,
button hrefs — match slugs by SUBSTRING). The SAME logical section on the old
and new site MUST get the SAME label. Output array elements:
  {{"id": "<the input id, e.g. old_0 / new_2>", "label": "<one allowed label>"}}

Sections to classify:
{home_classify_json}

═══════════════════════════════════════════════════════════════════════════
TASK 2 — VALIDATE HOME-PAGE SECTIONS  →  key "home_section_issues"
═══════════════════════════════════════════════════════════════════════════
For each section check (A) SPELLING & GRAMMAR in `heading` and `body_text`
word-by-word (letter transposition like "Tialored"→"Tailored"; missing/extra
letters like "untill"→"until"; homophones like "bellow"→"below"; doubled
words; missing spaces; the U+FFFD character \ufffd), and (B) ACCESSIBILITY of
interactive elements (`screen_reader_reads` should be a clear action label;
flag content-free labels like "Click Here"/"Submit" with no clarifying hidden
text; flag REDUNDANT hidden text repeating the visible text). Skip a11y checks
for service_type "map"/"newsletter"/"contact".
{_ENTITY_SUPPRESSION_RULE}
{_SOCIAL_LABEL_SUPPRESSION_RULE}
Do NOT flag missing headings, repeated CTAs, minor name variations, Title
Case / ALL CAPS, or <br> separators. Output array elements:
  {{"id": "<section id>", "issue": "OK: <note>" or "POTENTIAL ISSUE: <detail>"}}
List multiple problems in one issue string separated by " | ".

Sections to validate:
{home_sections_json}

═══════════════════════════════════════════════════════════════════════════
TASK 3 — VALIDATE HOME-PAGE REVIEWS  →  key "home_review_issues"
═══════════════════════════════════════════════════════════════════════════
Check each review against these rules:
- no specific price/pricetag (e.g. $20)
- must not name another business (NAMED competitors only; generic "another
  spot" is fine)
- must not contain a worker's / any person's name INSIDE the review text
{_REVIEWER_NAME_RULE}
- positive tone only; no swearing; must not say it is expensive
- must not contain the U+FFFD character (\ufffd)
- if the reviewer name is Tina, Carlos or Cris, or the text reads like
  "This is the n-th example", flag it
{_ENTITY_SUPPRESSION_RULE}
Output array elements:
  {{"id": "<review id>", "issue": "OK: <note>" or "POTENTIAL ISSUE: <detail>"}}

Reviews to validate:
{home_reviews_json}

═══════════════════════════════════════════════════════════════════════════
TASK 4 — CLASSIFY CUSTOM-PAGE SECTIONS  →  key "custom_labels"
═══════════════════════════════════════════════════════════════════════════
Same rules as TASK 1 (same allowed label list, same SUBSTRING slug matching,
same old/new consistency). These sections come from other pages (press /
locations / parties / cater / reserve / about); each id is prefixed with its
page, e.g. "press::old_0", "press::new_1", "locations::old_0". Echo the id
back UNCHANGED. Output array elements:
  {{"id": "<page::side_index>", "label": "<one allowed label>"}}

Sections to classify:
{custom_classify_json}

═══════════════════════════════════════════════════════════════════════════
Return ONLY the single JSON object with the four keys. No prose, no fences."""


def _validation_payload(section: dict, vid: str) -> dict:
    """Section → {id, service_type, heading, body_text, interactive} for TASK 2."""
    return _section_to_payload(section, vid)


def analyze_all(restaurant: str,
                home_old_sections: list,
                home_new_sections: list,
                home_new_reviews: list,
                custom_pages: list) -> dict:
    """
    ONE OpenAI call covering classification + validation for the home page and
    classification for every custom page.

    custom_pages: list of {"kind": str, "old_sections": list, "new_sections": list}

    Returns:
      {
        "home_labels":        {"old_0": "catering", "new_1": "reviews", ...},
        "home_section_issues":[{section_index, service_type, heading, status, issue}, ...],
        "home_review_issues": [{review_index, reviewer, text, status, issue}, ...],
        "custom_labels":      {"press": {"old_0": "...", "new_1": "..."}, ...},
      }
    Empty / safe defaults on any failure (no key, no SDK, parse error).
    """
    empty = {
        "home_labels": {},
        "home_section_issues": [],
        "home_review_issues": [],
        "custom_labels": {},
    }
    client = _get_client()
    if client is None:
        return empty

    # ---- TASK 1 payload: home section classification (both sides) ----
    home_classify = []
    for i, sec in enumerate(home_old_sections):
        home_classify.append({"id": f"old_{i}", **_cls_section_payload(sec)})
    for i, sec in enumerate(home_new_sections):
        home_classify.append({"id": f"new_{i}", **_cls_section_payload(sec)})

    # ---- TASK 2 payload: home section validation (new side) ----
    home_sections_val = []
    home_val_tracked = []  # (vid, index, section)
    for idx, sec in enumerate(home_new_sections):
        if sec is None or (sec.get("ai_service") or "") in ("footer", "header", "other"):
            continue
        vid = f"s{idx}"
        payload = _validation_payload(sec, vid)
        if payload["heading"] or payload["body_text"] or payload["interactive"]:
            home_sections_val.append(payload)
            home_val_tracked.append((vid, idx, sec))

    # ---- TASK 3 payload: home reviews (new side) ----
    home_reviews_val = []
    home_review_tracked = []  # (vid, index, review)
    for idx, rv in enumerate(home_new_reviews or []):
        text = (rv.get("text") or "").strip()
        if not text:
            continue
        vid = f"r{idx}"
        home_reviews_val.append({
            "id": vid,
            "reviewer": (rv.get("reviewer") or "Unknown").strip() or "Unknown",
            "text": text,
        })
        home_review_tracked.append((vid, idx, rv))

    # ---- TASK 4 payload: custom-page classification (both sides per page) ----
    custom_classify = []
    for page in custom_pages or []:
        kind = page["kind"]
        for i, sec in enumerate(page.get("old_sections", [])):
            custom_classify.append({"id": f"{kind}::old_{i}", **_cls_section_payload(sec)})
        for i, sec in enumerate(page.get("new_sections", [])):
            custom_classify.append({"id": f"{kind}::new_{i}", **_cls_section_payload(sec)})

    # If there is genuinely nothing to do, skip the call.
    if not (home_classify or home_sections_val or home_reviews_val or custom_classify):
        return empty

    prompt = _build_unified_prompt(
        restaurant,
        json.dumps(home_classify, indent=2),
        json.dumps(home_sections_val, indent=2),
        json.dumps(home_reviews_val, indent=2),
        json.dumps(custom_classify, indent=2),
    )

    raw = _call_openai(client, prompt, budget=16000)
    try:
        data = json.loads(_strip_fences(raw))
        if not isinstance(data, dict):
            raise ValueError("top-level JSON is not an object")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[content_validator] could not parse unified JSON: {e}", file=sys.stderr)
        return empty

    # ---- Split TASK 1 ----
    home_labels = {}
    for item in data.get("home_labels", []) or []:
        if isinstance(item, dict) and item.get("id"):
            home_labels[item["id"]] = item.get("label", "")

    # ---- Split TASK 2 ----
    sec_issue_map = {item.get("id"): item.get("issue", "OK: no detailed response")
                     for item in (data.get("home_section_issues", []) or [])
                     if isinstance(item, dict)}
    home_section_issues = []
    for vid, idx, sec in home_val_tracked:
        issue = sec_issue_map.get(vid, "OK: no detailed response")
        payload = _validation_payload(sec, vid)
        home_section_issues.append({
            "section_index": idx,
            "service_type": payload["service_type"],
            "heading": payload["heading"],
            "status": "POTENTIAL ISSUE" if "POTENTIAL ISSUE" in issue else "OK",
            "issue": issue,
        })

    # ---- Split TASK 3 ----
    rev_issue_map = {item.get("id"): item.get("issue", "OK: no detailed response")
                     for item in (data.get("home_review_issues", []) or [])
                     if isinstance(item, dict)}
    home_review_issues = []
    for vid, idx, rv in home_review_tracked:
        issue = rev_issue_map.get(vid, "OK: no detailed response")
        home_review_issues.append({
            "review_index": idx,
            "reviewer": (rv.get("reviewer") or "Unknown"),
            "text": (rv.get("text") or ""),
            "status": "POTENTIAL ISSUE" if "POTENTIAL ISSUE" in issue else "OK",
            "issue": issue,
        })

    # ---- Split TASK 4 ----
    custom_labels = {}
    for item in data.get("custom_labels", []) or []:
        if not (isinstance(item, dict) and item.get("id")):
            continue
        cid = item["id"]
        if "::" not in cid:
            continue
        kind, key = cid.split("::", 1)
        custom_labels.setdefault(kind, {})[key] = item.get("label", "")

    return {
        "home_labels": home_labels,
        "home_section_issues": home_section_issues,
        "home_review_issues": home_review_issues,
        "custom_labels": custom_labels,
    }