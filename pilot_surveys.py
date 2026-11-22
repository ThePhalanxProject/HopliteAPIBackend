"""One survey response per account reset, with persistent answer history."""
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from pilot_language import account_language
from models import PilotRefillSurvey, PilotUsageEvent, PilotUsageSurveyTrigger, PilotSurveyCycle, PilotSurveyAdditionalAnswers, PilotSurveyAnswers

SURVEY_VERSION = 3

LEGACY_SURVEY_QUESTIONS = {
    "en": (
        "Did you enjoy the application?",
        "Would you use it if we added other features and connected it to your product?",
        "Did you find it useful?",
        "How would you like us to enhance your experience?",
        "Are you happy to share your usage data anonymously to refine your search?",
        "Are you prepared to pay between 20€ and 30€ for a device that removes your need to press the refill button each time and ensures that the product never runs out?",
    ),
    "fr": (
        "Avez-vous apprécié l’application ?",
        "L’utiliseriez-vous si nous ajoutions d’autres fonctionnalités et la connections à votre produit ?",
        "L’avez-vous trouvée utile ?",
        "Comment aimeriez-vous que nous améliorions votre expérience ?",
        "Acceptez-vous de partager anonymement vos données d’utilisation pour affiner votre recherche ?",
        "Seriez-vous prêt(e) à payer entre 20 € et 30 € pour un appareil qui vous évite d’appuyer sur le bouton de recharge à chaque fois et garantit que le produit ne soit jamais épuisé ?",
    ),
}


SURVEY_QUESTIONS = {
    "en": (
        "Did you enjoy the application?",
        "Did you find the price comparison beneficial?",
        "Would you like to buy a device that reads directly the amount of the product without you pressing the I used it button?",
        "Would you be prepared to pay between 20€ to 30€ for this device?",
        "Do you agree to share your usage and demographic data anonymously?",
        "Any other comments or feedbacks you want to tell us?",
    ),
    "fr": (
        "Avez-vous apprécié l’application ?",
        "Avez-vous trouvé la comparaison des prix utile ?",
        "Souhaiteriez-vous acheter un appareil qui mesure directement la quantité de produit sans que vous ayez à appuyer sur le bouton « Je l’ai utilisé » ?",
        "Seriez-vous prêt(e) à payer entre 20 € et 30 € pour cet appareil ?",
        "Acceptez-vous de partager anonymement vos données d’utilisation et vos données démographiques ?",
        "Avez-vous d’autres commentaires ou suggestions à nous faire ?",
    ),
}


class LegacySurveyAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enjoyed_application: bool = Field(strict=True)
    would_use_connected_product: bool = Field(strict=True)
    found_useful: bool = Field(strict=True)
    experience_feedback: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class VersionTwoSurveyAnswers(LegacySurveyAnswers):
    share_anonymous_usage_data: bool = Field(strict=True)
    would_pay_for_device: bool = Field(strict=True)


class SurveyAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enjoyed_application: bool = Field(strict=True)
    price_comparison_beneficial: bool = Field(strict=True)
    would_buy_automatic_device: bool = Field(strict=True)
    would_pay_for_device: bool = Field(strict=True)
    share_anonymous_usage_and_demographic_data: bool = Field(strict=True)
    comments: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class SurveyView(BaseModel):
    survey_id: UUID
    trigger: Literal["usage", "refill"]
    trigger_request_id: UUID
    triggered_at: datetime
    refill_request_id: UUID | None
    refilled_at: datetime | None
    version: int
    language: Literal["en", "fr"]
    submitted_at: datetime | None
    questions: list[dict]
    answers: SurveyAnswers | VersionTwoSurveyAnswers | LegacySurveyAnswers | None


def survey_questions(language, version=SURVEY_VERSION):
    answer_model = {1: LegacySurveyAnswers, 2: VersionTwoSurveyAnswers, 3: SurveyAnswers}[version]
    texts = (SURVEY_QUESTIONS if version == SURVEY_VERSION else LEGACY_SURVEY_QUESTIONS)[language]
    labels = ("Oui", "Non") if language == "fr" else ("Yes", "No")
    questions = []
    for position, (name, text) in enumerate(zip(answer_model.model_fields, texts), start=1):
        is_text = name in ("experience_feedback", "comments")
        questions.append({
            "id": name, "position": position, "text": text,
            "answer_type": "text" if is_text else "yes_no", "required": True,
            **({"max_length": 2000} if is_text else {"options": [
                {"value": True, "label": labels[0]},
                {"value": False, "label": labels[1]},
            ]}),
        })
    return questions


def saved_survey_answers(survey, db):
    if survey.submitted_at is None:
        return None
    if survey.version == SURVEY_VERSION:
        row = db.get(PilotSurveyAnswers, survey.id)
        return SurveyAnswers(**{name: getattr(row, name) for name in SurveyAnswers.model_fields})
    values = {name: getattr(survey, name) for name in LegacySurveyAnswers.model_fields}
    if survey.version == 1:
        return LegacySurveyAnswers(**values)
    extra = db.get(PilotSurveyAdditionalAnswers, survey.id)
    return VersionTwoSurveyAnswers(**values,
        share_anonymous_usage_data=extra.share_anonymous_usage_data,
        would_pay_for_device=extra.would_pay_for_device,
    )


def save_survey_answers(survey, answers, db):
    db.add(PilotSurveyAnswers(survey_id=survey.id, **answers.model_dump()))
    survey.version = SURVEY_VERSION
    db.flush()


def survey_view(survey, db):
    language = account_language(db, survey.user_id)
    usage_trigger = db.get(PilotUsageSurveyTrigger, survey.id)
    version = survey.version if survey.submitted_at is not None else SURVEY_VERSION
    return SurveyView(
        survey_id=survey.id,
        trigger="usage" if usage_trigger else "refill",
        trigger_request_id=usage_trigger.request_id if usage_trigger else survey.refill_request_id,
        triggered_at=usage_trigger.occurred_at if usage_trigger else survey.refilled_at,
        refill_request_id=None if usage_trigger else survey.refill_request_id,
        refilled_at=None if usage_trigger else survey.refilled_at,
        version=version, language=language,
        submitted_at=survey.submitted_at, questions=survey_questions(language, version),
        answers=saved_survey_answers(survey, db),
    )


def current_survey(db, user_id):
    cycle = db.get(PilotSurveyCycle, user_id)
    query = select(PilotRefillSurvey).where(PilotRefillSurvey.user_id == user_id)
    if cycle is not None:
        query = query.where(PilotRefillSurvey.refilled_at >= cycle.started_at)
    # Earlier releases may have created several invitations. A submitted response
    # completes the cycle; otherwise use its oldest pending invitation.
    return db.scalar(query.order_by(
        PilotRefillSurvey.submitted_at.is_(None),
        PilotRefillSurvey.refilled_at, PilotRefillSurvey.id,
    ).limit(1))


def pending_survey(db, user_id):
    survey = current_survey(db, user_id)
    return survey if survey is not None and survey.submitted_at is None else None


def activate_usage_survey(db, pack, language):
    """Activate once after any recorded use; callers hold the pack lock."""
    existing = current_survey(db, pack.user_id)
    if existing is not None:
        return
    cycle = db.get(PilotSurveyCycle, pack.user_id)
    query = select(PilotUsageEvent).where(PilotUsageEvent.pack_id == pack.id)
    if cycle is not None:
        query = query.where(PilotUsageEvent.occurred_at >= cycle.started_at)
    first_use = db.scalar(query.order_by(
        PilotUsageEvent.occurred_at, PilotUsageEvent.id,
    ).limit(1))
    if first_use is None:
        return
    # Existing storage column names are retained for database compatibility.
    # Public trigger fields describe the actual event; refill aliases are null.
    survey = PilotRefillSurvey(
        id=str(uuid4()), user_id=pack.user_id, refill_request_id=str(uuid4()),
        refilled_at=first_use.occurred_at, version=SURVEY_VERSION, language=language,
    )
    db.add(survey)
    db.flush()
    db.add(PilotUsageSurveyTrigger(
        survey_id=survey.id, request_id=first_use.request_id, occurred_at=first_use.occurred_at,
    ))
    db.flush()
