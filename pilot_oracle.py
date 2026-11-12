import logging
import re
from decimal import Decimal
from math import isfinite
from os import getenv
from unicodedata import combining, normalize
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from agent import AgentError, call_hoplite_agent

logger = logging.getLogger(__name__)


class OracleMessage(BaseModel):
    stock_status: str
    message: str | None
    source: str


class PriceSearch(BaseModel):
    brand: str = Field(min_length=2, max_length=100, pattern=r"\S")
    city: str = Field(min_length=2, max_length=100)
    country: str = Field(pattern=r"^[a-z]{2}$")
    language: str | None = Field(default=None, pattern=r"^(en|fr)$")

    @field_validator("brand", "city", "country", mode="before")
    @classmethod
    def clean_location_input(cls, value):
        if not isinstance(value, str):
            return value
        return value.strip()

    @field_validator("country", mode="before")
    @classmethod
    def lowercase_country(cls, value):
        return value.lower() if isinstance(value, str) else value


STOCK_STYLES = (
    "Lead with the stock outlook.",
    "Lead with the reward if earned, then give the stock outlook.",
    "Use a warm, conversational update.",
    "Use a concise Oracle-style observation with a practical next step if stock is low.",
)


def oracle_copy(payload, fallback, *, generate=True):
    if generate:
        try:
            response = call_hoplite_agent(payload, pilot_mode=True)
        except AgentError as exc:
            logger.warning("Pilot Oracle generation failed: %s", exc)
            return fallback, "fallback"
        if response is None:
            logger.warning("Pilot Oracle credentials are not configured")
            return fallback, "fallback"
        if response:
            message = response.get("message")
            if isinstance(message, str) and 0 < len(message.strip()) <= 300:
                return message.strip(), "kimi"
        logger.warning("Pilot Oracle response had no usable message")
    return fallback, "fallback"


def stock_message(
    pack, language, *, points_earned=0, cashback_unlocked_cents=0, event=None, generate=True
):
    days = pack.days_remaining
    if pack.started_at is None:
        state = "not_started"
    elif pack.current_volume_l == 0:
        state = "empty"
    elif pack.full_doses_remaining == 0 or (days is not None and days <= 3):
        state = "urgent"
    elif days is not None and days <= 7:
        state = "low"
    else:
        state = "ok"

    message, _ = oracle_copy(
        {
            "task": "stock_update",
            "stock_status": state,
            "days_remaining": days,
            "current_volume_l": pack.current_volume_l,
            "capacity_l": pack.capacity_l,
            "dose_ml": pack.dose_ml,
            "event": event,
            "points_earned": points_earned,
            "total_points": pack.user_points,
            "cashback_earned_cents": pack.cashback_earned_cents,
            "cashback_unlocked_cents": cashback_unlocked_cents,
            "points_to_next_cashback": pack.points_to_next_cashback,
            "language": language,
            "style": STOCK_STYLES[pack.sample_count % len(STOCK_STYLES)],
            "variation_id": str(uuid4()),
        },
        None,
        generate=generate,
    )
    source = "kimi" if message else "unavailable" if generate else "skipped"
    return OracleMessage(stock_status=state, message=message, source=source)


WASH_COUNT = re.compile(
    r"\b([1-9][0-9]{0,3})\s*(?:lavages?|lessives?|doses?|wash(?:es)?|loads?)\b",
    re.IGNORECASE,
)
MULTIPACK = re.compile(r"\b(?:lot de|pack of|multipack|[2-9]\s*[x×])\b", re.IGNORECASE)


def shopping_results(query, location, country, language, key):
    try:
        response = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_shopping", "q": query, "location": location,
                "gl": country, "hl": language, "api_key": key,
            },
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("shopping_results", [])
        if data.get("error") or not isinstance(results, list):
            raise ValueError("Invalid shopping results")
        return results
    except (requests.RequestException, ValueError, AttributeError):
        raise HTTPException(502, "Shopping search is temporarily unavailable") from None


def brand_words(value: str) -> list[str]:
    folded = normalize("NFKD", value.casefold())
    return re.findall(r"[^\W_]+", "".join(char for char in folded if not combining(char)))


def matches_brand(row: dict, title: str, requested: list[str]) -> bool:
    if not requested:
        return False
    listed_brand = row.get("brand")
    if isinstance(listed_brand, str) and listed_brand.strip():
        return brand_words(listed_brand) == requested

    words = brand_words(title)
    if set(words) & {"alternative", "compatible", "equivalent", "replacement", "substitute"}:
        return False
    # Listings often begin with a product category before naming the brand.
    category_prefix = {"lessive", "liquide", "liquid", "laundry", "detergent", "de", "marque"}
    for index in range(len(words)):
        if words[index:index + len(requested)] == requested:
            return True
        if words[index] not in category_prefix:
            return False
    return False


BUSINESS_ONLY = re.compile(
    r"\b(?:b2b|wholesal(?:e|er|ers)|grossist(?:e|es)|"
    r"réservé aux professionnels|professionals only|trade only|"
    r"hors taxes|(?:prix )?h\.?\s?t\.?|"
    r"(?:ex(?:cl(?:uding)?)?\.?[-\s]+vat|without vat|before tax|"
    r"ex\.?[-\s]+tax|vat not included|iva esclusa|htva))\b",
    re.IGNORECASE,
)
BUSINESS_SELLERS = {"kechiq", "kechiq srl", "damiano profumi"}
BUSINESS_DOMAINS = ("damianoprofumi.it",)


def consumer_offer(row):
    source = row.get("source")
    if isinstance(source, str):
        seller = " ".join(brand_words(source))
        if seller in BUSINESS_SELLERS:
            return None
    else:
        source = None

    metadata = " ".join(
        str(row.get(field) or "")
        for field in ("source", "title", "price", "snippet", "extensions", "tag", "badge", "details_and_offers")
    )
    if BUSINESS_ONLY.search(metadata):
        return None

    for link in (row.get("link"), row.get("product_link")):
        if not isinstance(link, str):
            continue
        try:
            url = urlsplit(link)
        except ValueError:
            continue
        host = url.hostname or ""
        if url.scheme != "https" or not host or url.username or url.password:
            continue
        if any(host == domain or host.endswith("." + domain) for domain in BUSINESS_DOMAINS):
            return None
        return source, link
    return None


def pilot_prices(brand, language, location, country):
    key = getenv("SERPAPI_API_KEY")
    if not key:
        raise HTTPException(503, "Shopping search is not configured")
    category = "lessive liquide machine lavages" if language == "fr" else "liquid laundry detergent washes"
    reference = "1,5 L" if language == "fr" else "1.5 L"
    rows = shopping_results(f"{brand} {category} {reference}", location, country, language, key)
    alternatives = shopping_results(f"{brand} {category}", location, country, language, key)
    requested_brand = brand_words(brand)

    offers = []
    seen_links = set()
    for row in rows + alternatives:
        if not isinstance(row, dict) or row.get("second_hand_condition"):
            continue
        amount = row.get("extracted_price")
        price = row.get("price")
        title = row.get("title")
        consumer = consumer_offer(row)
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or not isfinite(amount)
            or amount <= 0
            or consumer is None
            or not isinstance(price, str)
            or not isinstance(title, str)
            or MULTIPACK.search(title)
            or not matches_brand(row, title, requested_brand)
        ):
            continue
        retailer, link = consumer
        if link in seen_links:
            continue
        counts = set(WASH_COUNT.findall(title))
        currency = re.sub(r"[\d.,\s]", "", price)
        currency = {"EUR": "€", "GBP": "£", "USD": "US$"}.get(currency, currency)
        if len(counts) != 1 or not currency:
            continue
        washes = int(counts.pop())
        seen_links.add(link)
        offers.append({
            "title": title, "retailer": retailer, "listed_price": price,
            "wash_count": washes, "currency": currency, "url": link,
            "price_per_wash": Decimal(str(amount)) / washes,
        })

    # Price ranking is meaningful only within a common currency.
    currency = max((o["currency"] for o in offers), key=lambda c: sum(o["currency"] == c for o in offers), default=None)
    offers = sorted(
        (o for o in offers if o["currency"] == currency),
        key=lambda offer: offer["price_per_wash"],
    )[:5]
    for offer in offers:
        offer["price_per_wash"] = float(round(offer["price_per_wash"], 4))
    result = {"location": location, "reference_brand": brand, "reference_volume_l": 1.5, "offers": offers}
    if not offers:
        return {**result, "message": None, "message_source": None}

    best = offers[0]
    message, source = oracle_copy(
        {
            "task": "shopping_update",
            "offer": best,
            "ranking_basis": "listed item price per wash; exclude delivery",
            "reference_brand": brand,
            "language": language,
        },
        None,
    )
    return {**result, "message": message, "message_source": source if message else "unavailable"}
