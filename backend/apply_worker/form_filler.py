"""Playwright-based form filler for ATS job application pages."""
import logging
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.config import settings

logger = logging.getLogger(__name__)

# Words in associated labels / name / id / aria that identify a resume upload
# (vs cover-letter, portfolio, profile photo). Case-insensitive.
_RESUME_KEYWORDS = re.compile(r"resume|cv|curriculum", re.I)

# Some ATSs gate the file type via the `accept` attribute; image/* only and
# common photo extensions are almost certainly NOT resumes.
_IMAGE_EXT_RE = re.compile(r"\.jpe?g|\.png|\.gif|\.webp", re.I)


def _accept_is_image_only(accept: str | None) -> bool:
    if not accept:
        return False
    if "image/*" in accept or "image/" in accept:
        return True
    return bool(_IMAGE_EXT_RE.search(accept))

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


async def attach_resume(page: Page, file_path: Path) -> bool:
    """Find the page's resume `<input type="file">` and set the file.

    Strategy:
      1. Skip any input whose `accept` is image-only (profile photos).
      2. Prefer inputs whose name / id / aria-label matches resume-keywords.
      3. Then inputs whose associated `<label>` text matches.
      4. Fall back to the first non-image file input.

    `set_input_files` bypasses Playwright's visibility checks, so hidden ATS
    inputs work transparently. Returns True if a file was attached, False if
    no suitable input was found.
    """
    if not file_path or not file_path.exists():
        logger.error(f"Resume path missing on disk: {file_path}")
        return False

    try:
        inputs = page.locator('input[type="file"]')
        count = await inputs.count()
    except Exception as e:
        logger.error(f"Failed to enumerate file inputs: {e}")
        return False

    if count == 0:
        logger.warning("No <input type=\"file\"> found on the page")
        return False

    # Pass 1 — semantic match on name / id / aria-label.
    for i in range(count):
        el = inputs.nth(i)
        try:
            if _accept_is_image_only(await el.get_attribute("accept")):
                continue
            blob = " ".join(filter(None, [
                await el.get_attribute("name") or "",
                await el.get_attribute("id") or "",
                await el.get_attribute("aria-label") or "",
            ]))
            if _RESUME_KEYWORDS.search(blob):
                await el.set_input_files(str(file_path))
                logger.info(f"Resume attached via name/id/aria match (input #{i})")
                return True
        except Exception as e:
            logger.debug(f"Pass-1 probe on input #{i} skipped: {e}")

    # Pass 2 — semantic match on associated <label>.
    for i in range(count):
        el = inputs.nth(i)
        try:
            if _accept_is_image_only(await el.get_attribute("accept")):
                continue
            label_text = ""
            el_id = await el.get_attribute("id") or ""
            if el_id:
                lbl = await page.query_selector(f'label[for="{el_id}"]')
                if lbl:
                    label_text = (await lbl.inner_text()) or ""
            if not label_text:
                wrap = await el.evaluate_handle('(e) => e.closest("label")')
                wrap_el = wrap.as_element() if wrap else None
                if wrap_el:
                    label_text = (await wrap_el.inner_text()) or ""
            if _RESUME_KEYWORDS.search(label_text):
                await el.set_input_files(str(file_path))
                logger.info(f"Resume attached via label match (input #{i})")
                return True
        except Exception as e:
            logger.debug(f"Pass-2 probe on input #{i} skipped: {e}")

    # Pass 3 — best-effort: first non-image file input.
    for i in range(count):
        el = inputs.nth(i)
        try:
            if _accept_is_image_only(await el.get_attribute("accept")):
                continue
            await el.set_input_files(str(file_path))
            logger.warning(
                f"Resume attached via fallback (first non-image input #{i})"
            )
            return True
        except Exception as e:
            logger.debug(f"Pass-3 probe on input #{i} skipped: {e}")

    logger.warning("No suitable resume `<input type=\"file\">` could be targeted")
    return False
