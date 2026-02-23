import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

from apify import Actor
from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from crawlee.router import Router

SDBULLION_HOST = 'sdbullion.com'
SEARCH_URL_TEMPLATE = 'https://sdbullion.com/catalogsearch/result/?q={query}'
CATEGORY_PATH_KEYWORDS = ['/gold/', '/silver/', '/platinum/', '/palladium/', '/copper/']
AVAILABILITY_STATES = ['In Stock', 'Out of Stock', 'Pre-Order', 'Sold Out', 'Coming Soon', 'Discontinued']
MAX_DESCRIPTION_LENGTH = 2000
SKIP_PATH_SEGMENTS = ['/about/', '/shipping/', '/contact/', '/faq/', '/policies/', '/blog/', '/customer/', '/checkout/', '/cart/']

# JS snippet to extract product cards from Magento 2 listing/search pages.
# Tries multiple selector strategies to find product name, price, and URL.
EXTRACT_LISTING_PRODUCTS_JS = '''() => {
    const products = [];
    const seen = new Set();

    // Strategy 1: Magento 2 product-item selectors
    document.querySelectorAll('.product-item, .product-item-info, .item.product.product-item').forEach(el => {
        const link = el.querySelector('a.product-item-link, a.product-item-photo, a[href]');
        const nameEl = el.querySelector('.product-item-link, .product-item-name, .product.name a, h2 a, h3 a');
        const priceEl = el.querySelector('.price-box .price, .price-wrapper .price, .price, [data-price-type="finalPrice"] .price');
        const imgEl = el.querySelector('img.product-image-photo, img[src]');
        if (link && (nameEl || link.title)) {
            const url = link.href;
            if (!seen.has(url)) {
                seen.add(url);
                products.push({
                    url: url,
                    name: nameEl ? nameEl.innerText.trim() : (link.title || '').trim(),
                    price: priceEl ? priceEl.innerText.trim() : null,
                    image: imgEl ? (imgEl.src || imgEl.dataset.src || null) : null
                });
            }
        }
    });

    // Strategy 2: Generic product grid/list fallbacks
    if (products.length === 0) {
        document.querySelectorAll('.products-grid .product-item, .products.list .product-item, [class*="product"] li, [class*="product-list"] > div').forEach(el => {
            const link = el.querySelector('a[href*="sdbullion.com"]');
            const nameEl = el.querySelector('h2, h3, h4, [class*="name"], [class*="title"], a[class*="link"]');
            const priceEl = el.querySelector('[class*="price"]');
            const imgEl = el.querySelector('img[src], img[data-src]');
            if (link && nameEl) {
                const url = link.href;
                if (!seen.has(url)) {
                    seen.add(url);
                    products.push({
                        url: url,
                        name: nameEl.innerText.trim(),
                        price: priceEl ? priceEl.innerText.trim() : null,
                        image: imgEl ? (imgEl.src || imgEl.dataset.src || null) : null
                    });
                }
            }
        });
    }

    // Strategy 3: Any product link with price nearby
    if (products.length === 0) {
        document.querySelectorAll('a[href*="sdbullion.com"]').forEach(link => {
            const url = link.href;
            const name = (link.innerText || link.title || '').trim();
            if (name && name.length > 5 && !seen.has(url) && !url.includes('/catalogsearch/') && !url.includes('/checkout/') && !url.includes('/customer/')) {
                const parent = link.closest('div, li, article, section');
                const priceEl = parent ? parent.querySelector('[class*="price"]') : null;
                if (priceEl) {
                    seen.add(url);
                    const imgEl = parent ? parent.querySelector('img[src], img[data-src]') : null;
                    products.push({
                        url: url,
                        name: name,
                        price: priceEl ? priceEl.innerText.trim() : null,
                        image: imgEl ? (imgEl.src || imgEl.dataset.src || null) : null
                    });
                }
            }
        });
    }

    return products;
}'''

products_scraped = 0
scraped_urls: set[str] = set()


def parse_price(price_str: str) -> float | None:
    """Extract numeric price from a string like '$5,120.96'."""
    if not price_str:
        return None
    match = re.search(r'\$?([\d,]+\.?\d*)', price_str)
    if match:
        try:
            return float(match.group(1).replace(',', ''))
        except ValueError:
            return None
    return None


def is_product_url(url: str) -> bool:
    """Check if a URL looks like a product page (not category, search, or informational)."""
    if not validate_url(url):
        return False
    if is_search_url(url):
        return False
    if any(skip in url for skip in SKIP_PATH_SEGMENTS):
        return False
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    if not path:
        return False
    # Skip file extensions (images, etc.)
    if '.' in path.rsplit('/', 1)[-1]:
        return False
    # SD Bullion product URLs can be single-segment (/2025-american-silver-eagle-coin)
    # or multi-segment (/silver/us-mint-american-silver-eagle-coins/silver-american-eagles-1-ounce)
    # If it's not a category URL, treat it as a product
    if not is_category_url(url):
        return True
    return False


def is_search_url(url: str) -> bool:
    """Determine if a URL is a search results page."""
    return '/catalogsearch/' in url or 'q=' in url


def is_category_url(url: str) -> bool:
    """Determine if a URL is a category/listing page based on path structure."""
    if is_search_url(url):
        return False
    parsed = urlparse(url)
    path = parsed.path.lower().strip('/')
    if not path:
        return True  # Homepage
    segments = [s for s in path.split('/') if s]
    # Top-level metal categories are listing pages: /silver, /gold, /platinum, etc.
    top_level_categories = ('gold', 'silver', 'platinum', 'palladium', 'copper', 'on-sale', 'new-arrivals', 'specials')
    if len(segments) == 1 and segments[0] in top_level_categories:
        return True
    # Two-segment category paths like /silver/silver-coins, /gold/gold-bars
    if len(segments) == 2 and segments[0] in top_level_categories:
        # If second segment looks like a subcategory (contains the metal name or generic listing terms)
        sub = segments[1]
        if any(cat in sub for cat in ('coin', 'bar', 'round', 'bullion', 'mint', 'eagle', 'maple', 'all-')):
            return True
    # Paths with 'inventory' or explicit category markers
    if 'inventory' in path:
        return True
    return False


def validate_url(url: str) -> bool:
    """Validate that a URL is well-formed and belongs to sdbullion.com."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        return parsed.scheme in ('http', 'https') and (host == SDBULLION_HOST or host == f'www.{SDBULLION_HOST}')
    except Exception:
        return False


async def dismiss_overlays(page) -> None:
    """Dismiss cookie consent banners and other overlays that block interaction."""
    dismiss_selectors = [
        'button:has-text("Accept")',
        'button:has-text("Allow all")',
        'button:has-text("Allow All")',
        'button:has-text("Got it")',
        'button:has-text("I agree")',
        'button:has-text("OK")',
        'button:has-text("Close")',
        '[class*="cookie"] button',
        '[class*="consent"] button',
        '[id*="cookie"] button',
        '[id*="consent"] button',
        '.onetrust-close-btn-handler',
        '#onetrust-accept-btn-handler',
        # Magento-specific overlay dismissals
        '.modals-overlay + .modal-popup button.action-close',
        '.modal-popup .action-close',
        'button.action-close',
    ]
    for selector in dismiss_selectors:
        try:
            btn = await page.query_selector(selector)
            if btn and await btn.is_visible():
                await btn.click()
                Actor.log.info(f"Dismissed overlay with selector: {selector}")
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def extract_products_from_listing(context: PlaywrightCrawlingContext, max_items: int, source_type: str) -> int:
    """Extract product data directly from a listing/search page. Returns count of products scraped."""
    global products_scraped

    listing_products = await context.page.evaluate(EXTRACT_LISTING_PRODUCTS_JS)

    if not listing_products:
        Actor.log.info(f"No products found via JS extraction on {source_type} page, will enqueue links as fallback")
        return 0

    Actor.log.info(f"Found {len(listing_products)} products on {source_type} page via JS extraction")

    # Log sample URLs for debugging classification
    sample_urls = [p.get('url', '') for p in listing_products[:3]]
    for sample_url in sample_urls:
        Actor.log.info(f"  Sample URL: {sample_url} -> is_product={is_product_url(sample_url)}, is_category={is_category_url(sample_url)}, is_search={is_search_url(sample_url)}")

    count = 0
    skipped_urls = []
    for product in listing_products:
        if products_scraped >= max_items:
            break

        url = product.get('url', '').rstrip('/')
        if not is_product_url(url):
            skipped_urls.append(url)
            continue
        if url in scraped_urls:
            continue
        scraped_urls.add(url)

        name = product.get('name', '')
        price_text = product.get('price')
        price_numeric = parse_price(price_text) if price_text and '$' in str(price_text) else None
        image_url = product.get('image')

        await Actor.push_data({
            'url': url,
            'name': name,
            'price': price_text if price_text and '$' in str(price_text) else None,
            'priceNumeric': price_numeric,
            'imageUrl': image_url,
            'sku': None,
            'availability': None,
            'description': None,
            'scrapedAt': datetime.now(timezone.utc).isoformat(),
        })

        products_scraped += 1
        count += 1
        Actor.log.info(f"Scraped {products_scraped}/{max_items} products (from {source_type} listing)")

    if skipped_urls:
        Actor.log.info(f"Skipped {len(skipped_urls)} non-product URLs, samples: {skipped_urls[:3]}")

    return count


async def main():
    global products_scraped

    async with Actor:
        actor_input = await Actor.get_input() or {}
        start_urls_input = actor_input.get("start_urls", [])
        search_terms = actor_input.get("search_terms", [])
        max_items = actor_input.get("max_items", 10)

        # Build URLs from search terms
        start_urls = []
        for term in search_terms:
            term = term.strip()
            if term:
                search_url = SEARCH_URL_TEMPLATE.format(query=quote_plus(term))
                start_urls.append(search_url)
                Actor.log.info(f"Added search term: '{term}' -> {search_url}")

        # Add explicit start URLs
        for item in start_urls_input:
            if isinstance(item, dict) and "url" in item:
                url = item["url"]
            elif isinstance(item, str):
                url = item
            else:
                Actor.log.warning(f"Skipping invalid start_urls entry: {item}")
                continue

            if validate_url(url):
                start_urls.append(url)
            else:
                Actor.log.warning(f"Skipping non-sdbullion URL: {url}")

        if not start_urls:
            default_term = "Silver coin"
            start_urls = [SEARCH_URL_TEMPLATE.format(query=quote_plus(default_term))]
            Actor.log.info(f"No input provided, defaulting to search: '{default_term}'")

        Actor.log.info(f"Starting SD Bullion Scraper with {len(start_urls)} start URLs, max_items={max_items}")

        router = Router[PlaywrightCrawlingContext]()

        @router.handler('PRODUCT')
        async def product_handler(context: PlaywrightCrawlingContext):
            global products_scraped
            if products_scraped >= max_items:
                return

            url = context.request.url.rstrip('/')
            if any(skip in url for skip in SKIP_PATH_SEGMENTS):
                Actor.log.info(f"Skipping informational URL: {url}")
                return
            if is_category_url(url):
                Actor.log.info(f"Skipping category URL in product handler: {url}")
                return
            if url in scraped_urls:
                Actor.log.info(f"Skipping duplicate product: {url}")
                return
            scraped_urls.add(url)

            Actor.log.info(f'Extracting product ({products_scraped + 1}/{max_items}): {url}')

            try:
                await context.page.wait_for_selector('h1', timeout=10000)
            except Exception as e:
                Actor.log.warning(f"Title not found for {url}: {e}")
                return

            name = await context.page.inner_text('h1')

            # Price extraction — Magento 2 price-box selectors
            price_text = None
            price_selectors = [
                '.price-box .price',
                '.product-info-price .price',
                '[data-price-type="finalPrice"] .price',
                '.price-wrapper .price',
                '.special-price .price',
                '.normal-price .price',
                'span.price',
                '.price',
            ]
            for selector in price_selectors:
                try:
                    el = await context.page.query_selector(selector)
                    if el:
                        text = await el.inner_text()
                        if text and '$' in text:
                            price_text = text.strip()
                            break
                except Exception:
                    continue

            price_numeric = parse_price(price_text)

            # Image extraction — Magento 2 gallery and og:image fallback
            image_url = await context.page.evaluate('''() => {
                const ogImage = document.querySelector('meta[property="og:image"]');
                if (ogImage && ogImage.content) return ogImage.content;
                const img = document.querySelector('.gallery-placeholder img, .fotorama__stage img, .product.media img, .product-image-photo, [class*="product"] img[src*="sdbullion"]');
                if (img) return img.src || img.dataset.src || null;
                return null;
            }''')

            # Availability detection
            availability = "Unknown"
            page_text = await context.page.content()
            for state in AVAILABILITY_STATES:
                if state in page_text:
                    availability = state
                    break

            # SKU extraction — Magento 2 typically shows SKU on product pages
            sku = await context.page.evaluate('''() => {
                // Magento 2 standard SKU display
                const skuEl = document.querySelector('[itemprop="sku"], .product.attribute.sku .value, .sku .value');
                if (skuEl) return skuEl.innerText.trim();
                // Try meta tag
                const metaSku = document.querySelector('meta[itemprop="sku"]');
                if (metaSku) return metaSku.content;
                // Try table rows
                const rows = Array.from(document.querySelectorAll('tr, .data-table tr'));
                const skuRow = rows.find(r => {
                    const text = r.innerText || '';
                    return text.includes('SKU') || text.includes('Product ID');
                });
                if (skuRow) {
                    const cells = skuRow.querySelectorAll('td');
                    return cells.length > 1 ? cells[1].innerText.trim() : null;
                }
                return null;
            }''')

            # Description extraction
            description = None
            try:
                desc_el = await context.page.query_selector('.product.attribute.description .value, .product.info.detailed .description .value, #description .value, [itemprop="description"]')
                if not desc_el:
                    # Try clicking the description tab first (Magento 2 tabs)
                    desc_tab = await context.page.query_selector('a[href="#description"], a:has-text("Description"), [data-role="collapsible"]:has-text("Description")')
                    if desc_tab:
                        await desc_tab.click()
                        await context.page.wait_for_timeout(500)
                    desc_el = await context.page.query_selector('.product.attribute.description .value, .description .value, [itemprop="description"]')

                if desc_el:
                    raw = await desc_el.inner_text()
                    if raw:
                        description = raw.strip()[:MAX_DESCRIPTION_LENGTH]
            except Exception as e:
                Actor.log.warning(f'Error extracting description for {url}: {e}')

            await Actor.push_data({
                'url': url,
                'name': name.strip() if name else None,
                'price': price_text,
                'priceNumeric': price_numeric,
                'imageUrl': image_url,
                'sku': sku.strip() if sku else None,
                'availability': availability,
                'description': description,
                'scrapedAt': datetime.now(timezone.utc).isoformat(),
            })

            products_scraped += 1
            Actor.log.info(f"Scraped {products_scraped}/{max_items} products")

        @router.handler('SEARCH')
        async def search_handler(context: PlaywrightCrawlingContext):
            if products_scraped >= max_items:
                return

            url = context.request.url
            Actor.log.info(f'Processing search results: {url}')

            # Dismiss cookie consent / overlays that may block rendering
            await dismiss_overlays(context.page)

            # Magento 2 renders search results server-side, wait for product grid
            magento_selectors = '.products-grid, .products.list, .product-items, .search.results'
            found_grid = False
            try:
                await context.page.wait_for_selector(magento_selectors, timeout=15000)
                found_grid = True
            except Exception:
                Actor.log.info("Magento product grid not found on first attempt, scrolling and waiting...")
                await context.page.evaluate('window.scrollBy(0, 500)')
                await context.page.wait_for_timeout(3000)
                try:
                    await context.page.wait_for_selector(magento_selectors, timeout=10000)
                    found_grid = True
                except Exception:
                    Actor.log.info("Magento product grid still not found after scroll, trying with longer wait...")
                    await context.page.wait_for_timeout(5000)

            if found_grid:
                Actor.log.info("Magento product grid detected")

            # Try to extract products directly from the listing page
            count = await extract_products_from_listing(context, max_items, 'search')

            if count == 0:
                # Log what's actually on the page for debugging
                try:
                    body_text = await context.page.evaluate('() => document.body.innerText.substring(0, 500)')
                    Actor.log.info(f"Page body preview: {body_text}")
                    link_count = await context.page.evaluate('''() => {
                        const links = document.querySelectorAll('a[href*="sdbullion.com"]');
                        return { total: links.length, samples: Array.from(links).slice(0, 5).map(a => a.href) };
                    }''')
                    Actor.log.info(f"Links on page: {link_count}")
                except Exception:
                    pass

            # Enqueue search pagination if we still need more products
            if products_scraped < max_items:
                await context.enqueue_links(
                    selector='.pages a.next, .pages-items a, [class*="pagination"] a, a.action.next',
                    label='SEARCH',
                )

        @router.handler('CATEGORY')
        async def category_handler(context: PlaywrightCrawlingContext):
            if products_scraped >= max_items:
                return

            url = context.request.url
            Actor.log.info(f'Processing category: {url}')

            await dismiss_overlays(context.page)

            magento_selectors = '.products-grid, .products.list, .product-items'
            try:
                await context.page.wait_for_selector(magento_selectors, timeout=15000)
            except Exception:
                Actor.log.info("Magento product grid not found on category, scrolling and waiting...")
                await context.page.evaluate('window.scrollBy(0, 500)')
                await context.page.wait_for_timeout(5000)

            # Try to extract products directly from the listing page
            count = await extract_products_from_listing(context, max_items, 'category')

            if count == 0:
                Actor.log.info("No products extracted from category listing via JS")
                try:
                    link_count = await context.page.evaluate('''() => {
                        const links = document.querySelectorAll('a[href*="sdbullion.com"]');
                        return { total: links.length, samples: Array.from(links).slice(0, 5).map(a => a.href) };
                    }''')
                    Actor.log.info(f"Links on page: {link_count}")
                except Exception:
                    pass

            # Enqueue category pagination if we still need more products
            if products_scraped < max_items:
                await context.enqueue_links(
                    selector='.pages a.next, .pages-items a, [class*="pagination"] a, a.action.next',
                    label='CATEGORY',
                )

        @router.default_handler
        async def default_handler(context: PlaywrightCrawlingContext):
            url = context.request.url
            if is_search_url(url):
                return await search_handler(context)
            elif is_category_url(url):
                return await category_handler(context)
            else:
                return await product_handler(context)

        concurrency_settings = ConcurrencySettings(
            max_concurrency=5,
            min_concurrency=2,
            desired_concurrency=3,
        )

        proxy_input = actor_input.get('proxyConfiguration')
        if proxy_input:
            proxy_configuration = await Actor.create_proxy_configuration(
                actor_proxy_input=proxy_input,
            )
        else:
            # Default: use Apify proxy to avoid blocking
            Actor.log.info("No proxy configuration provided, using default Apify proxy with residential group")
            proxy_configuration = await Actor.create_proxy_configuration(
                groups=['RESIDENTIAL'],
            )

        crawler = PlaywrightCrawler(
            request_handler=router,
            concurrency_settings=concurrency_settings,
            max_requests_per_crawl=max_items * 5,
            browser_type='chromium',
            headless=True,
            request_handler_timeout=timedelta(seconds=60),
            proxy_configuration=proxy_configuration,
        )

        initial_requests = []
        for url in start_urls:
            if is_search_url(url):
                label = 'SEARCH'
            elif is_category_url(url):
                label = 'CATEGORY'
            else:
                label = 'PRODUCT'
            initial_requests.append(Request.from_url(url=url, label=label))

        await crawler.run(initial_requests)
        Actor.log.info(f'Scraping completed. Total products scraped: {products_scraped}')

if __name__ == "__main__":
    asyncio.run(main())
