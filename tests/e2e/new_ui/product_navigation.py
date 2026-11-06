"""Navigate the served Product workspace through its visible controls."""

import re


def open_product_section(page, section="Product information"):
    page.get_by_role("button", name="Profile setup", exact=True).click()
    page.get_by_role("navigation", name="Profile setup sections").get_by_role("button", name=re.compile(section)).click()


def product_session_status(page):
    open_product_section(page, "Financials")
    details = page.get_by_text("Saved session details", exact=True)
    details.wait_for(timeout=30_000)
    if details.locator("..").get_attribute("open") is None:
        details.click()
    return page.get_by_role("region", name="Product onboarding").get_by_role("status")
