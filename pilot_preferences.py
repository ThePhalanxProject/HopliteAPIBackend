"""Retailer preferences learned from verified offer interactions."""
from sqlalchemy import select
from models import PilotOfferPreference
from pilot_oracle import brand_words


def preference_scores(db, user_id, brand, country):
    rows = db.scalars(select(PilotOfferPreference).where(
        PilotOfferPreference.user_id == user_id,
        PilotOfferPreference.brand == " ".join(brand_words(brand)),
        PilotOfferPreference.country == country,
    ))
    signals = {}
    for row in rows:
        key = (row.search_id, row.url, row.retailer)
        signals[key] = max(signals.get(key, 0), 2 if row.action == "select" else 1)
    scores = {}
    for (_, _, retailer), weight in signals.items():
        scores[retailer] = scores.get(retailer, 0) + weight
    return scores
