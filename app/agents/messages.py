from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4
from pydantic import BaseModel, Field


class Performative(StrEnum):
    REQUEST = "REQUEST"
    AGREE = "AGREE"
    REFUSE = "REFUSE"
    PROPOSE = "PROPOSE"
    INFORM = "INFORM"
    FAILURE = "FAILURE"


class AgentMessage(BaseModel):
    sender: str
    receiver: str
    performative: Performative
    content: dict = Field(default_factory=dict)
    conversation_id: str = Field(default_factory=lambda: str(uuid4()))
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
