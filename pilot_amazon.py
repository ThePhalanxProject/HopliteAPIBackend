"""Country-specific Amazon lookup using the existing SerpApi credential."""
import re
from urllib.parse import urlsplit

import requests
from fastapi import HTTPException

AMAZON_MARKETPLACES = {
    "ae": "amazon.ae", "au": "amazon.com.au", "be": "amazon.com.be",
    "br": "amazon.com.br", "ca": "amazon.ca", "cn": "amazon.cn",
    "de": "amazon.de", "eg": "amazon.eg", "es": "amazon.es",
    "fr": "amazon.fr", "gb": "amazon.co.uk", "in": "amazon.in",
    "it": "amazon.it", "jp": "amazon.co.jp", "mx": "amazon.com.mx",
    "nl": "amazon.nl", "pl": "amazon.pl", "sa": "amazon.sa",
    "se": "amazon.se", "sg": "amazon.sg", "tr": "amazon.com.tr",
    "us": "amazon.com", "za": "amazon.co.za",
}
# SerpApi's direct Amazon engine supports these markets. South Africa uses the
# localized Shopping engine until its Amazon domain is supported directly.
DIRECT_AMAZON_DOMAINS = set(AMAZON_MARKETPLACES.values()) - {"amazon.co.za"}


def amazon_domain(link):
    if not isinstance(link, str):
        return None
    try:
        parsed = urlsplit(link)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)):
            return None
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    return next((domain for domain in AMAZON_MARKETPLACES.values()
                 if host == domain or host.endswith("." + domain)), None)


def amazon_retailer(retailer, country):
    name = retailer.strip().casefold() if isinstance(retailer, str) else ""
    domain = AMAZON_MARKETPLACES.get(country)
    names = {"amazon", "amazon marketplace"}
    if domain:
        names.update((domain, "www." + domain))
    return name in names or any(name.startswith(alias + " - ") for alias in names)


def amazon_country_conflict(row, country):
    local = AMAZON_MARKETPLACES.get(country)
    if not local:
        return False
    for link in (row.get("link"), row.get("product_link")):
        domain = amazon_domain(link)
        if domain and domain != local:
            return True
    raw_source = row.get("source")
    source = raw_source.strip().casefold() if isinstance(raw_source, str) else ""
    return any(domain != local and (
        source == domain or source == "www." + domain or source.startswith(domain + " - ")
    ) for domain in AMAZON_MARKETPLACES.values())


def offer_identity(link):
    domain = amazon_domain(link)
    if domain:
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", urlsplit(link).path, re.I)
        if match:
            return domain + ":" + match[1].upper()
    return link


def amazon_results(brand, language, location, country, key, *, refresh=False, postal_code=None):
    domain = AMAZON_MARKETPLACES.get(country)
    category = "lessive liquide lavages" if domain in {"amazon.fr", "amazon.com.be"} else "liquid laundry detergent washes"
    direct = domain in DIRECT_AMAZON_DOMAINS
    if direct:
        params = {"engine": "amazon", "amazon_domain": domain,
                  "k": f"{brand} {category}", "shipping_location": country.upper(), "api_key": key}
        if domain in {"amazon.ae", "amazon.sa", "amazon.eg"}:
            params["delivery_city"] = location.split(",")[0].strip()
        elif postal_code:
            params["delivery_zip"] = postal_code
    else:
        target = f"site:{domain}" if domain else "Amazon"
        params = {"engine": "google_shopping", "q": f"{brand} {category} {target}",
                  "location": location, "gl": country, "hl": language, "api_key": key}
    if refresh:
        params["no_cache"] = "true"
    try:
        response = requests.get("https://serpapi.com/search.json", params=params, timeout=8)
        response.raise_for_status()
        data = response.json()
        rows = data.get("organic_results" if direct else "shopping_results", [])
        if data.get("error") or not isinstance(rows, list):
            raise ValueError("Invalid Amazon search response")
    except (requests.RequestException, ValueError, AttributeError):
        raise HTTPException(502, "Amazon search is temporarily unavailable") from None

    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not direct:
            # A targeted Shopping query can still return other sellers.
            linked_domain = amazon_domain(row.get("link")) or amazon_domain(row.get("product_link"))
            if not (amazon_retailer(row.get("source"), country)
                    or (linked_domain is not None and (domain is None or linked_domain == domain))):
                continue
            if amazon_country_conflict(row, country):
                continue
            results.append(row)
            continue
        link = row.get("link_clean") or row.get("link")
        if amazon_domain(link) != domain:
            continue
        condition = str(row.get("condition") or "").casefold()
        unavailable = str(row.get("stock") or "").casefold()
        if condition in {"used", "renewed", "refurbished"} or unavailable in {
            "currently unavailable", "out of stock", "indisponible",
        }:
            continue
        # Keep the actual listed pack price and fulfillment evidence. The caller
        # applies the shared brand, wash-count, B2B, tax, currency and location rules.
        results.append({
            **row, "source": "Amazon" + domain[len("amazon"):],
            "link": link, "product_link": link,
            "second_hand_condition": row.get("second_hand_condition"),
        })
    return results
