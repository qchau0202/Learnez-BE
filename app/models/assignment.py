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
    due_date: Optional[datetime] = None
    total_score: Optional[float] = None
    is_graded: bool = False
    questions: Optional[List[QuestionCreate]] = None


class AssignmentUpdate(BaseModel):
    module_id: Optional[int] = None
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=5000)
    due_date: Optional[datetime] = None
    total_score: Optional[float] = None
    is_graded: Optional[bool] = None


class AssignmentOut(BaseModel):
    id: int
    module_id: Optional[int] = None
    created_at: datetime
    description: Optional[str] = None
    due_date: Optional[datetime] = None
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


class SubmissionUpdate(BaseModel):
    answers: Optional[List[AnswerIn]] = None
    status: Optional[Literal["draft", "submitted"]] = None


class GradeAnswerIn(BaseModel):
    question_id: int
    earned_score: float
    is_correct: Optional[bool] = None
    ai_feedback: Optional[str] = None


class SubmissionGradeIn(BaseModel):
    answer_grades: List[GradeAnswerIn] = Field(default_factory=list)
    feedback: Optional[str] = None
    finalize: bool = True


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
