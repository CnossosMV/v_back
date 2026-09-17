import asyncio
import base64
import os
from playwright.async_api import async_playwright
import logging

logger = logging.getLogger(__name__)


class QRCodeExtractor:
    def __init__(self, evolution_url: str, api_key: str):
        self.evolution_url = evolution_url
        self.api_key = api_key
        self.manager_url = f"{evolution_url}/manager"

    async def extract_qr_code(self, instance_name: str) -> str:
        """
        Extract QR code from Evolution Manager using Playwright
        Returns base64 encoded QR code image or empty string if not found
        """
        try:
            async with async_playwright() as p:
                # Launch browser in headless mode
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )

                context = await browser.new_context()
                page = await context.new_page()

                # Navigate to Evolution Manager
                logger.info(f"Navigating to Evolution Manager: {self.manager_url}")
                await page.goto(self.manager_url, wait_until='networkidle', timeout=30000)

                # Wait for the page to load completely
                await page.wait_for_timeout(3000)

                # Look for instance in the list or try to find QR code directly
                try:
                    # Try to find the instance by name
                    instance_selector = f"text='{instance_name}'"
                    await page.wait_for_selector(instance_selector, timeout=10000)
                    logger.info(f"Found instance {instance_name}")

                    # Click on the instance to open details
                    await page.click(instance_selector)
                    await page.wait_for_timeout(2000)

                except Exception as e:
                    logger.warning(f"Could not find instance by name: {e}")
                    # Continue anyway, might already be on the right page

                # Look for QR code image - try different possible selectors
                qr_selectors = [
                    'img[alt*="QR"]',
                    'img[src*="qr"]',
                    'img[src*="base64"]',
                    'canvas',
                    '[class*="qr"]',
                    '[data-testid*="qr"]'
                ]

                qr_code_base64 = ""

                for selector in qr_selectors:
                    try:
                        logger.info(f"Trying selector: {selector}")
                        qr_element = await page.wait_for_selector(selector, timeout=5000)

                        if qr_element:
                            # Get the src attribute if it's an img
                            if selector.startswith('img'):
                                src = await qr_element.get_attribute('src')
                                if src and ('base64' in src or 'data:image' in src):
                                    # Extract base64 from data URL
                                    if 'base64,' in src:
                                        qr_code_base64 = src.split('base64,')[1]
                                        logger.info("Found QR code in img src")
                                        break

                            # If it's a canvas, take a screenshot
                            elif selector == 'canvas':
                                screenshot = await qr_element.screenshot()
                                qr_code_base64 = base64.b64encode(screenshot).decode()
                                logger.info("Found QR code in canvas")
                                break

                    except Exception as e:
                        logger.debug(f"Selector {selector} failed: {e}")
                        continue

                # If no QR code found in elements, try taking a screenshot of potential QR area
                if not qr_code_base64:
                    try:
                        # Look for any element that might contain a QR code
                        qr_containers = await page.query_selector_all('[class*="qr"], [id*="qr"], .qr-code, #qrcode')

                        if qr_containers:
                            screenshot = await qr_containers[0].screenshot()
                            qr_code_base64 = base64.b64encode(screenshot).decode()
                            logger.info("Found QR code by screenshot")
                        else:
                            # Last resort - screenshot a central area of the page
                            logger.info("Taking screenshot of central area as last resort")
                            screenshot = await page.screenshot(clip={'x': 100, 'y': 100, 'width': 400, 'height': 400})
                            qr_code_base64 = base64.b64encode(screenshot).decode()

                    except Exception as e:
                        logger.error(f"Screenshot fallback failed: {e}")

                await browser.close()

                if qr_code_base64:
                    logger.info(f"Successfully extracted QR code (length: {len(qr_code_base64)})")
                else:
                    logger.warning("No QR code found")

                return qr_code_base64

        except Exception as e:
            logger.error(f"QR extraction failed: {e}")
            return ""

    async def wait_for_qr_code(self, instance_name: str, max_attempts: int = 10, delay: int = 3) -> str:
        """
        Wait for QR code to appear, trying multiple times
        """
        for attempt in range(max_attempts):
            logger.info(f"QR code extraction attempt {attempt + 1}/{max_attempts}")

            qr_code = await self.extract_qr_code(instance_name)
            if qr_code:
                return qr_code

            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)

        logger.warning(f"QR code not found after {max_attempts} attempts")
        return ""


# Singleton instance
_qr_extractor = None


def get_qr_extractor() -> QRCodeExtractor:
    global _qr_extractor
    if _qr_extractor is None:
        evolution_url = os.getenv("EVOLUTION_API_URL", "http://localhost:8003")
        api_key = os.getenv("OPENAI_API_KEY")
        _qr_extractor = QRCodeExtractor(evolution_url, api_key)
    return _qr_extractor
