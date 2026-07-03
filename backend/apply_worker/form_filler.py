"""Playwright-based form filler for ATS job application pages."""
import logging
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.config import settings

logger = logging.getLogger(__name__)


async def extract_form_fields(page: Page) -> list[dict]:
    """Extract all visible form field labels and their input elements.

    Returns list of dicts: {label, selector, input_type}
    """
    fields = []
    labels = await page.query_selector_all("label")

    for label_el in labels:
        label_text = (await label_el.inner_text()).strip()
        if not label_text:
            continue

        # Try for= attribute first
        for_attr = await label_el.get_attribute("for")
        if for_attr:
            input_el = await page.query_selector(f"#{for_attr}")
        else:
            input_el = await label_el.query_selector("input, textarea, select")
            if not input_el:
                input_el = await page.evaluate_handle(
                    "(el) => el.nextElementSibling", label_el
                )
                tag = await page.evaluate("(el) => el ? el.tagName : null", input_el)
                if tag not in ("INPUT", "TEXTAREA", "SELECT"):
                    input_el = None

        if not input_el:
            continue

        input_type = await page.evaluate(
            "(el) => el.type || el.tagName.toLowerCase()", input_el
        )
        selector = await page.evaluate(
            "(el) => { if (el.id) return '#' + el.id; if (el.name) return `[name='${el.name}']`; return null; }",
            input_el,
        )

        if selector:
            fields.append({
                "label": label_text,
                "selector": selector,
                "input_type": input_type,
            })

    logger.debug(f"Extracted {len(fields)} form fields")
    return fields


async def fill_field(page: Page, field: dict, answer: str) -> None:
    """Fill a single form field with the given answer."""
    selector = field["selector"]
    input_type = field.get("input_type", "text")

    if input_type == "select":
        await page.select_option(selector, label=answer)
    elif input_type in ("checkbox", "radio"):
        lower = answer.lower()
        if lower in ("yes", "true", "1"):
            await page.check(selector)
        else:
            await page.uncheck(selector)
    else:
        await page.fill(selector, answer)


async def take_screenshot(page: Page, job_id: str) -> str:
    """Save screenshot and return relative URL path (e.g. screenshots/{job_id}.png)."""
    screenshot_dir = Path(settings.apply_worker_screenshot_dir)
    screenshot_dir.mkdir(exist_ok=True)
    filename = f"{job_id}.png"
    path = screenshot_dir / filename
    await page.screenshot(path=str(path), full_page=True)
    # Return relative URL path so StaticFiles can serve it
    return f"screenshots/{filename}"
