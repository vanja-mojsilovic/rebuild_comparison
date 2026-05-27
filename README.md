# Clone Comparison Tool — Python (Rebuild Mode)

A web-based tool for verifying that a rebuilt website is content-identical to its predecessor. Renders both sites with a real headless browser (Playwright), extracts text content, classifies sections by service type, and produces a side-by-side comparison report.

In **rebuild mode**, Old and New must match exactly with two known exceptions:

1. **Heading cascade.** Old's `<h1>` becomes New's `<h2>`; Old's `<h2>` becomes New's `<h3>`.
2. **URL shortening.** Old's link paths may have a location-slug prefix that is stripped on New (e.g. `/dobbs-ferry-westchester-hudson-social-about` → `/about`).

Anything else that differs is a regression.

## Setup (Windows + PowerShell + VS Code)

### 1. Open the folder in VS Code

```powershell
cd path\to\clone-comparison-py
code .
```

### 2. Create a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks the activation script, run this once first:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
playwright install chromium
```

(The second command downloads the headless Chromium browser that Playwright drives. ~150 MB, one-time.)

### 4. Run the app

```powershell
uvicorn main:app --reload
```

Then open **http://localhost:8000** in your browser.

## Using the tool

1. Enter the **Old URL** and **New URL**.
2. Leave the **URL prefix** field blank to auto-detect, or fill it in manually (e.g. `dobbs-ferry-westchester-hudson-social-`).
3. Click **Compare**.
4. The report opens in the same browser window. Green = match, orange = regression.

First comparison takes ~30 seconds (Playwright cold-starts the browser). Subsequent ones are faster.

## Project layout

```
clone-comparison-py/
├── main.py              FastAPI app — routes, request handling
├── scraper.py           Playwright-based renderer + content extractor
├── comparator.py        Rebuild-mode comparison logic
├── templates/
│   ├── index.html       Input form
│   └── report.html      Comparison results
├── static/
│   └── style.css        Green palette and table styles
└── requirements.txt
```

## VS Code tips

- Install the **Python** extension if you haven't already.
- Once the venv is created, VS Code will prompt to select it as the interpreter — click yes.
- Use `Ctrl+Shift+P` → "Terminal: Create New Terminal" to get a PowerShell terminal with the venv auto-activated.
- For debugging, create a `.vscode/launch.json` that runs `uvicorn main:app --reload` — `Ctrl+Shift+P` → "Debug: Add Configuration" → "Python: FastAPI".

## Known limits

- Pages that require login or have aggressive bot-blocking may fail to load.
- The 30-second Playwright timeout may not be enough for very slow sites — increase it in `scraper.py` if needed.
- Section classification uses the same keyword rules as the Apps Script tool (catering, parties, reservations, etc.). Sections that don't match any keyword fall into "other".
- The heading-shift rule currently checks Old H1 → New H2 and Old H2 → New H3. If your rebuild template moves H3s to H4 too, extend `compare_container` in `comparator.py`.