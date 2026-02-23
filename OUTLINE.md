# SD Bullion Scraper — Build Outline

Replicating the JM Bullion scraper pattern for sdbullion.com.

---

## Target Data (per product)

| Field | Description |
|-------|-------------|
| `url` | Product page URL |
| `name` | Product name from `<h1>` |
| `price` | Display price (e.g. "$34.99") |
| `priceNumeric` | Parsed float for sorting/calculations |
| `imageUrl` | Product image URL (from listing card or `og:image` fallback) |
| `sku` | Product ID / SKU from specs table |
| `availability` | Stock status (In Stock, Out of Stock, Pre-Order, etc.) |
| `description` | Specs/description text (max 2000 chars) |
| `scrapedAt` | ISO 8601 timestamp |

---

## Project Structure

```
SDbullion/
├── .actor/
│   ├── input_schema.json    # Apify input UI definition
│   └── dataset_schema.json  # Output field definitions
├── actor.json               # Actor metadata (name, version, dataset views)
├── Dockerfile               # Base image: apify/actor-python-playwright:3.10
├── requirements.txt         # apify + crawlee[playwright]
└── main.py                  # All scraping logic
```

---

## Step-by-Step Build Process

### Step 1: Scaffold the Apify Actor

1. Create `Dockerfile` — use `apify/actor-python-playwright:3.10` base image
2. Create `requirements.txt` — `apify` and `crawlee[playwright]`
3. Create `actor.json` — actor name, title, description, dataset table view
4. Create `.actor/input_schema.json` — input fields (see Step 2)
5. Create `.actor/dataset_schema.json` — output field definitions matching target data above

### Step 2: Define Input Schema

Fields to include:
- **search_terms** — `stringList` editor, e.g. ["Silver coin", "gold eagles"]
- **start_urls** — `requestListSources` editor, optional direct URLs
- **max_items** — `number` editor, default 5, min 1, max 1000
- **proxyConfiguration** — `proxy` editor (built-in Apify proxy picker UI)
  - Default: `{ "useApifyProxy": true }`
  - **Use `"editor": "proxy"` NOT a checkbox + string list** (lesson learned from JM Bullion)

### Step 3: Investigate SD Bullion's Site Structure

Before writing scraping code, manually inspect sdbullion.com to determine:

1. **Search URL pattern** — What does `sdbullion.com/search?q=...` look like? Or do they use a different search path?
2. **Product listing rendering** — Is it server-rendered HTML or dynamic JS (like JM Bullion's SearchSpring)?
   - Check for frameworks: SearchSpring, Algolia, custom React/Vue, etc.
   - Inspect the DOM for product card selectors (class names, data attributes)
3. **Product URL pattern** — What do individual product URLs look like?
   - Single-segment slug? (`/1-oz-silver-eagle/`)
   - Multi-segment? (`/silver/coins/1-oz-silver-eagle/`)
4. **Category URL pattern** — How to distinguish category pages from product pages
5. **Product page layout** — Where are these fields in the DOM?
   - Name: `<h1>` tag
   - Price: class names containing "price"
   - Image: main product image selector, or `og:image` meta tag
   - SKU: specs table, `<tr>` with "SKU" or "Product ID"
   - Availability: text like "In Stock", "Out of Stock" in page content
   - Description: specs section, tab content, etc.
6. **Anti-bot measures** — Cookie consent banners, Cloudflare, geo-blocking

### Step 4: Build URL Classification Functions

Based on Step 3 findings, create:

```python
SDBULLION_HOST = 'www.sdbullion.com'
SEARCH_URL_TEMPLATE = 'https://www.sdbullion.com/...?q={query}'  # Adjust to actual pattern

def validate_url(url) -> bool     # Must be sdbullion.com
def is_search_url(url) -> bool    # Matches search page pattern
def is_category_url(url) -> bool  # Matches category/listing pages
def is_product_url(url) -> bool   # Everything else that's valid
```

**Key lesson from JM Bullion:** Don't use trailing `/` alone to classify URLs. Use path segment analysis — count segments, check against known category slugs.

### Step 5: Build JS Extraction Snippet

Create `EXTRACT_LISTING_PRODUCTS_JS` tailored to SD Bullion's DOM:

- Inspect the product card elements on search/category pages
- Find selectors for: product link, name, price, image
- Use multiple fallback strategies (site-specific selectors first, then generic)
- Extract `{ url, name, price, image }` per product
- Deduplicate within the page using a `Set`

### Step 6: Build the Main Scraper (main.py)

Architecture (same pattern as JM Bullion):

```
main()
├── Parse input (search_terms, start_urls, max_items, proxyConfiguration)
├── Build start URLs from search terms
├── Set up Router with handlers:
│   ├── SEARCH handler  — dismiss overlays, wait for results, extract from listing
│   ├── CATEGORY handler — same pattern as search
│   ├── PRODUCT handler  — extract full details from individual product page
│   └── default handler  — classify URL and route to correct handler
├── Configure proxy (with RESIDENTIAL fallback)
├── Create PlaywrightCrawler
└── Run crawler with labeled initial requests
```

Key components:

1. **Global state** — `products_scraped` counter + `scraped_urls` set for deduplication
2. **`dismiss_overlays()`** — Click away cookie/consent banners before extraction
3. **`extract_products_from_listing()`** — Run JS snippet, filter by `is_product_url`, check duplicates, push to dataset
4. **Search/Category handlers** — Dismiss overlays, wait for dynamic content, extract, enqueue pagination
5. **Product handler** — Wait for `<h1>`, extract name/price/image/availability/SKU/description, push to dataset
6. **Proxy config** — Use `actor_proxy_input` from UI, fall back to `RESIDENTIAL` group

### Step 7: Handle Site-Specific Quirks

Things that caused issues on JM Bullion (anticipate similar on SD Bullion):

| Issue | Solution |
|-------|----------|
| Dynamic JS-rendered results | Wait for specific selectors, scroll to trigger lazy load, retry with longer waits |
| Cookie consent banners | `dismiss_overlays()` runs before extraction on every listing page |
| 403 blocking with datacenter proxies | Default to RESIDENTIAL proxy group |
| Geo-blocking (US-only site) | Residential proxies with US exit nodes |
| URL misclassification | Analyze URL structure carefully; don't rely on trailing slash |
| Duplicate products across pages | Track `scraped_urls` set, check before push_data |
| Request timeout too short | Use 60s handler timeout to allow for overlay dismissal + JS loading |

### Step 8: Test and Debug

1. Deploy to Apify, run with default "Silver coin" search
2. Check logs for:
   - "Found X products on search page via JS extraction" — JS snippet works
   - "Sample URL: ... -> is_product=True" — URL classification works
   - "Scraped X/Y products" — data actually getting pushed
   - No 403 errors — proxy working
3. If 0 products scraped, check:
   - Are products found but all skipped? (URL classification issue)
   - Are 0 products found? (JS selectors don't match the DOM)
   - Is the page loading at all? (proxy/blocking issue)
4. Verify exported data has all fields populated

---

## Lessons Learned from JM Bullion

1. **Use `"editor": "proxy"`** in input schema — gives users a dropdown UI, not a blank text field
2. **Always default to RESIDENTIAL proxies** — datacenter IPs get blocked by bullion sites
3. **URL classification must be precise** — trailing slash is NOT a reliable category indicator; use path segment analysis
4. **Dismiss overlays first** — cookie banners can prevent SearchSpring / dynamic content from rendering
5. **Scroll the page** — some content is lazy-loaded and only appears after scrolling
6. **Log sample URLs and their classification** — invaluable for debugging when 0 products are scraped
7. **Deduplicate by URL** — same product can appear on multiple search pages or category pages
8. **Increase handler timeout** — 30s is too tight when you need to dismiss overlays + wait for JS + scroll + retry
