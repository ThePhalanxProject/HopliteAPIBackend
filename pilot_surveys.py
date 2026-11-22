"""Persistent survey invitations and answers for each pilot refill."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select

from models import PilotRefillSurvey

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
    refill_request_id: UUID
    refilled_at: datetime
    version: int
    language: Literal["en", "fr"]
    submitted_at: datetime | None
    questions: list[dict]
    answers: SurveyAnswers | None


def survey_view(survey):
    answer_names = tuple(SurveyAnswers.model_fields)
    questions = [
        {"id": name, "text": text, "answer_type": "yes_no" if index < 3 else "text",
         **({"max_length": 2000} if index == 3 else {})}
        for index, (name, text) in enumerate(zip(answer_names, SURVEY_QUESTIONS[survey.language]))
    ]
    return SurveyView(
        survey_id=survey.id, refill_request_id=survey.refill_request_id,
        refilled_at=survey.refilled_at, version=survey.version, language=survey.language,
        submitted_at=survey.submitted_at, questions=questions,
        answers=SurveyAnswers(**{name: getattr(survey, name) for name in answer_names})
        if survey.submitted_at is not None else None,
    )


def pending_survey(db, user_id):
    return db.scalar(select(PilotRefillSurvey).where(
        PilotRefillSurvey.user_id == user_id,
        PilotRefillSurvey.submitted_at.is_(None),
    ).order_by(PilotRefillSurvey.refilled_at, PilotRefillSurvey.id).limit(1))
