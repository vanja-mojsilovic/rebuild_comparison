"""
Clone Comparison Tool — Python / FastAPI version.

Run:
    uvicorn main:app --reload

Then open http://localhost:8000 in your browser.
"""

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from scraper import scrape_page
from comparator import build_sections_view

app = FastAPI(title="Clone Comparison Tool")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Simple in-memory cache of recent comparisons.
# Key: (old_url, new_url) — Value: the view dict.
# When the user clicks Download, we look up the cached view instead of
# re-scraping. Cache survives until the server is restarted.
_CACHE: dict = {}
_CACHE_MAX = 20  # keep memory reasonable; drop oldest when exceeded


def _cache_put(key, value):
    _CACHE[key] = value
    while len(_CACHE) > _CACHE_MAX:
        # pop the oldest entry — dicts preserve insertion order in Py3.7+
        _CACHE.pop(next(iter(_CACHE)))


def _inline_css() -> str:
    """Read static/style.css from disk and return its content for inlining."""
    css_path = Path(__file__).parent / "static" / "style.css"
    return css_path.read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Render the form page."""
    return templates.TemplateResponse(request, "index.html")


@app.post("/compare", response_class=HTMLResponse)
def compare(
    request: Request,
    old_url: str = Form(...),
    new_url: str = Form(...),
):
    """Scrape both pages and render side-by-side."""
    try:
        old_data = scrape_page(old_url)
        new_data = scrape_page(new_url)
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "error": f"Failed to fetch one of the URLs: {e}",
                "old_url": old_url,
                "new_url": new_url,
            },
        )

    view = build_sections_view(old_data, new_data)
    _cache_put((old_url, new_url), view)

    return templates.TemplateResponse(
        request,
        "report.html",
        {
            "view": view,
            "old_url": old_url,
            "new_url": new_url,
        },
    )


@app.post("/download", response_class=Response)
def download(
    old_url: str = Form(...),
    new_url: str = Form(...),
):
    """
    Return a self-contained HTML file (CSS inlined) for download.
    Uses cached view data from the most recent /compare call for this
    URL pair, so no re-scraping happens.
    """
    view = _CACHE.get((old_url, new_url))
    if view is None:
        # Cache miss (server restarted, or user opened /download directly).
        # Re-scrape so the download still works.
        try:
            old_data = scrape_page(old_url)
            new_data = scrape_page(new_url)
            view = build_sections_view(old_data, new_data)
            _cache_put((old_url, new_url), view)
        except Exception as e:
            return Response(
                content=f"<h1>Download failed</h1><p>{e}</p>",
                media_type="text/html",
                status_code=500,
            )

    # Render the download-friendly template (CSS embedded inline)
    html = templates.get_template("report_download.html").render({
        "view": view,
        "old_url": old_url,
        "new_url": new_url,
        "inline_css": _inline_css(),
    })

    # Build a filename from the restaurant name when available
    restaurant = view.get("restaurant", {}).get("new") or view.get("restaurant", {}).get("old") or "comparison"
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in restaurant).strip()
    filename = f"{safe_name}-rebuild-comparison.html"

    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )