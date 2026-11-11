from math import isfinite
from os import getenv

import requests
from fastapi import HTTPException
from pydantic import BaseModel, Field

from agent import AgentError, call_hoplite_agent


class OracleMessage(BaseModel):
    stock_status: str
    message: str
    source: str


class PriceSearch(BaseModel):
    product: str = Field(min_length=3, max_length=160)
    city: str = Field(min_length=2, max_length=100)
    country: str = Field(pattern=r"^[a-z]{2}$")
    language: str = Field(default="en", pattern=r"^(en|fr)$")


STOCK_COPY = {
    "en": {
        "not_started": "Start your pack when you’re ready.",
        "ok": "Your stock looks good for now.",
        "low": "Your product is running low. Plan to replenish it soon.",
        "urgent": "Your product is nearly finished. It’s time to replenish.",
        "empty": "Your pack is empty. It’s time to replenish.",
    },
    "fr": {
        "not_started": "Activez votre pack quand vous êtes prêt.",
        "ok": "Il vous reste suffisamment de produit pour le moment.",
        "low": "Votre produit commence à manquer. Pensez à le renouveler bientôt.",
        "urgent": "Votre produit est presque épuisé. Il est temps de le renouveler.",
        "empty": "Votre pack est vide. Il est temps de le renouveler.",
    },
}


def oracle_copy(payload, fallback, *, generate=True):
    if generate:
        try:
            response = call_hoplite_agent(payload, pilot_mode=True)
        except AgentError:
            response = None
        if response:
            message = response.get("message")
            if isinstance(message, str) and 0 < len(message.strip()) <= 300:
                return message.strip(), "kimi"
    return fallback, "fallback"


def stock_message(pack, language, *, generate=True):
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

    fallback = STOCK_COPY[language][state]
    if state in {"low", "urgent"} and days is not None:
        if language == "fr":
            duration = "moins d’un jour" if days < 1 else f"environ {round(days)} j"
            fallback += f" Stock estimé pour {duration}."
        else:
            duration = "less than a day" if days < 1 else f"about {round(days)} day(s)"
            fallback += f" Estimated supply remaining is {duration}."

    message, source = oracle_copy(
        {
            "task": "stock_update",
            "stock_status": state,
            "days_remaining": days,
            "language": language,
        },
        fallback,
        generate=generate,
    )
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
