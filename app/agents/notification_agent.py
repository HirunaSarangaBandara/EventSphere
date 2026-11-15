from sqlalchemy.orm import Session
from ..models import Notification
from .messages import AgentMessage
from .base import record_agent_belief


class NotificationAgent:
    name = "NotificationAgent"

    def handle(self, message: AgentMessage, db: Session, bus):
        record_agent_belief(db, self.name, message)
        if message.performative not in {"INFORM", "PROPOSE", "REFUSE"}:
            return None
        user_id = message.content.get("user_id")
        title = message.content.get("title")
        body = message.content.get("message") or message.content.get("reason")
        if user_id and title and body:
            db.add(Notification(user_id=int(user_id), title=str(title), message=str(body)))
            db.flush()
        return None
