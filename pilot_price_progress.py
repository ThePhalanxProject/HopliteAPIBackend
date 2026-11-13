"""Progress snapshots for one pilot shopping search."""

import logging
from datetime import datetime

from sqlalchemy import update

from consumption import _to_utc
from database import SessionLocal
from models import PilotPriceSearch
from pilot_oracle import PriceSearch, search_price_offers, summarize_price_offers

logger = logging.getLogger(__name__)


def price_search_snapshot(search: PilotPriceSearch, now: datetime) -> dict:
    elapsed = (now - _to_utc(search.created_at)).total_seconds()
    result = search.result or {}
    status = search.status
    if elapsed >= 120 and status in {"searching", "summarizing"}:
        status = "complete" if result.get("offers") else "failed"
        if status == "complete":
            result = {**result, "message_source": "unavailable"}

    if status in {"complete", "failed"}:
        stage = "final"
    elif elapsed >= 15:
        stage = "15_seconds"
    elif elapsed >= 5:
        stage = "5_seconds"
    else:
        stage = "initial"

    return {
        "search_id": search.id,
        "status_url": f"/pilot/prices/{search.id}",
        "stage": stage,
        "status": status,
        "offers": [],
        "message": None,
        "message_source": None,
        **result,
    }


def save_progress(search_id: str, status: str, result: dict | None = None) -> None:
    values = {"status": status}
    if result is not None:
        values["result"] = result
    with SessionLocal.begin() as db:
        db.execute(update(PilotPriceSearch).where(
            PilotPriceSearch.id == search_id
        ).values(**values))


def run_price_search(search_id: str, query: PriceSearch, language: str) -> None:
    try:
        result = search_price_offers(
            query.brand, language, query.city, query.country,
            refresh=query.refresh, postal_code=query.postal_code,
        )
    except Exception as exc:
        logger.warning("Pilot price search %s failed (%s)", search_id, type(exc).__name__)
        save_progress(search_id, "failed")
        return

    if result["offers"]:
        save_progress(search_id, "summarizing", result)
    try:
        result = summarize_price_offers(result, language, query.country)
    except Exception as exc:
        logger.warning("Pilot price summary %s failed (%s)", search_id, type(exc).__name__)
        result = {**result, "message": None, "message_source": "unavailable"}
    save_progress(search_id, "complete", result)
