import json
from datetime import datetime
from typing import Protocol
from sqlalchemy.orm import Session
from ..models import AgentMessageLog, AgentState, utcnow
from .messages import AgentMessage


class Agent(Protocol):
    name: str
    def handle(self, message: AgentMessage, db: Session, bus: "MessageBus") -> AgentMessage | None: ...


def record_agent_belief(db: Session, agent_name: str, message: AgentMessage) -> None:
    """Persist per-agent counters and the last decision context across restarts."""
    row = db.get(AgentState, agent_name)
    state = json.loads(row.value_json) if row and row.value_json else {}
    state["messages_handled"] = int(state.get("messages_handled", 0)) + 1
    state["last_action"] = message.content.get("action", "message")
    state["last_performative"] = message.performative.value
    state["last_sender"] = message.sender
    state["last_conversation_id"] = message.conversation_id
    state["last_handled_at_utc"] = utcnow().isoformat() + "Z"
    if row is None:
        row = AgentState(key=agent_name, value_json=json.dumps(state, default=str), updated_at=utcnow())
        db.add(row)
    else:
        row.value_json = json.dumps(state, default=str)
        row.updated_at = utcnow()
    db.flush()


class MessageBus:
    """Synchronous JSON message router. Agents only interact with peers via this bus."""
    def __init__(self, agents: list[Agent]):
        self.agents = {agent.name: agent for agent in agents}

    def send(self, message: AgentMessage, db: Session) -> AgentMessage | None:
        self._log(message, db)
        agent = self.agents.get(message.receiver)
        if agent is None:
            response = AgentMessage(
                sender="MessageBus", receiver=message.sender, performative="FAILURE",
                content={"reason": f"Unknown receiver: {message.receiver}"},
                conversation_id=message.conversation_id,
            )
            self._log(response, db)
            return response
        try:
            response = agent.handle(message, db, self)
            if response is not None:
                self._log(response, db)
                reply_agent = self.agents.get(response.receiver)
                if reply_agent is not None:
                    reply_agent.handle(response, db, self)
            db.flush()
            return response
        except Exception as exc:
            db.rollback()
            failure = AgentMessage(
                sender=message.receiver, receiver=message.sender, performative="FAILURE",
                content={"reason": "Agent processing failed", "detail": str(exc)},
                conversation_id=message.conversation_id,
            )
            self._log(failure, db)
            return failure

    def _log(self, message: AgentMessage, db: Session) -> None:
        db.add(AgentMessageLog(
            conversation_id=message.conversation_id,
            sender=message.sender,
            receiver=message.receiver,
            performative=message.performative.value,
            content_json=json.dumps(message.content, default=str),
        ))

    def inform_notification(self, sender: str, content: dict, db: Session, conversation_id: str | None = None) -> None:
        self.send(AgentMessage(
            sender=sender, receiver="NotificationAgent", performative="INFORM",
            content=content, conversation_id=conversation_id or AgentMessage(sender=sender, receiver="NotificationAgent", performative="INFORM").conversation_id,
        ), db)
