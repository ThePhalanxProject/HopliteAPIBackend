"""English/French account preferences and localized pilot API errors."""
from datetime import datetime, timezone
from hashlib import sha256

from fastapi import Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from database import SessionLocal
from models import PilotMessagePreference, PilotSession, PilotUser

FRENCH_ERRORS = {
    "Invalid pilot credential": "Session invalide ou expirée. Veuillez vous reconnecter.",
    "No pilot pack assigned": "Aucun produit pilote n’est associé à votre compte.",
    "Start the pilot pack before logging events": "Activez votre produit avant d’enregistrer une utilisation.",
    "The pilot pack is already full": "Votre produit est déjà plein.",
    "The pilot pack is empty": "Votre produit est vide. Veuillez le recharger.",
    "Refill message not found": "Message de recharge introuvable.",
    "Usage message not found": "Message d’utilisation introuvable.",
    "Price search not found": "Recherche de prix introuvable.",
    "Request ID already used for another interaction": "Cet identifiant a déjà été utilisé pour une autre action.",
    "Offer not found in this search": "Offre introuvable dans cette recherche.",
    "This offer has no identified retailer": "Le commerçant de cette offre n’a pas été identifié.",
    "Refresh prices before recording this preference": "Actualisez les prix avant d’enregistrer cette préférence.",
    "Survey not found": "Questionnaire introuvable.",
    "This survey is no longer active. Refresh the survey tab.": "Ce questionnaire n’est plus actif. Actualisez l’onglet Questionnaires.",
    "This survey has already been submitted": "Ce questionnaire a déjà été envoyé.",
    "Shopping search is temporarily unavailable": "La recherche de prix est temporairement indisponible.",
    "Shopping search is not configured": "La recherche de prix n’est pas configurée.",
    "Invalid or already used pilot invitation": "Le code d’invitation est invalide ou a déjà été utilisé.",
    "This email is already registered": "Cette adresse e-mail est déjà enregistrée.",
    "Too many attempts. Try again later.": "Trop de tentatives. Veuillez réessayer plus tard.",
    "Invalid email or password": "Adresse e-mail ou mot de passe incorrect.",
    "Incorrect password": "Mot de passe incorrect.",
    "Enroll your pilot login before resetting activity": "Créez votre compte pilote avant de réinitialiser votre activité.",
    "Not Found": "Ressource introuvable.",
    "Method Not Allowed": "Méthode non autorisée.",
}


def account_language(db, user_id):
    preference = db.get(PilotMessagePreference, user_id)
    return preference.language if preference else "en"


def header_language(request):
    for item in request.headers.get("Accept-Language", "en").split(","):
        code = item.split(";")[0].strip().lower().split("-")[0]
        if code in ("en", "fr"):
            return code
    return "en"


def request_language(request):
    explicit = getattr(request.state, "language", None)
    if explicit in ("en", "fr"):
        return explicit
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token:
        digest = sha256(token.encode()).hexdigest()
        try:
            with SessionLocal() as db:
                user_id = db.scalar(select(PilotUser.id).where(PilotUser.token_hash == digest))
                if user_id is None:
                    user_id = db.scalar(select(PilotSession.user_id).where(
                        PilotSession.token_hash == digest,
                        PilotSession.expires_at > datetime.now(timezone.utc),
                    ))
                if user_id is not None:
                    return account_language(db, user_id)
        except SQLAlchemyError:
            pass
    return header_language(request)


async def localized_http_error(request: Request, exc):
    if not request.url.path.startswith("/pilot/"):
        return await http_exception_handler(request, exc)
    language = await run_in_threadpool(request_language, request)
    detail = exc.detail
    if language == "fr" and isinstance(detail, str):
        detail = FRENCH_ERRORS.get(detail, "La demande n’a pas pu être traitée.")
    return JSONResponse(
        status_code=exc.status_code, content={"detail": detail},
        headers={**(exc.headers or {}), "Content-Language": language},
    )


async def localized_validation_error(request: Request, exc):
    if not request.url.path.startswith("/pilot/"):
        return await request_validation_exception_handler(request, exc)
    language = await run_in_threadpool(request_language, request)
    errors = []
    translations = {
        "missing": "Ce champ est obligatoire.",
        "bool_type": "Choisissez Oui ou Non.",
        "string_pattern_mismatch": "Le format de ce champ est invalide.",
        "literal_error": "Valeur non prise en charge.",
        "uuid_parsing": "Identifiant invalide.",
        "json_invalid": "Le corps de la demande doit être un JSON valide.",
    }
    for error in exc.errors():
        entry = {"loc": error["loc"], "type": error["type"], "msg": error["msg"]}
        if language == "fr":
            context = error.get("ctx", {})
            if error["type"] == "string_too_long":
                entry["msg"] = f'Ce champ doit contenir au maximum {context["max_length"]} caractères.'
            elif error["type"] == "string_too_short":
                entry["msg"] = f'Ce champ doit contenir au moins {context["min_length"]} caractères.'
            else:
                entry["msg"] = translations.get(error["type"], "Valeur invalide.")
        errors.append(entry)
    return JSONResponse(status_code=422, content={"detail": errors},
                        headers={"Content-Language": language})
