"""One survey response per account reset, with persistent answer history."""
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from pilot_language import account_language
from models import PilotRefillSurvey, PilotUsageEvent, PilotUsageSurveyTrigger, PilotSurveyCycle, PilotSurveyAdditionalAnswers

SURVEY_VERSION = 2

SURVEY_QUESTIONS = {
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


class LegacySurveyAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enjoyed_application: bool = Field(strict=True)
    would_use_connected_product: bool = Field(strict=True)
    found_useful: bool = Field(strict=True)
    experience_feedback: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class SurveyAnswers(LegacySurveyAnswers):
    share_anonymous_usage_data: bool = Field(strict=True)
    would_pay_for_device: bool = Field(strict=True)


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
    answers: SurveyAnswers | LegacySurveyAnswers | None


def survey_questions(language, version=SURVEY_VERSION):
    answer_names = tuple((LegacySurveyAnswers if version == 1 else SurveyAnswers).model_fields)
    return [
        {"id": name, "text": text, "answer_type": "text" if name == "experience_feedback" else "yes_no",
         **({"max_length": 2000} if name == "experience_feedback" else {})}
        for index, (name, text) in enumerate(zip(answer_names, SURVEY_QUESTIONS[language]))
    ]


def saved_survey_answers(survey, db):
    if survey.submitted_at is None:
        return None
    values = {name: getattr(survey, name) for name in LegacySurveyAnswers.model_fields}
    if survey.version == 1:
        return LegacySurveyAnswers(**values)
    extra = db.get(PilotSurveyAdditionalAnswers, survey.id)
    return SurveyAnswers(**values, **{
        name: getattr(extra, name) for name in SurveyAnswers.model_fields
        if name not in LegacySurveyAnswers.model_fields
    })


def save_survey_answers(survey, answers, db):
    for name in LegacySurveyAnswers.model_fields:
        setattr(survey, name, getattr(answers, name))
    db.add(PilotSurveyAdditionalAnswers(
        survey_id=survey.id,
        share_anonymous_usage_data=answers.share_anonymous_usage_data,
        would_pay_for_device=answers.would_pay_for_device,
    ))
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
