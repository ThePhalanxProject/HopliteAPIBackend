import logging
from math import isfinite
from os import getenv
from uuid import uuid4

import requests
from fastapi import HTTPException
from pydantic import BaseModel, Field

from agent import AgentError, call_hoplite_agent

logger = logging.getLogger(__name__)


class OracleMessage(BaseModel):
    stock_status: str
    message: str | None
    source: str


class PriceSearch(BaseModel):
    product: str = Field(min_length=3, max_length=160)
    city: str = Field(min_length=2, max_length=100)
    country: str = Field(pattern=r"^[a-z]{2}$")
    language: str = Field(default="en", pattern=r"^(en|fr)$")


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


def stock_message(pack, language, *, points_earned=0, generate=True):
    days = pack.days_remaining
    if pack.started_at is None:
        state = "not_started"
    elif pack.current_weight_g == 0:
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
            "points_earned": points_earned,
            "language": language,
            "style": STOCK_STYLES[pack.sample_count % len(STOCK_STYLES)],
            "variation_id": str(uuid4()),
        },
        None,
        generate=generate,
    )
    source = "kimi" if message else "unavailable" if generate else "skipped"
    return OracleMessage(stock_status=state, message=message, source=source)


def pilot_prices(query):
    key = getenv("SERPAPI_API_KEY")
    if not key:
        raise HTTPException(503, "Shopping search is not configured")

    try:
        response = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_shopping",
                "q": query.product,
                "location": query.city,
                "gl": query.country,
                "hl": query.language,
                "api_key": key,
            },
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError("Shopping search returned an error")
        results = data.get("shopping_results", [])
        if not isinstance(results, list):
            raise ValueError("Invalid shopping results")
    except (requests.RequestException, ValueError):
        raise HTTPException(502, "Shopping search is temporarily unavailable") from None

    offers = []
    seen_links = set()
    for row in results:
        if not isinstance(row, dict) or row.get("second_hand_condition"):
            continue
        amount = row.get("extracted_price")
        link = row.get("link") or row.get("product_link")
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or not isfinite(amount)
            or amount <= 0
            or not isinstance(link, str)
            or not link.startswith("https://")
            or link in seen_links
            or not row.get("price")
            or not row.get("title")
        ):
            continue
        seen_links.add(link)
        offers.append({
            "title": row["title"],
            "retailer": row.get("source"),
            "price": row["price"],
            "amount": amount,
            "url": link,
            "delivery": row.get("delivery"),
        })
    offers = sorted(offers, key=lambda offer: offer["amount"])[:5]
    if not offers:
        return {"offers": [], "message": None, "message_source": None}

    best = offers[0]
    fallback = (
        f"Prix le plus bas trouvé dans ces résultats : {best['price']}."
        if query.language == "fr"
        else f"The lowest listed price in these results is {best['price']}."
    )
    message, source = oracle_copy(
        {"task": "shopping_update", "offer": best["price"], "language": query.language},
        fallback,
    )
    return {"offers": offers, "message": message, "message_source": source}
