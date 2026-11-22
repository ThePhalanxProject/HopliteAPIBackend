"""One survey response per account reset, with persistent answer history."""
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select

from models import PilotRefillSurvey, PilotUsageEvent, PilotUsageSurveyTrigger, PilotSurveyCycle

SURVEY_QUESTIONS = {
    "en": (
        "Did you enjoy the application?",
        "Would you use it if we added other features and connected it to your product?",
        "Did you find it useful?",
        "How would you like us to enhance your experience?",
    ),
    "fr": (
        "Avez-vous apprécié l’application ?",
        "L’utiliseriez-vous si nous ajoutions d’autres fonctionnalités et la connections à votre produit ?",
        "L’avez-vous trouvée utile ?",
        "Comment aimeriez-vous que nous améliorions votre expérience ?",
    ),
}


class SurveyAnswers(BaseModel):
    enjoyed_application: bool = Field(strict=True)
    would_use_connected_product: bool = Field(strict=True)
    found_useful: bool = Field(strict=True)
    experience_feedback: str = Field(min_length=1, max_length=2000, pattern=r"\S")


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
    answers: SurveyAnswers | None


def survey_questions(language):
    answer_names = tuple(SurveyAnswers.model_fields)
    return [
        {"id": name, "text": text, "answer_type": "yes_no" if index < 3 else "text",
         **({"max_length": 2000} if index == 3 else {})}
        for index, (name, text) in enumerate(zip(answer_names, SURVEY_QUESTIONS[language]))
    ]


def survey_view(survey, db):
    usage_trigger = db.get(PilotUsageSurveyTrigger, survey.id)
    answer_names = tuple(SurveyAnswers.model_fields)
    return SurveyView(
        survey_id=survey.id,
        trigger="usage" if usage_trigger else "refill",
        trigger_request_id=usage_trigger.request_id if usage_trigger else survey.refill_request_id,
        triggered_at=usage_trigger.occurred_at if usage_trigger else survey.refilled_at,
        refill_request_id=None if usage_trigger else survey.refill_request_id,
        refilled_at=None if usage_trigger else survey.refilled_at,
        version=survey.version, language=survey.language,
        submitted_at=survey.submitted_at, questions=survey_questions(survey.language),
        answers=SurveyAnswers(**{name: getattr(survey, name) for name in answer_names})
        if survey.submitted_at is not None else None,
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
        refilled_at=first_use.occurred_at, version=1, language=language,
    )
    db.add(survey)
    db.flush()
    db.add(PilotUsageSurveyTrigger(
        survey_id=survey.id, request_id=first_use.request_id, occurred_at=first_use.occurred_at,
    ))
    db.flush()
