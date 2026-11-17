import asyncio
import re
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


app = FastAPI(title="COMPAMED Company Data Extractor")

# Same-origin requests work automatically. Wildcard also permits
# a separately hosted frontend if you use one later.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

EMAIL_RE = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.IGNORECASE
)

URL_RE = re.compile(
    r"https?://[^\s<>\"']+|(?:www\.)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s<>\"']*)?",
    re.IGNORECASE
)

BLOCKED_HOSTS = {
    "compamed-tradefair.com",
    "compamed.de",
    "messe-duesseldorf.de",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "youtube.com",
    "youtu.be",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "whatsapp.com",
}


class ExtractRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    click_extra: bool = False
    button_name: str = ""


def clean_url(value: str) -> str:
    value = value.strip().rstrip(".,;)]}")
    if not value:
        return ""
    if value.startswith("www."):
        value = "https://" + value
    if not value.startswith(("http://", "https://")):
        return ""
    return value


def is_blocked_url(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").lower()
        return any(
            host == blocked or host.endswith("." + blocked)
            for blocked in BLOCKED_HOSTS
        )
    except Exception:
        return True


def normalize_profile_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        return ""
    return value


async def click_visible_company_data(page):
    """Open the exhibitor's Company data tab/section."""

    labels = [
        page.get_by_role("tab", name=re.compile(r"company\s*data", re.I)),
        page.get_by_role("button", name=re.compile(r"company\s*data", re.I)),
        page.get_by_text(re.compile(r"^\s*company\s*data\s*$", re.I)),
    ]

    for locator in labels:
        try:
            target = locator.first
            if await target.count() and await target.is_visible():
                await target.click(timeout=5000)
                await page.wait_for_timeout(900)
                return True
        except Exception:
            continue

    return False


async def click_extra_button(page, button_name: str):
    """Click the optional user-specified button, if it exists."""

    name = button_name.strip()
    if not name:
        return False

    candidates = [
        page.get_by_role("button", name=re.compile(re.escape(name), re.I)),
        page.get_by_role("tab", name=re.compile(re.escape(name), re.I)),
        page.get_by_role("link", name=re.compile(re.escape(name), re.I)),
        page.get_by_text(re.compile(r"^\s*" + re.escape(name) + r"\s*$", re.I)),
    ]

    for locator in candidates:
        try:
            target = locator.first
            if await target.count() and await target.is_visible():
                await target.click(timeout=5000)
                await page.wait_for_timeout(700)
                return True
        except Exception:
            continue

    return False


async def read_visible_company_data(page):
    """Read visible dialog/popup content, then fall back to page content."""

    selectors = [
        '[role="dialog"]',
        '[aria-modal="true"]',
        '.modal.show',
        '.modal',
        '.dialog',
        '.popup',
    ]

    for selector in selectors:
        try:
            locator = page.locator(selector).last
            if await locator.count() and await locator.is_visible():
                text = await locator.inner_text(timeout=2500)
                if text and len(text.strip()) > 15:
                    return locator, text
        except Exception:
            pass

    return page.locator("body"), await page.locator("body").inner_text()


async def extract_from_profile(browser, profile_url, click_extra, button_name):
    result = {
        "profile": profile_url,
        "website": "",
        "email": "",
        "status": "Not found",
    }

    page = await browser.new_page()
    page.set_default_timeout(7000)

    try:
        response = await page.goto(
            profile_url,
            wait_until="domcontentloaded",
            timeout=25000
        )

        if response and response.status >= 400:
            result["status"] = f"Profile HTTP {response.status}"
            return result

        await page.wait_for_timeout(1200)

        # Optional extra button is clicked before Company data.
        if click_extra and button_name.strip():
            await click_extra_button(page, button_name)

        opened = await click_visible_company_data(page)

        if not opened:
            # Try once more after the page has settled.
            await page.wait_for_timeout(1000)
            opened = await click_visible_company_data(page)

        await page.wait_for_timeout(900)

        content_locator, visible_text = await read_visible_company_data(page)

        # Extract email addresses from visible Company Data content.
        emails = EMAIL_RE.findall(visible_text or "")
        if emails:
            result["email"] = emails[0].strip(".,;:")

        # Prefer actual external website links from the visible panel.
        try:
            anchors = content_locator.locator("a[href]")
            count = min(await anchors.count(), 150)

            for index in range(count):
                anchor = anchors.nth(index)
                href = (await anchor.get_attribute("href") or "").strip()
                label = (await anchor.inner_text().catch(lambda: "")) if False else ""

                if href.startswith("mailto:"):
                    if not result["email"]:
                        result["email"] = href[7:].split("?")[0].strip()
                    continue

                candidate = clean_url(href)
                if candidate and not is_blocked_url(candidate):
                    result["website"] = candidate
                    break
        except Exception:
            pass

        # Fallback: find a website URL written as visible text.
        if not result["website"]:
            for match in URL_RE.findall(visible_text or ""):
                candidate = clean_url(match)
                if candidate and not is_blocked_url(candidate):
                    result["website"] = candidate
                    break

        if result["website"] or result["email"]:
            result["status"] = "Found"
        elif opened:
            result["status"] = "Company data opened; no website/email found"
        else:
            result["status"] = "Company data tab not found"

    except PlaywrightTimeoutError:
        result["status"] = "Timed out"
    except Exception as exc:
        result["status"] = "Error: " + str(exc)[:160]
    finally:
        await page.close()

    return result


@app.get("/")
async def home():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "COMPAMED Data Extractor"}


@app.post("/api/extract")
async def extract(request: ExtractRequest):
    urls = []
    seen = set()

    for raw_url in request.urls:
        url = normalize_profile_url(raw_url)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    if not urls:
        raise HTTPException(status_code=400, detail="No valid profile links provided.")

    results = []

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            # Keep concurrency low to reduce memory use on the free Render plan.
            semaphore = asyncio.Semaphore(2)

            async def run_one(url):
                async with semaphore:
                    return await extract_from_profile(
                        browser,
                        url,
                        request.click_extra,
                        request.button_name
                    )

            results = await asyncio.gather(
                *(run_one(url) for url in urls)
            )

            await browser.close()

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="Browser startup/extraction failed: " + str(exc)[:250]
        )

    found = sum(
        1 for row in results
        if row["website"] or row["email"]
    )

    return {
        "processed": len(results),
        "found": found,
        "not_found": len(results) - found,
        "results": results
    }
