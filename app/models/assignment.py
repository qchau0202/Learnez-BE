"""Pydantic models for public.assignments, questions, and submissions."""

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


class QuestionCreate(BaseModel):
    type: str = Field(default="", max_length=100)
    content: Optional[str] = None
    order_index: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None


class QuestionUpdate(BaseModel):
    type: Optional[str] = Field(None, max_length=100)
    content: Optional[str] = None
    order_index: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None


class QuestionOut(BaseModel):
    id: int
    assignment_id: Optional[int] = None
    created_at: datetime
    type: Optional[str] = ""
    content: Optional[str] = None
    order_index: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None

    class Config:
        from_attributes = True


class AssignmentCreate(BaseModel):
    module_id: int
    title: str = Field(min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=5000)
    due_date: Optional[datetime] = Field(
        None,
        description="Soft deadline (overdue reminders). Late submission allowed after this unless hard_due_date applies.",
    )
    hard_due_date: Optional[datetime] = Field(
        None,
        description="If set, final student submission cutoff. Must be on or after due_date when both are set.",
    )
    total_score: Optional[float] = None
    is_graded: bool = False
    questions: Optional[List[QuestionCreate]] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "module_id": 101,
                "title": "Quiz 1 - Basics",
                "description": "MCQ + essay introduction quiz.",
                "due_date": "2026-06-30T23:59:00+00:00",
                "hard_due_date": "2026-07-02T23:59:00+00:00",
                "total_score": 10.0,
                "is_graded": True,
                "questions": [
                    {
                        "type": "mcq",
                        "content": "Choose the correct option.",
                        "order_index": 0,
                        "metadata": {
                            "options": [{"id": "A", "text": "Wrong"}, {"id": "B", "text": "Right"}],
                            "correct_option_ids": ["B"],
                            "allow_multiple": False,
                        },
                    }
                ],
            }
        }
    }


class AssignmentUpdate(BaseModel):
    module_id: Optional[int] = None
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=5000)
    due_date: Optional[datetime] = None
    hard_due_date: Optional[datetime] = None
    total_score: Optional[float] = None
    is_graded: Optional[bool] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Quiz 1 - Basics (updated)",
                "due_date": "2026-07-01T23:59:00+00:00",
                "hard_due_date": "2026-07-03T23:59:00+00:00",
            }
        }
    }


class AssignmentOut(BaseModel):
    id: int
    module_id: Optional[int] = None
    created_at: datetime
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    hard_due_date: Optional[datetime] = None
    total_score: Optional[float] = None
    title: Optional[str] = None
    is_graded: Optional[bool] = None
    uploaded_by: Optional[str] = None

    class Config:
        from_attributes = True


class AssignmentDetailOut(AssignmentOut):
    questions: List[QuestionOut] = Field(default_factory=list)


class AnswerIn(BaseModel):
    question_id: int
    answer_content: Optional[str] = None


class SubmissionCreate(BaseModel):
    """Admin may set student_id to submit on behalf of a learner."""

    student_id: Optional[str] = None
    answers: List[AnswerIn] = Field(default_factory=list)
    status: Literal["draft", "submitted"] = "submitted"

    model_config = {
        "json_schema_extra": {
            "example": {
                "answers": [
                    {"question_id": 7001, "answer_content": "{\"selected\": [\"B\"]}"},
                    {"question_id": 7002, "answer_content": "Essay response content."},
                ],
                "status": "submitted",
            }
        }
    }


class SubmissionUpdate(BaseModel):
    answers: Optional[List[AnswerIn]] = None
    status: Optional[Literal["draft", "submitted"]] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "answers": [{"question_id": 7002, "answer_content": "Updated essay answer."}],
                "status": "submitted",
            }
        }
    }


class GradeAnswerIn(BaseModel):
    question_id: int
    earned_score: float
    is_correct: Optional[bool] = None
    ai_feedback: Optional[str] = None


class SubmissionGradeIn(BaseModel):
    answer_grades: List[GradeAnswerIn] = Field(default_factory=list)
    feedback: Optional[str] = None
    finalize: bool = True

    model_config = {
        "json_schema_extra": {
            "example": {
                "answer_grades": [
                    {
                        "question_id": 7002,
                        "earned_score": 4.5,
                        "is_correct": True,
                        "ai_feedback": "Good structure and clear explanation.",
                    }
                ],
                "feedback": "Overall good performance.",
                "finalize": True,
            }
        }
    }


class SubmissionFeedbackIn(BaseModel):
    feedback: str = Field(min_length=1, max_length=5000)


class SubmissionAnswerOut(BaseModel):
    id: int
    created_at: datetime
    submission_id: Optional[int] = None
    question_id: Optional[int] = None
    answer_content: Optional[str] = None
    is_correct: Optional[bool] = None
    earned_score: Optional[float] = None
    ai_feedback: Optional[str] = None

    class Config:
        from_attributes = True


class SubmissionOut(BaseModel):
    id: int
    created_at: datetime
    student_id: Optional[str] = None
    assignment_id: Optional[int] = None
    status: Optional[str] = None
    is_corrected: Optional[bool] = None
    final_score: Optional[float] = None
    feedback: Optional[str] = None
    risk_score: Optional[float] = None
    answers: List[SubmissionAnswerOut] = Field(default_factory=list)

    class Config:
        from_attributes = True
