from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from typing import Literal


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: EmailStr
    full_name: str
    role: str


class VenueCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    location: str = Field(min_length=2, max_length=255)
    capacity: int = Field(gt=0)
    description: str = ""
    image_url: str | None = None
    hourly_hire_rate: float = Field(ge=0)
    open_for_hire: bool = True


class VenueUpdate(BaseModel):
    name: str | None = None
    location: str | None = None
    capacity: int | None = Field(default=None, gt=0)
    description: str | None = None
    image_url: str | None = None
    hourly_hire_rate: float | None = Field(default=None, ge=0)
    open_for_hire: bool | None = None


class VenueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    location: str
    capacity: int
    description: str
    image_url: str | None
    hourly_hire_rate: float
    open_for_hire: bool


class TicketCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price: float = Field(ge=0)
    quantity: int = Field(gt=0)


class EventCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    category: str = Field(min_length=2, max_length=100)
    venue_id: int
    start_at: datetime
    end_at: datetime
    description: str = ""
    status: str = "draft"
    ticket_categories: list[TicketCategoryCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates_and_status(self):
        if self.end_at <= self.start_at:
            raise ValueError("Event end time must be after start time")
        if self.status not in {"draft", "published"}:
            raise ValueError("Status must be draft or published")
        return self


class EventUpdate(BaseModel):
    title: str | None = None
    category: str | None = None
    venue_id: int | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    description: str | None = None
    status: str | None = None


class TicketCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    price: float
    quantity: int
    quantity_sold: int
    quantity_held: int


class EventOut(BaseModel):
    id: int
    title: str
    category: str
    venue_id: int
    venue_name: str
    venue_location: str
    start_at: datetime
    end_at: datetime
    description: str
    status: str
    ticket_categories: list[dict]


class BookingRequest(BaseModel):
    ticket_category_id: int
    quantity: int = Field(ge=1, le=6)


class CancelResponse(BaseModel):
    booking_id: int
    status: str
    refund_amount: float
    refund_policy: str


class CheckInRequest(BaseModel):
    code: str = Field(min_length=10, max_length=100)
    event_id: int


class VenueHireCreate(BaseModel):
    venue_id: int
    start_at: datetime
    end_at: datetime
    purpose: str = Field(min_length=3, max_length=255)
    guest_count: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_at <= self.start_at:
            raise ValueError("Hire end time must be after start time")
        return self


class AdminHireDecision(BaseModel):
    decision: str
    reason: str = ""


class WaitlistJoin(BaseModel):
    ticket_category_id: int
    quantity: int = Field(ge=1, le=6)


class WaitlistClaim(BaseModel):
    entry_id: int


class AgentMessageOut(BaseModel):
    id: int
    conversation_id: str
    sender: str
    receiver: str
    performative: str
    content: dict
    created_at: datetime
