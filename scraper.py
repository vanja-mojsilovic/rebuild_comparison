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
    }

Every text fragment belongs to EXACTLY ONE bucket — text inside a clickable
(a/button/role=button/role=link) goes to the button bucket only and does
NOT also appear in paragraphs/headings/etc.
"""

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup, Tag


SECTION_SELECTORS = [
    "section.wp-block-group",
    "div.text-content",
    "div.custom_html_1-section",
    "div.uk-overlay-panel",
    "div.carousel-wrapper",
    "div.tmt-section",
]

HIDDEN_CLASSES = {
    "visuallyhidden",
    "visually-hidden",
    "sr-only",
    "screen-reader-text",
    "screen-reader-only",
}


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
        "all_links": collect_all_links(soup),
    }


def extract_sections(soup: BeautifulSoup) -> list:
    """Walk the document in order and pull every section-like container."""
    combined = ", ".join(SECTION_SELECTORS)
    seen_ids = set()
    out = []

    for el in soup.select(combined):
        key = id(el)
        if key in seen_ids:
            continue
        seen_ids.add(key)

        if el.find_parent(class_="map-newsletter"):
            continue
        cls = " ".join(el.get("class") or [])
        if any(x in cls for x in ("reviews", "banner", "contact")):
            continue

        out.append(_section_data(el))

    return out


def _section_data(el: Tag) -> dict:
    """
    Pull headings, paragraphs, buttons, list items from one section.

    Key rule: text inside a clickable goes to `buttons` only — never
    duplicated in paragraph/heading buckets.
    """

    # --- 1. Find all clickables and mark their text as off-limits ---
    clickable_selector = 'a, button, [role="button"], [role="link"]'
    clickable_els = el.select(clickable_selector)

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

    # --- 3. Collect headings/paragraphs/list-items, excluding clickable text ---
    def texts(tag_name: str) -> list:
        out = []
        for t in el.find_all(tag_name):
            if id(t) in off_limits:
                continue
            text_value = _text_excluding_clickables(t)
            if text_value:
                out.append(text_value)
        return out

    h1 = texts("h1")
    h2 = texts("h2")
    h3 = texts("h3")
    paragraphs = texts("p")
    list_items = texts("li")

    headings = h1 + h2 + h3
    raw_text = _clean_text(el.get_text(separator=" "))

    return {
        "headings": headings,
        "h1": h1,
        "h2": h2,
        "h3": h3,
        "paragraphs": paragraphs,
        "list_items": list_items,
        "buttons": buttons,
        "raw_text": raw_text,
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


def extract_restaurant_name(soup: BeautifulSoup) -> str:
    """
    Pull the restaurant name from the page metadata. Tries the cleanest
    sources first, falls back to the page <title>.
    """
    # Best: apple-mobile-web-app-title (name-only, no location suffix)
    apple = soup.find("meta", attrs={"name": "apple-mobile-web-app-title"})
    if apple and apple.get("content"):
        return apple["content"].strip()

    # Next: og:title — typically "Name - City, State"
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        return _strip_location_suffix(og["content"].strip())

    # Next: twitter:title
    tw = soup.find("meta", attrs={"name": "twitter:title"})
    if tw and tw.get("content"):
        return _strip_location_suffix(tw["content"].strip())

    # Fallback: <title>
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


def _is_visually_hidden(node) -> bool:
    """True if the element carries a known accessibility-hidden class."""
    if not isinstance(node, Tag):
        return False
    cls = node.get("class") or []
    return any(c in HIDDEN_CLASSES for c in cls)


def _text_excluding_clickables(node: Tag) -> str:
    """
    Return the text content of `node` with all <a>, <button>,
    [role=button], and [role=link] descendant text removed. Prevents
    link text from appearing twice (once in buttons, once in p/h*).
    """
    skip_ids = set()
    for clickable in node.select('a, button, [role="button"], [role="link"]'):
        for s in clickable.find_all(string=True):
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