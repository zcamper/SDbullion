import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlparse

from apify import Actor
from bs4 import BeautifulSoup
from curl_cffi.requests import Session

SDBULLION_HOST = 'sdbullion.com'
SDBULLION_HOSTS = {'sdbullion.com', 'www.sdbullion.com'}
SEARCH_URL_TEMPLATE = 'https://www.sdbullion.com/catalogsearch/result/?q={query}'
CATEGORY_PATH_KEYWORDS = ['/gold/', '/silver/', '/platinum/', '/palladium/', '/copper/']
AVAILABILITY_STATES = ['In Stock', 'Out of Stock', 'Pre-Order', 'Sold Out', 'Coming Soon', 'Discontinued']
MAX_DESCRIPTION_LENGTH = 2000
SKIP_PATH_SEGMENTS = ['/about/', '/shipping/', '/contact/', '/faq/', '/policies/', '/blog/', '/customer/', '/checkout/', '/cart/']

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


def validate_url(url: str) -> bool:
    """Validate that a URL is well-formed and belongs to sdbullion.com."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        return parsed.scheme in ('http', 'https') and host in SDBULLION_HOSTS
    except Exception:
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
    top_level_categories = ('gold', 'silver', 'platinum', 'palladium', 'copper', 'on-sale', 'new-arrivals', 'specials')
    if len(segments) == 1 and segments[0] in top_level_categories:
        return True
    if len(segments) == 2 and segments[0] in top_level_categories:
        sub = segments[1]
        if any(cat in sub for cat in ('coin', 'bar', 'round', 'bullion', 'mint', 'eagle', 'maple', 'all-')):
            return True
    if 'inventory' in path:
        return True
    return False


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
    if '.' in path.rsplit('/', 1)[-1]:
        return False
    if not is_category_url(url):
        return True
    return False


def extract_listing_products(html: str, base_url: str) -> list[dict]:
    """Extract products from a Magento 2 listing page HTML."""
    soup = BeautifulSoup(html, 'html.parser')
    products = []
    seen = set()

    # Strategy 1: Magento 2 product-item selectors
    for item in soup.select('.product-item, .product-item-info'):
        link_el = (
            item.select_one('a.product-item-link')
            or item.select_one('a.product-item-photo')
            or item.select_one('a[href]')
        )
        if not link_el:
            continue

        url = urljoin(base_url, link_el.get('href', ''))
        if url in seen:
            continue
        seen.add(url)

        name_el = item.select_one('.product-item-link, .product-item-name a, h2 a, h3 a')
        name = name_el.get_text(strip=True) if name_el else (link_el.get('title', '') or link_el.get_text(strip=True))

        price_el = item.select_one('.price-box .price, [data-price-type="finalPrice"] .price, .price-wrapper .price, .price')
        price = price_el.get_text(strip=True) if price_el else None

        img_el = item.select_one('img.product-image-photo, img[src]')
        image = None
        if img_el:
            image = img_el.get('src') or img_el.get('data-src')

        if name:
            products.append({'url': url, 'name': name, 'price': price, 'image': image})

    # Strategy 2: Broader product grid fallback
    if not products:
        for item in soup.select('.products-grid li, .products.list .item, ol.product-items > li'):
            link_el = item.select_one('a[href]')
            if not link_el:
                continue
            url = urljoin(base_url, link_el.get('href', ''))
            if url in seen:
                continue
            seen.add(url)

            name = link_el.get_text(strip=True) or link_el.get('title', '')
            price_el = item.select_one('[class*="price"]')
            price = price_el.get_text(strip=True) if price_el else None

            img_el = item.select_one('img[src]')
            image = img_el.get('src') if img_el else None

            if name and len(name) > 3:
                products.append({'url': url, 'name': name, 'price': price, 'image': image})

    return products


def extract_product_details(html: str) -> dict:
    """Extract product details from a Magento 2 product page HTML."""
    soup = BeautifulSoup(html, 'html.parser')

    # Name
    h1 = soup.select_one('h1')
    name = h1.get_text(strip=True) if h1 else None

    # Price
    price_el = soup.select_one(
        '.price-box .price, .product-info-price .price, '
        '[data-price-type="finalPrice"] .price, .special-price .price, '
        '.normal-price .price, span.price'
    )
    price_text = price_el.get_text(strip=True) if price_el else None

    # Image — og:image first, then product gallery
    og_image = soup.select_one('meta[property="og:image"]')
    image_url = og_image.get('content') if og_image else None
    if not image_url:
        img_el = soup.select_one('.gallery-placeholder img, .fotorama__stage img, .product-image-photo')
        image_url = img_el.get('src') if img_el else None

    # SKU
    sku_el = soup.select_one('[itemprop="sku"], .product.attribute.sku .value, .sku .value')
    sku = sku_el.get_text(strip=True) if sku_el else None
    if not sku:
        meta_sku = soup.select_one('meta[itemprop="sku"]')
        sku = meta_sku.get('content') if meta_sku else None

    # Availability
    availability = "Unknown"
    page_text = soup.get_text()
    for state in AVAILABILITY_STATES:
        if state in page_text:
            availability = state
            break

    # Description
    desc_el = soup.select_one(
        '.product.attribute.description .value, '
        '[itemprop="description"], #description .value'
    )
    description = desc_el.get_text(strip=True)[:MAX_DESCRIPTION_LENGTH] if desc_el else None

    return {
        'name': name,
        'price': price_text if price_text and '$' in str(price_text) else None,
        'priceNumeric': parse_price(price_text) if price_text else None,
        'imageUrl': image_url,
        'sku': sku,
        'availability': availability,
        'description': description,
    }


def get_next_page_url(html: str, base_url: str) -> str | None:
    """Get the next page URL from Magento 2 pagination."""
    soup = BeautifulSoup(html, 'html.parser')
    next_link = soup.select_one('.pages a.next, a.action.next, .pages-items li.current + li a')
    if next_link:
        return urljoin(base_url, next_link.get('href', ''))
    return None


async def scrape_listing(http: Session, url: str, proxies: dict, max_items: int) -> None:
    """Scrape a search or category listing page and follow pagination."""
    global products_scraped

    page_num = 1
    current_url = url

    while current_url and products_scraped < max_items:
        Actor.log.info(f"Fetching listing page {page_num}: {current_url}")

        try:
            response = http.get(current_url, proxies=proxies, timeout=30)
            Actor.log.info(f"Listing response: status={response.status_code}, length={len(response.text)}")
        except Exception as e:
            Actor.log.error(f"Failed to fetch listing {current_url}: {e}")
            break

        if response.status_code != 200:
            Actor.log.warning(f"Non-200 status ({response.status_code}) for listing {current_url}")
            Actor.log.info(f"Response preview: {response.text[:500]}")
            break

        products = extract_listing_products(response.text, current_url)
        Actor.log.info(f"Found {len(products)} products on listing page {page_num}")

        # Log sample URLs for debugging
        for p in products[:3]:
            sample_url = p['url']
            Actor.log.info(f"  Sample: {sample_url} -> is_product={is_product_url(sample_url)}")

        skipped = 0
        for product in products:
            if products_scraped >= max_items:
                break

            prod_url = product['url'].rstrip('/')
            if not is_product_url(prod_url):
                skipped += 1
                continue
            if prod_url in scraped_urls:
                continue
            scraped_urls.add(prod_url)

            price_text = product.get('price')
            await Actor.push_data({
                'url': prod_url,
                'name': product.get('name', ''),
                'price': price_text if price_text and '$' in str(price_text) else None,
                'priceNumeric': parse_price(price_text) if price_text else None,
                'imageUrl': product.get('image'),
                'sku': None,
                'availability': None,
                'description': None,
                'scrapedAt': datetime.now(timezone.utc).isoformat(),
            })

            products_scraped += 1
            Actor.log.info(f"Scraped {products_scraped}/{max_items} products (from listing)")

        if skipped:
            Actor.log.info(f"Skipped {skipped} non-product URLs")

        # Check for next page
        next_url = get_next_page_url(response.text, current_url)
        if next_url and next_url != current_url:
            current_url = next_url
            page_num += 1
        else:
            break


async def scrape_product(http: Session, url: str, proxies: dict, max_items: int) -> None:
    """Scrape a single product page."""
    global products_scraped
    if products_scraped >= max_items:
        return

    url = url.rstrip('/')
    if url in scraped_urls:
        return
    scraped_urls.add(url)

    Actor.log.info(f"Fetching product ({products_scraped + 1}/{max_items}): {url}")

    try:
        response = http.get(url, proxies=proxies, timeout=30)
    except Exception as e:
        Actor.log.error(f"Failed to fetch product {url}: {e}")
        return

    if response.status_code != 200:
        Actor.log.warning(f"Non-200 status ({response.status_code}) for product {url}")
        return

    details = extract_product_details(response.text)

    await Actor.push_data({
        'url': url,
        'name': details['name'],
        'price': details['price'],
        'priceNumeric': details['priceNumeric'],
        'imageUrl': details['imageUrl'],
        'sku': details['sku'],
        'availability': details['availability'],
        'description': details['description'],
        'scrapedAt': datetime.now(timezone.utc).isoformat(),
    })

    products_scraped += 1
    Actor.log.info(f"Scraped {products_scraped}/{max_items} products")


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

        # Set up proxy — SD Bullion WAF requires residential proxies
        proxy_input = actor_input.get('proxyConfiguration')
        if proxy_input and proxy_input.get('apifyProxyGroups'):
            Actor.log.info(f"Using user-provided proxy config: {proxy_input}")
            proxy_configuration = await Actor.create_proxy_configuration(
                actor_proxy_input=proxy_input,
            )
        else:
            Actor.log.info("Forcing RESIDENTIAL proxy with US country (required for SD Bullion WAF)")
            proxy_configuration = await Actor.create_proxy_configuration(
                actor_proxy_input={
                    'useApifyProxy': True,
                    'apifyProxyGroups': ['RESIDENTIAL'],
                    'apifyProxyCountry': 'US',
                },
            )

        proxy_url = await proxy_configuration.new_url()
        masked = re.sub(r'://([^:]+):([^@]+)@', r'://\1:***@', proxy_url or '')
        Actor.log.info(f"Proxy URL (masked): {masked}")
        proxies = {"http": proxy_url, "https": proxy_url}

        # Create HTTP session with Chrome TLS fingerprint impersonation.
        # This bypasses WAF TLS fingerprinting that blocks Playwright browsers.
        http = Session(impersonate="chrome110")
        Actor.log.info("Created curl_cffi session with chrome110 TLS impersonation")

        # Verify proxy connectivity
        try:
            test_resp = http.get("https://httpbin.org/ip", proxies=proxies, timeout=10)
            Actor.log.info(f"Proxy verification — IP: {test_resp.text.strip()}")
        except Exception as e:
            Actor.log.warning(f"Proxy verification failed: {e}")

        # Warm up session: visit homepage first to establish cookies/session.
        # Many WAFs require an initial visit to set tracking cookies before
        # allowing access to deeper pages like search results.
        Actor.log.info("Warming up session by visiting homepage...")
        try:
            home_resp = http.get("https://www.sdbullion.com/", proxies=proxies, timeout=30)
            Actor.log.info(f"Homepage response: status={home_resp.status_code}, length={len(home_resp.text)}, cookies={len(http.cookies)}")
            if http.cookies:
                cookie_names = [c.name for c in http.cookies]
                Actor.log.info(f"Cookies received: {cookie_names}")
            # Log what headers curl_cffi actually sends
            debug_resp = http.get("https://httpbin.org/headers", proxies=proxies, timeout=10)
            Actor.log.info(f"Request headers being sent: {debug_resp.text.strip()[:500]}")
        except Exception as e:
            Actor.log.warning(f"Homepage warm-up failed: {e}")

        # Process start URLs
        for url in start_urls:
            if products_scraped >= max_items:
                break

            if is_search_url(url) or is_category_url(url):
                await scrape_listing(http, url, proxies, max_items)
            elif is_product_url(url):
                await scrape_product(http, url, proxies, max_items)
            else:
                Actor.log.warning(f"Could not classify URL, trying as listing: {url}")
                await scrape_listing(http, url, proxies, max_items)

        Actor.log.info(f'Scraping completed. Total products scraped: {products_scraped}')


if __name__ == "__main__":
    asyncio.run(main())
