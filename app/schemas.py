from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

Role = Literal["EMPLOYEE", "MANAGER", "ADMIN"]
Status = Literal[
    "NEW",
    "IN_REVIEW",
    "IN_PROGRESS",
    "AWAITING_CLARIFICATION",
    "IMPLEMENTED",
    "REJECTED",
    "CLOSED",
]
Priority = Literal["LOW", "MEDIUM", "HIGH"]
TicketType = Literal["PROBLEM", "PROPOSAL", "PRAISE"]


class Login(BaseModel):
    email: EmailStr
    password: str


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    full_name: str
    role: Role
    department: str
    position: str
    is_active: bool


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: str = Field(min_length=1, max_length=255)
    role: Role = "EMPLOYEE"
    department: str = Field(min_length=1, max_length=150)
    position: str = Field(default="", max_length=150)
    is_active: bool = True


class UserPatch(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=72)
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: Role | None = None
    department: str | None = Field(default=None, min_length=1, max_length=150)
    position: str | None = None
    is_active: bool | None = None


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    default_assignee_id: int | None = None
    sla_first_response_hours: int = Field(default=48, ge=1, le=8760)
    is_active: bool = True
    sort_order: int = 0


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    default_assignee_id: int | None = None
    sla_first_response_hours: int | None = Field(default=None, ge=1, le=8760)
    is_active: bool | None = None
    sort_order: int | None = None


class TicketCreate(BaseModel):
    onec_item_ref: UUID | None = None
    onec_document_ref: UUID | None = None
    onec_document_kind: Literal["purchase", "sale"] = "purchase"
    onec_scope: str | None = Field(default=None,max_length=80)
    self_service_attempt_id: UUID | None = None
    is_shared: bool = False
    type: TicketType
    category_id: int
    title: str = Field(min_length=3, max_length=250)
    description: str = Field(min_length=10, max_length=20000)
    proposed_solution: str | None = Field(default=None, max_length=20000)

    @field_validator("title", "description", mode="before")
    @classmethod
    def strip_content(cls, value):
        return value.strip() if isinstance(value, str) else value


class TicketPatch(BaseModel):
    is_shared: bool | None = None
    status: Status | None = None
    priority: Priority | None = None
    assignee_id: int | None = None
    resolution: str | None = Field(default=None, max_length=20000)
    expected_status: Status | None = None
    expected_resolution_review_version: int | None = Field(default=None, ge=0)


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)
    is_clarification_request: bool = False


class ResolutionDeadlinePatch(BaseModel):
    resolution_due_at: datetime | None
    expected_version: int = Field(ge=0)
    reason: str = Field(default="", max_length=2000)

    @field_validator("resolution_due_at")
    @classmethod
    def utc_deadline(cls, value):
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Укажите часовой пояс срока решения")
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return value.strip() if isinstance(value, str) else value


class ResolutionReviewCreate(BaseModel):
    decision: Literal["confirm", "rework"]
    expected_version: int = Field(ge=0)
    explanation: str = Field(default="", max_length=10000)

    @field_validator("explanation", mode="before")
    @classmethod
    def strip_explanation(cls, value):
        return value.strip() if isinstance(value, str) else value
