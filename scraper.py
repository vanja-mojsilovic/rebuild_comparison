"""
Playwright-based scraper. Renders the page in a real headless Chromium
so JavaScript-driven content (Tock widgets, Toast embeds, etc.) is visible.

extract_sections() returns a list of section dictionaries. Each section has:
    {
        "headings": ["..."],   # h1+h2+h3 unified (because rebuild shifts levels)
        "h1": [...], "h2": [...], "h3": [...],
        "paragraphs": [...],
        "list_items": [...],
        "buttons": [...],      # {text, visible_text, hidden_text, href} dicts
        "raw_text": "...",     # full text content for diffing
        "html_element_type": "...",  # slideshow | carousel | cover_video | text+image
    }

Every text fragment belongs to EXACTLY ONE bucket — text inside a clickable
(a/button/role=button/role=link) goes to the button bucket only and does
NOT also appear in paragraphs/headings/etc.

HTML element type classification
---------------------------------
Each section is classified by the CSS selector that matched it, plus
additional signals found inside the element:

  carousel     – div.carousel-wrapper, or any section containing
                 [data-uk-slideshow], .uk-slideshow, .slick-slider,
                 .swiper-container, [data-ride="carousel"],
                 or role="listbox" / .carousel
  slideshow    – sections whose matched selector is NOT carousel-wrapper
                 but contain .uk-slider, .slick-list, .splide__list,
                 or a <ul> / <ol> with more than one <li> each holding
                 an <img> (image gallery pattern)
  cover_video  – div.custom_html_1-section, or any section that contains
                 a <video> or <iframe> with an autoplay / muted attribute,
                 or class tokens like "cover-video", "hero-video",
                 "video-background"
  text+image   – everything else (standard wp-block-group / tmt-section /
                 uk-overlay-panel / text-content sections)
"""

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup, Tag
import re


SECTION_SELECTORS = [
    # The actual SpotHopper cover-video container. This is the only wrapper
    # we can reliably classify as cover_video by selector alone — the
    # custom_html_1-section wrapper despite its name can hold anything
    # (EGift Cards button, announcements, custom HTML the operator wrote).
    "div#home_page_cover",
    # Specific SpotHopper section identifiers — order matters because the
    # selector name drives html-type fast-paths for some of these.
    "div.carousel-wrapper",
    "div.reviews-v2-wrapper",
    "div.gallery-v4-wrapper",
    "div.about-us-v8-wrapper",
    "div.reservations-wrapper",
    "div.ot-reservations-wrapper",
    "div.googlemap-v3-wrapper",
    "div.openstreetmap-v3-wrapper",
    "div.maps-wrapper",
    "div.contact-v4",
    # custom_html_1-section is a generic "custom HTML block" wrapper —
    # classify by its actual content (video / button / text) rather than
    # by the selector name.
    "div.custom_html_1-section",
    # Generic / WP-rebuild wrappers
    "section.wp-block-group",
    "div.text-content",
    # Legacy SpotHopper bits
    "div.uk-overlay-panel",
    "div.tmt-section",
    # Press-page cards: each press mention is its own <section class=
    # "press-custom-card"> inside a .press-custom-grid. They aren't
    # .section-wrapper blocks, so match them explicitly (each card → one
    # section) for the custom-page press comparison.
    "section.press-custom-card",
    # Universal SpotHopper wrapper — comes LAST so it catches any top-level
    # section we didn't identify by a more specific class. This ensures
    # every visible block on the page is captured in document order.
    "div.section-wrapper",
]

HIDDEN_CLASSES = {
    "visuallyhidden",
    "visually-hidden",
    "sr-only",
    "screen-reader-text",
    "screen-reader-only",
    "off-screen",
    "offscreen",
    "show-for-sr",
    "sr-only-focusable",
}

# ---------------------------------------------------------------------------
# HTML element-type classification helpers
# ---------------------------------------------------------------------------

# CSS selectors that indicate a slideshow (one full slide visible, auto-advance,
# dot-nav). SpotHopper's hero/CTA blocks use UIKit's data-uk-slideshow widget,
# which despite the "slideshow" name is the dominant pattern here.
_SLIDESHOW_SELECTORS = [
    "[data-uk-slideshow]",
    "[data-slideshow]",
    ".uk-slideshow",
    ".slideshow-v2-wrapper",
    ".slideshow-wrapper",
    ".uk-slidenav-position",
    ".slick-slider",
    ".splide",
    ".glide",
    ".flickity-slider",
]

# CSS selectors that indicate a carousel (multi-item horizontal scroll/swipe,
# typically used for reviews, testimonials, gallery thumbs).
_CAROUSEL_SELECTORS = [
    ".uk-slider",
    ".uk-slider-items",
    ".swiper-container",
    ".swiper-wrapper",
    ".swiper",
    '[data-ride="carousel"]',
    ".owl-carousel",
    '[role="listbox"]',
    ".carousel",
    ".carousel-wrapper",
    ".slick-list",
]

# Class/attr tokens that mark a cover-video section
_VIDEO_CLASS_TOKENS = {
    "cover-video",
    "hero-video",
    "video-background",
    "bg-video",
    "video-hero",
    "video-cover",
}


def _matches_any(el: Tag, selectors: list) -> bool:
    """
    True if the element itself, any ancestor, OR any descendant matches one of
    the selectors. Both directions are needed because a section element can
    sit *inside* a slideshow wrapper (look up) or *contain* a carousel widget
    (look down).
    """
    for sel in selectors:
        # Self + ancestors
        cursor = el
        while cursor is not None and isinstance(cursor, Tag):
            if _tag_matches_simple_selector(cursor, sel):
                return True
            cursor = cursor.parent
        # Descendants
        if el.select_one(sel):
            return True
    return False


def _tag_matches_simple_selector(tag: Tag, sel: str) -> bool:
    """
    Cheap matcher for the limited selector shapes used in our rule lists:
      .class-name        → class membership
      [data-attr]        → attribute presence
      [data-attr="val"]  → attribute equality
    """
    if sel.startswith("."):
        return sel[1:] in (tag.get("class") or [])
    if sel.startswith("[") and sel.endswith("]"):
        body = sel[1:-1]
        if "=" in body:
            name, val = body.split("=", 1)
            val = val.strip().strip('"').strip("'")
            return tag.get(name.strip()) == val
        return tag.has_attr(body.strip())
    return False


def _detect_html_element_type(el: Tag, matched_selector: str) -> str:
    """
    Classify one section element into one of four layout types:
      - "slideshow"   one slide visible at a time with auto-advance / dot nav
                      (UIKit data-uk-slideshow, SpotHopper slideshow-v2-wrapper,
                      Slick single-slider, Splide, Glide, Flickity)
      - "carousel"    multi-item horizontal scroll/swipe (reviews row, gallery
                      thumbs, owl/swiper/bootstrap carousels, .uk-slider)
      - "cover_video" full-bleed background video
      - "text+image"  standard content block (default)

    Detection is hierarchical: the section element may be an *inner* node of a
    slideshow (e.g. div.uk-overlay-panel inside a single <li> slide). In that
    case the wrapper is several levels up, so we walk ancestors.

    Parameters
    ----------
    el : Tag
        The BeautifulSoup element for this section.
    matched_selector : str
        The CSS selector string that caused this element to be selected
        (one of SECTION_SELECTORS).
    """
    # 1. Selector-based fast paths from SECTION_SELECTORS
    # home_page_cover is the ONLY wrapper that reliably means cover_video.
    # Everything else is decided by what's actually inside the element.
    if "home_page_cover" in matched_selector:
        return "cover_video"
    if "carousel-wrapper" in matched_selector or "reviews-v2-wrapper" in matched_selector:
        return "carousel"
    if "gallery-v4-wrapper" in matched_selector:
        return "slideshow"

    # 2. Slideshow check first — walk ANCESTORS as well as descendants.
    #    SpotHopper's slideshow-v2-wrapper sits above the uk-overlay-panel that
    #    the section selector matched, so we have to look upward.
    if _matches_any(el, _SLIDESHOW_SELECTORS):
        return "slideshow"

    # 3. Carousel check — also walk ancestors (reviews are wrapped above).
    if _matches_any(el, _CAROUSEL_SELECTORS):
        return "carousel"

    # 4. Image-gallery pattern: a <ul>/<ol> in the section whose direct
    #    <li> children each contain an <img>. Treat as slideshow.
    for list_el in el.find_all(["ul", "ol"]):
        items = list_el.find_all("li", recursive=False)
        if len(items) > 1 and all(li.find("img") for li in items):
            return "slideshow"

    # 5. Cover-video signals (descendants or own class)
    for video_el in el.find_all("video"):
        if video_el.get("autoplay") is not None or video_el.get("muted") is not None:
            return "cover_video"
    for iframe_el in el.find_all("iframe"):
        src = iframe_el.get("src", "")
        if "autoplay=1" in src or "muted" in src:
            return "cover_video"
    el_classes = set(el.get("class") or [])
    if el_classes & _VIDEO_CLASS_TOKENS:
        return "cover_video"
    for desc in el.find_all(True):
        desc_classes = set(desc.get("class") or [])
        if desc_classes & _VIDEO_CLASS_TOKENS:
            return "cover_video"

    # 6. Default
    return "text+image"


# ---------------------------------------------------------------------------
# Core scraping / parsing
# ---------------------------------------------------------------------------

def scrape_page(url: str, timeout_ms: int = 30000) -> dict:
    """Render a page with Playwright and return a structured dict of content."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (compatible; CloneComparisonBot/1.0)"
        )
        page = context.new_page()
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_load_state("load", timeout=timeout_ms)
        # Give JS-rendered widgets (Tock, Toast, etc.) time to inject markup
        page.wait_for_timeout(2500)
        html = page.content()
        browser.close()

    return parse_html(html, base_url=url)


def parse_html(html: str, base_url: str = "") -> dict:
    """Parse rendered HTML into structured sections + identity fields."""
    soup = BeautifulSoup(html, "html.parser")
    return {
        "base_url": base_url,
        "title": (soup.title.string.strip() if soup.title and soup.title.string else ""),
        "restaurant_name": extract_restaurant_name(soup),
        "sections": extract_sections(soup),
        "identity": extract_identity(soup),
        "reviews": extract_reviews(soup),
        "all_links": collect_all_links(soup),
        "page_h1s": extract_page_h1s(soup),
    }


def extract_page_h1s(soup: BeautifulSoup) -> list:
    """
    Find every <h1> on the page, regardless of visibility, section membership,
    or whether it sits inside an interactive element.

    Returns a list of dicts:
        [{"text": "...", "visible": True/False, "empty": True/False}, ...]

    - text:    cleaned text content (may be "" if the tag is empty)
    - visible: False if the element OR any ancestor is hidden via class,
               inline style, or aria-hidden; True otherwise
    - empty:   True if the tag exists but has no non-whitespace text content

    This is used by the SEO summary so we can tell apart three states:
      missing  — no <h1> tag found anywhere on the page
      empty    — <h1> tag exists but is text-empty
      text     — <h1> tag exists and has text content
    """
    out = []
    for tag in soup.find_all("h1"):
        text = _clean_text(tag.get_text())
        out.append({
            "text": text,
            "visible": not _is_hidden_anywhere(tag),
            "empty": text == "",
        })
    return out


def _is_hidden_anywhere(node) -> bool:
    """
    True if `node` or any of its ancestors is hidden via:
      - a known visually-hidden class (sr-only, visually-hidden, etc.)
      - a structurally-hidden class (.hidden, .d-none, .invisible)
      - aria-hidden="true"
      - inline style: display:none or visibility:hidden
      - the `hidden` HTML attribute
      - SpotHopper's data-hidden="yes" toggle (used on .hide-show-section
        wrappers when the operator hid a section through the admin UI)
    """
    cursor = node
    while cursor is not None and isinstance(cursor, Tag):
        if _is_visually_hidden(cursor):
            return True
        # Structurally-hidden classes (Bootstrap d-none, generic .hidden, .invisible)
        cls = cursor.get("class") or []
        if any(c in ("hidden", "d-none", "invisible") for c in cls):
            return True
        # Owl Carousel injects duplicate slides marked .cloned to fake an
        # infinite loop. These are literal copies of real slides and must
        # never be scraped — on some sites they carry aria-hidden, on others
        # (e.g. the old SpotHopper template) they don't, so we key off the
        # .cloned class directly.
        if "cloned" in cls:
            return True
        if cursor.get("aria-hidden", "").lower() == "true":
            return True
        if cursor.has_attr("hidden"):
            return True
        # SpotHopper section toggle: <article data-hidden="yes"> means the
        # operator chose to hide this whole block.
        if (cursor.get("data-hidden") or "").lower() == "yes":
            return True
        style = (cursor.get("style") or "").lower().replace(" ", "")
        if "display:none" in style or "visibility:hidden" in style:
            return True
        cursor = cursor.parent
    return False


def extract_sections(soup: BeautifulSoup) -> list:
    """
    Walk the document tree ONCE in document order and pull every section-like
    container. The earlier implementation iterated SECTION_SELECTORS one at a
    time (all wp-block-groups, then all text-contents, ...), which broke the
    on-page order — a cover_video block at the top of the page could end up
    listed after several wp-block-groups that visually appeared below it.

    Here we do a single descendant walk. For each element we check whether it
    matches ANY of the section selectors; if so it becomes a section. We also
    record which selector matched first so the html-type classifier can use
    the selector-based fast path.

    A section nested inside another already-recorded section is skipped to
    avoid double-counting (e.g. a uk-overlay-panel inside a carousel-wrapper).
    """
    out = []
    recorded_roots = []  # list of Tag objects already added

    # Pre-compute the class-token and tag predicates for each selector so we
    # can match against a single element cheaply.
    matchers = []
    for sel in SECTION_SELECTORS:
        matchers.append((sel, _compile_section_selector(sel)))

    for el in soup.descendants:
        if not isinstance(el, Tag):
            continue

        matched_sel = None
        for sel, predicate in matchers:
            if predicate(el):
                matched_sel = sel
                break
        if matched_sel is None:
            continue

        # Skip if this element sits inside one we've already recorded
        if any(_is_descendant_of(el, root) for root in recorded_roots):
            continue

        if el.find_parent(class_="map-newsletter"):
            continue

        # Skip sections that are hidden from the user — operators sometimes
        # leave a SpotHopper block in the HTML but toggle data-hidden="yes"
        # to hide it from the live page. Comparing those would show false
        # differences for content that isn't actually visible.
        if _is_hidden_anywhere(el):
            continue

        # Note: previously we skipped any element with "reviews", "banner",
        # or "contact" in its class list, to keep those out of the main
        # report's element-by-element comparison. That filter was too coarse:
        # it dropped reviews-v2-wrapper / contact-v4 sections from the
        # sections-tab listing too. We now capture every section here; the
        # comparator handles reviews separately via _compare_reviews so they
        # won't be double-counted in the main report.

        # SpotHopper's about-us-v8 template often lays out multiple visually-
        # distinct blocks as sibling <section> elements inside one wrapper:
        #   about-us-v8-wrapper > .container.about-us-v8 > section, section, ...
        # Each child section is a separate visual block (e.g. main "about us",
        # then "parties", then "downstairs") with its own heading and CTA, so
        # we emit one section dict per child rather than collapsing them.
        # The wrapper itself is still added to recorded_roots so the catch-all
        # div.section-wrapper selector doesn't re-match it.
        exploded = _explode_multi_section_wrapper(el, matched_sel)
        if exploded:
            for child in exploded:
                child_section = _section_data(child)
                child_section["html_element_type"] = _detect_html_element_type(child, matched_sel)
                child_section["section_kind"] = _section_kind_from_selector(matched_sel, child)
                out.append(child_section)
            recorded_roots.append(el)
            continue

        section = _section_data(el)
        section["html_element_type"] = _detect_html_element_type(el, matched_sel)
        section["section_kind"] = _section_kind_from_selector(matched_sel, el)
        out.append(section)
        recorded_roots.append(el)

    return out


def _explode_multi_section_wrapper(el: Tag, matched_sel: str) -> list:
    """
    If `el` is a SpotHopper about-us-v8 wrapper containing multiple sibling
    <section> children laying out distinct visual blocks, return those child
    sections as a list (so each one becomes a separate section row).

    Returns [] when:
      - the wrapper is not about-us-v8-wrapper, or
      - the wrapper only contains zero or one child section (no need to explode)
    """
    if "about-us-v8-wrapper" not in matched_sel:
        return []

    # The template structure is:
    #   div.about-us-v8-wrapper > div.container.about-us-v8 > section, section, ...
    container = el.find("div", class_="about-us-v8")
    if container is None:
        return []
    children = container.find_all("section", recursive=False)
    if len(children) <= 1:
        # One or none — nothing to explode; let the wrapper become the section.
        return []
    return children


# Maps from a SpotHopper section selector to a friendly section name.
# The label-resolution flow in comparator.py uses this when the service
# classifier can't find a name from heading text alone (e.g. the gallery
# wrapper has no <h2>, just images).
_SECTION_KIND_BY_SELECTOR = {
    "div#home_page_cover":         "cover video",
    "div.carousel-wrapper":        "carousel",
    "div.reviews-v2-wrapper":      "reviews",
    "div.gallery-v4-wrapper":      "gallery",
    "div.about-us-v8-wrapper":     "",  # let service classifier name this from headings
    "div.reservations-wrapper":    "reservations",
    "div.ot-reservations-wrapper": "reservations",
    "div.googlemap-v3-wrapper":    "map",
    "div.openstreetmap-v3-wrapper": "map",
    "div.maps-wrapper":            "map",
    "div.contact-v4":              "contact",
    # custom_html_1-section is a generic container; do not hardcode a kind.
    "div.custom_html_1-section":   "",
}


def _section_kind_from_selector(matched_sel: str, el: Tag) -> str:
    """Return a friendly section name hint based on the matched selector."""
    return _SECTION_KIND_BY_SELECTOR.get(matched_sel, "")


def _compile_section_selector(sel: str):
    """
    Build a predicate function that returns True for a Tag matching `sel`.
    Supports the shapes used in SECTION_SELECTORS:
        "section.wp-block-group"   tag + class
        "div.text-content"         tag + class
        "div#home_page_cover"      tag + id
        "div"                      tag only
    """
    if "#" in sel:
        tag_name, id_name = sel.split("#", 1)
        tag_name = tag_name.strip() or None
        id_name = id_name.strip()

        def _pred_id(el: Tag) -> bool:
            if tag_name and el.name != tag_name:
                return False
            return el.get("id") == id_name
        return _pred_id

    if "." in sel:
        tag_name, class_name = sel.split(".", 1)
        tag_name = tag_name.strip() or None
        class_name = class_name.strip()

        def _pred(el: Tag) -> bool:
            if tag_name and el.name != tag_name:
                return False
            return class_name in (el.get("class") or [])
        return _pred

    # Plain tag selector fallback
    def _pred_tag(el: Tag) -> bool:
        return el.name == sel
    return _pred_tag


def _is_descendant_of(el: Tag, root: Tag) -> bool:
    """True if `el` is somewhere inside `root` in the document tree."""
    cursor = el.parent
    while cursor is not None:
        if cursor is root:
            return True
        cursor = cursor.parent
    return False


def _section_data(el: Tag) -> dict:
    """
    Pull headings, paragraphs, buttons, list items from one section.

    Key rule: text inside a clickable goes to `buttons` only — never
    duplicated in paragraph/heading buckets.

    Review carousels are special-cased: the per-slide content (the
    "review by - X" heading, the reviewer name, and the quote paragraph) is
    NOT collected as ordinary heading/paragraph rows, because reviews are
    compared separately as REVIEW rows (deduplicated across the whole
    carousel). Only the section header (e.g. "Reviews") is kept. This avoids
    asymmetry between the old template (which leaves inactive slides visible
    in the DOM) and the new one (which hides them).
    """

    # Detect a reviews carousel by its wrapper classes.
    el_classes = " ".join(el.get("class") or [])
    _is_review_carousel = "reviews-v2-wrapper" in el_classes or (
        "carousel-wrapper" in el_classes and "review" in el_classes.lower()
    )

    def _in_review_slide(node: Tag) -> bool:
        """True if node sits inside a carousel review slide (owl-item / item /
        review-text), i.e. it's per-slide content rather than the section
        header."""
        if not _is_review_carousel:
            return False
        cursor = node.parent
        while cursor is not None and isinstance(cursor, Tag):
            if cursor is el:
                return False
            cls = cursor.get("class") or []
            if any(c in ("owl-item", "review-text") or c == "item" for c in cls):
                return True
            cursor = cursor.parent
        return False

    def _is_review_source_heading(node: Tag) -> bool:
        """
        True if `node` is the per-slide review SOURCE heading — the
        "Review by - Google" / "review by - Yelp" line (class "review-by" on
        the new template, a bare <h2> on the old one).

        This is kept (deduplicated to one row, since it's identical on every
        slide) while reviewer names and quote paragraphs remain skipped, so
        the report shows which platform the reviews come from.
        """
        if not _is_review_carousel:
            return False
        cls = node.get("class") or []
        if "review-by" in cls:
            return True
        txt = _clean_text(node.get_text(separator=" ")).lower()
        return txt.startswith("review by") or txt.startswith("reviews by")

    # --- 1. Find all clickables and mark their text as off-limits ---
    clickable_selector = 'a, button, [role="button"], [role="link"]'
    # Skip clickables that sit inside a hidden wrapper (aria-hidden="true",
    # display:none, .d-none, data-hidden="yes", etc.). Carousel libraries like
    # Owl clone slides and mark both the clones and the non-active slides
    # aria-hidden="true"; those duplicates must not be scraped.
    clickable_els = [c for c in el.select(clickable_selector)
                     if not _is_hidden_anywhere(c)]

    off_limits = set()
    for c in clickable_els:
        off_limits.add(id(c))
        for desc in c.find_all(True):
            off_limits.add(id(desc))

    # --- 2. Build the buttons list ---
    buttons = []
    for clickable in clickable_els:
        hidden_text = ""
        for descendant in clickable.find_all(True):
            if _is_visually_hidden(descendant):
                t = _clean_text(descendant.get_text())
                if t:
                    hidden_text = f"{hidden_text} {t}".strip()

        full_text = _clean_text(clickable.get_text())
        visible_text = full_text
        if hidden_text and hidden_text in visible_text:
            visible_text = _clean_text(visible_text.replace(hidden_text, ""))

        combined = " ".join(x for x in [visible_text, hidden_text] if x)
        if not combined:
            continue

        href = clickable.get("href") or ""
        if not href:
            inner_a = clickable.find("a", href=True)
            if inner_a:
                href = inner_a.get("href") or ""

        buttons.append({
            "text": combined,
            "visible_text": visible_text,
            "hidden_text": hidden_text,
            "href": href,
        })

    # --- 3. Collect headings/paragraphs/list-items, excluding clickable text
    #         and excluding anything inside a hidden wrapper (carousel clones,
    #         inactive slides, display:none blocks, etc.). ---
    def texts(tag_name: str) -> list:
        out = []
        seen = set()
        for t in el.find_all(tag_name):
            if id(t) in off_limits:
                continue
            if _is_hidden_anywhere(t):
                continue
            # In a reviews carousel, skip per-slide content (reviewer name,
            # quote paragraph) — those are reported as REVIEW rows instead.
            # Keep the section header AND the review SOURCE heading
            # ("Review by - Google"), which is deduped to a single row below.
            if _in_review_slide(t) and not _is_review_source_heading(t):
                continue
            text_value = _text_excluding_clickables(t)
            if not text_value:
                continue
            # De-duplicate identical text within the same section. Carousels
            # render several slides at once (only one "active"), and on the
            # old template the inactive slides aren't marked hidden, so the
            # same "review by - Yelp" heading would otherwise be collected
            # many times. Collapsing duplicates keeps one row per distinct
            # heading/paragraph and aligns old vs new (where the new template
            # hides inactive slides and yields one already).
            key = text_value.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text_value)
        return out

    # Hidden (screen-reader-only) text per heading, keyed by the heading's
    # VISIBLE text. The comparison strips this from the matched text (so the
    # address still pairs) but reports it in the Old/New hidden columns, and
    # uses it to mark a visible-match-but-hidden-differs pair as EXPECTED.
    heading_hidden = {}
    def collect_hidden(tag_name: str):
        for t in el.find_all(tag_name):
            if id(t) in off_limits:
                continue
            if _is_hidden_anywhere(t):
                continue
            if _in_review_slide(t):
                continue
            visible = _text_excluding_clickables(t)
            if not visible:
                continue
            hidden = _hidden_text_excluding_clickables(t)
            if hidden:
                # Keyed by normalized-ish visible text; last write wins (rare dup)
                heading_hidden[visible] = hidden

    h1 = texts("h1")
    h2 = texts("h2")
    h3 = texts("h3")
    h4 = texts("h4")
    for lvl in ("h1", "h2", "h3", "h4"):
        collect_hidden(lvl)
    paragraphs = texts("p")
    list_items = texts("li")

    headings = h1 + h2 + h3 + h4
    raw_text = _clean_text(el.get_text(separator=" "))

    return {
        "headings": headings,
        "h1": h1,
        "h2": h2,
        "h3": h3,
        "h4": h4,
        "heading_hidden": heading_hidden,
        "paragraphs": paragraphs,
        "list_items": list_items,
        "buttons": buttons,
        "raw_text": raw_text,
        # html_element_type is injected by extract_sections() after this call
    }


def extract_identity(soup: BeautifulSoup) -> dict:
    """Pull address, phone, email, Google Business link."""
    def pick(selector: str) -> str:
        el = soup.select_one(selector)
        return _clean_text(el.get_text()) if el else ""

    street = (pick(".contact-location .address") or pick(".address")
              or pick('[itemprop="streetAddress"]'))
    city_state = (pick(".contact-location .city-state") or pick(".city-state"))
    if not city_state:
        locality = pick('[itemprop="addressLocality"]')
        region = pick('[itemprop="addressRegion"]')
        if locality or region:
            city_state = ", ".join([x for x in [locality, region] if x])
    zip_code = (pick(".contact-location .zip") or pick(".zip")
                or pick('[itemprop="postalCode"]'))
    phone_el = (
        soup.select_one('.contact-us a[href^="tel:"]')
        or soup.select_one('.contact-location a[href^="tel:"]')
        or soup.select_one('.footer a[href^="tel:"]')
        or _first_tel_with_digits(soup)
    )
    phone_href = phone_el.get("href", "") if phone_el else ""
    phone_display = _clean_text(phone_el.get_text()) if phone_el else ""
    phone_digits = "".join(c for c in (phone_href or phone_display) if c.isdigit())
    mail_el = (
        soup.select_one('.contact-us a[href^="mailto:"]')
        or soup.select_one('.contact-location a[href^="mailto:"]')
        or soup.select_one('.footer a[href^="mailto:"]')
        or soup.select_one('a[href^="mailto:"]')
    )
    email_href = mail_el.get("href", "") if mail_el else ""
    email_display = _clean_text(mail_el.get_text()) if mail_el else ""
    email_normalized = email_href.replace("mailto:", "").strip().lower()
    google_link = ""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        cls = " ".join(a.get("class") or [])
        if "google-icon" in cls.lower():
            google_link = href
            break
        if a.find(class_=lambda c: c and "fa-google" in c):
            google_link = href
            break
        if "google.com/search" in href and "lrd=" in href:
            google_link = href
            break
        if any(s in href for s in ("google.com/maps", "maps.google.com", "g.page/")):
            google_link = href
            break
    return {
        "address": {
            "street": street,
            "city_state": city_state,
            "zip": zip_code,
            "full": ", ".join([x for x in [street, city_state, zip_code] if x]),
        },
        "phone": {
            "display": phone_display,
            "digits": phone_digits,
        },
        "email": {
            "display": email_display,
            "normalized": email_normalized,
        },
        "google_link": google_link,
    }


def extract_reviews(soup: BeautifulSoup) -> list:
    """
    Pull review snippets from the page. Tries four patterns:
      1. SpotHopper blockquote layout
      2. schema.org Review microdata
      3. Generic .review / .testimonial CSS classes
      4. Third-party widget markers (Yelp / Google reviews badges)
    Returns a list of dicts: {text, reviewer}.
    """
    out = []
    seen = set()

    def push(text, reviewer):
        text = _clean_text(text or "")
        reviewer = _clean_text(reviewer or "")
        if not text or len(text) < 10:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        out.append({"text": text, "reviewer": reviewer})

    # 1. blockquote layout (SpotHopper default)
    for bq in soup.find_all("blockquote"):
        p = bq.find("p")
        text = (p.get_text() if p else bq.get_text()) or ""
        reviewer = ""
        # The reviewer name lives in an <h4 class="reviewer"> (or h3) that is
        # usually a SIBLING of the .review-text wrapper, not a descendant of
        # the blockquote's immediate parent. Walk up to the review "item"
        # container and search there so the name is found regardless of which
        # sibling holds it.
        container = bq.parent
        for _ in range(3):  # blockquote → review-text → item (a few hops max)
            if container is None:
                break
            cls = container.get("class") or []
            if any(c in ("item", "review", "review-item", "testimonial") for c in cls):
                break
            container = container.parent
        search_root = container if container is not None else bq.parent
        heading = None
        if search_root is not None:
            heading = search_root.find(
                class_=lambda c: c and ("reviewer" in c or "review-author" in c)
            ) or search_root.find(["h3", "h4"])
        if heading:
            # Collapse whitespace/newlines first so the strip regexes below
            # behave predictably (the raw heading text spans multiple lines).
            raw_name = _clean_text(heading.get_text())
            reviewer = re.sub(r"^.*?\bby\s*", "", raw_name, flags=re.I)
            reviewer = re.sub(r"[:|].*$", "", reviewer)
            reviewer = re.sub(r"\s*-\s*.*$", "", reviewer).strip()
        push(text, reviewer)

    # 2. schema.org Review microdata
    for el in soup.find_all(attrs={"itemtype": re.compile(r"Review", re.I)}):
        body = el.find(attrs={"itemprop": "reviewBody"})
        author = el.find(attrs={"itemprop": "author"})
        push(body.get_text() if body else "", author.get_text() if author else "")

    # 3. Generic class-based review markup
    for sel in (".review", ".review-item", ".testimonial"):
        for el in soup.select(sel):
            push(el.get_text(), "")

    # 4. Third-party widget markers (content rendered by JS we can't crawl)
    if not out:
        if soup.select_one('[id^="yelp-biz-badge-"], [class*="yelp-widget"]'):
            push("[Yelp reviews widget — content rendered by JS, not visible in HTML]", "")
        elif soup.select_one('[class*="google-review"], [class*="grw-"]'):
            push("[Google reviews widget — content rendered by JS, not visible in HTML]", "")

    return out


def extract_restaurant_name(soup: BeautifulSoup) -> str:
    """
    Pull the restaurant name from the page metadata. Tries the cleanest
    sources first, falls back to the page <title>.
    """
    apple = soup.find("meta", attrs={"name": "apple-mobile-web-app-title"})
    if apple and apple.get("content"):
        return apple["content"].strip()

    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return _strip_location_suffix(og["content"].strip())

    tw = soup.find("meta", attrs={"name": "twitter:title"})
    if tw and tw.get("content"):
        return _strip_location_suffix(tw["content"].strip())

    if soup.title and soup.title.string:
        return _strip_location_suffix(soup.title.string.strip())

    return ""


def _strip_location_suffix(s: str) -> str:
    """
    Turn 'Hudson Social - Westchester, Dobbs Ferry, NY' into 'Hudson Social'.
    Splits on common separators and keeps the first chunk.
    """
    if not s:
        return ""
    for sep in (" — ", " – ", " - ", " | "):
        if sep in s:
            return s.split(sep, 1)[0].strip()
    return s.strip()


def collect_all_links(soup: BeautifulSoup) -> list:
    """All anchor hrefs on the page, with their visible text."""
    out = []
    for a in soup.find_all("a", href=True):
        text = _clean_text(a.get_text())
        out.append({"text": text, "href": a["href"]})
    return out


# Characteristic slug fragments for the "other relevant pages" content check.
# Matching is a substring test of the bare fragment against the URL path, so a
# fragment like "cater" matches "/catering", "parties" matches
# "/private-parties-page", "reserve" matches "/reservations", and "locations"
# matches "/our-locations". Order matters only for display.
RELEVANT_PAGE_SLUGS = {
    "about":     "about",
    "cater":     "cater",
    "parties":   "parties",
    "reserve":   "reserv",   # matches both "reserve" and "reservations"
    "locations": "location",  # matches "location" and "locations"
    "press":     "press",
}


def find_relevant_page_urls(page_data: dict) -> dict:
    """
    From a scraped page's `all_links`, find the URLs of the other relevant
    pages (about / cater / parties / reserve / locations) by matching the
    characteristic slug fragment in each link's href path.

    Returns {page_kind: absolute_url} for the kinds found. Relative hrefs are
    resolved against the page's base_url. When several links match a kind, the
    first one in document order wins. Matching is a substring test on the path,
    so "/catering", "/private-parties-page", "/reservations", "/our-locations"
    all match their respective kinds. Off-site links (different host) and
    non-navigational schemes (mailto/tel/#) are ignored.
    """
    from urllib.parse import urljoin, urlparse

    base = page_data.get("base_url", "") or ""
    base_host = urlparse(base).netloc.lower().lstrip("www.") if base else ""
    found = {}
    for link in page_data.get("all_links", []):
        href = (link.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base, href)
        parsed = urlparse(absolute)
        path = parsed.path.lower().rstrip("/")
        if not path:
            continue
        # Stay on the same site (ignore external order/booking domains).
        host = parsed.netloc.lower().lstrip("www.")
        if base_host and host and host != base_host:
            continue
        for kind, frag in RELEVANT_PAGE_SLUGS.items():
            if kind in found:
                continue
            if frag in path:
                found[kind] = absolute
    return found


def _is_visually_hidden(node) -> bool:
    """True if the element carries a known accessibility-hidden class."""
    if not isinstance(node, Tag):
        return False
    cls = node.get("class") or []
    return any(c in HIDDEN_CLASSES for c in cls)


def _hidden_text_excluding_clickables(node: Tag) -> str:
    """
    Return the visually-hidden (screen-reader-only) text directly within
    `node`, EXCLUDING any text inside clickables (whose hidden text belongs to
    the button bucket). Used to surface a heading's sr-only prefix/suffix
    (e.g. "Visit us at") in the report's hidden column without letting it
    affect the visible-text match.
    """
    skip_ids = set()
    for clickable in node.select('a, button, [role="button"], [role="link"]'):
        for s in clickable.find_all(string=True):
            skip_ids.add(id(s))

    parts = []
    for hidden_el in node.find_all(True):
        if not _is_visually_hidden(hidden_el):
            continue
        for s in hidden_el.find_all(string=True):
            if id(s) in skip_ids:
                continue
            parts.append(str(s))
    return _clean_text(" ".join(parts))


def _text_excluding_clickables(node: Tag) -> str:
    """
    Return the VISIBLE text content of `node` with two kinds of text removed:

      1. Text inside clickables (<a>, <button>, [role=button], [role=link]) —
         that text belongs to the buttons bucket, not headings/paragraphs,
         so excluding it here prevents double-counting.
      2. Visually-hidden text (sr-only / visuallyhidden / etc.) — this is
         screen-reader-only chrome like "five star review by" prepended to a
         reviewer name. It isn't visible content and would otherwise make an
         old reviewer-name heading ("Milka K:") fail to match the new site's
         "five star review by Milka K:". Stripping it leaves just the visible
         name on both sides so they pair correctly.
    """
    skip_ids = set()
    for clickable in node.select('a, button, [role="button"], [role="link"]'):
        for s in clickable.find_all(string=True):
            skip_ids.add(id(s))
    # Also skip text inside any visually-hidden descendant
    for hidden_el in node.find_all(True):
        if _is_visually_hidden(hidden_el):
            for s in hidden_el.find_all(string=True):
                skip_ids.add(id(s))

    parts = []
    for s in node.find_all(string=True):
        if id(s) in skip_ids:
            continue
        parts.append(str(s))
    return _clean_text(" ".join(parts))


def _first_tel_with_digits(soup: BeautifulSoup) -> Tag | None:
    """Find the first tel: link whose href has at least 7 digits."""
    for a in soup.find_all("a", href=True):
        if not a["href"].startswith("tel:"):
            continue
        digits = "".join(c for c in a["href"] if c.isdigit())
        if len(digits) >= 7:
            return a
    return None


def _clean_text(s: str) -> str:
    """Collapse whitespace and trim."""
    if not s:
        return ""
    return " ".join(s.split())