import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import and_
from sqlalchemy.orm import Session
from ..models import Event, Venue, VenueHireRequest, utcnow
from .messages import AgentMessage
from .base import record_agent_belief


def as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class VenueAgent:
    name = "VenueAgent"
    TURNAROUND_BUFFER_MINUTES = 30

    def _conflicts(self, db: Session, venue_id: int, start: datetime, end: datetime,
                   exclude_event_id: int | None = None, exclude_hire_id: int | None = None) -> list[str]:
        buffer = timedelta(minutes=self.TURNAROUND_BUFFER_MINUTES)
        query = db.query(Event).filter(
            Event.venue_id == venue_id,
            Event.status != "cancelled",
            Event.start_at < end + buffer,
            Event.end_at > start - buffer,
        )
        if exclude_event_id:
            query = query.filter(Event.id != exclude_event_id)
        conflicts = [f"event:{event.id}:{event.title}" for event in query.all()]
        hires = db.query(VenueHireRequest).filter(
            VenueHireRequest.venue_id == venue_id,
            VenueHireRequest.status == "approved",
            VenueHireRequest.start_at < end + buffer,
            VenueHireRequest.end_at > start - buffer,
        )
        if exclude_hire_id:
            hires = hires.filter(VenueHireRequest.id != exclude_hire_id)
        conflicts.extend(f"hire:{hire.id}" for hire in hires.all())
        return conflicts

    def _price(self, venue: Venue, start: datetime, end: datetime) -> float:
        hours = (end - start).total_seconds() / 3600
        return round(hours * venue.hourly_hire_rate, 2)

    def _alternatives(self, db: Session, venue: Venue, start: datetime, end: datetime, guests: int) -> list[dict]:
        duration = end - start
        options: list[dict] = []
        # Offer alternative times at the requested venue only when that venue
        # is actually eligible for this guest count and private hire.
        if venue.open_for_hire and guests <= venue.capacity:
            for days in (1, 2, 7, 14):
                candidate_start = start + timedelta(days=days)
                candidate_end = candidate_start + duration
                if not self._conflicts(db, venue.id, candidate_start, candidate_end):
                    options.append({
                        "venue_id": venue.id, "venue_name": venue.name,
                        "start_at": candidate_start.isoformat(), "end_at": candidate_end.isoformat(),
                        "price": self._price(venue, candidate_start, candidate_end),
                        "reason": "Alternative available time at the requested venue",
                    })
        # Also offer other venues at the requested time.
        for other in db.query(Venue).filter(Venue.id != venue.id, Venue.open_for_hire.is_(True), Venue.capacity >= guests).limit(10).all():
            if not self._conflicts(db, other.id, start, end):
                options.append({
                    "venue_id": other.id, "venue_name": other.name,
                    "start_at": start.isoformat(), "end_at": end.isoformat(),
                    "price": self._price(other, start, end),
                    "reason": "Alternative venue available at the requested time",
                })
        return options[:5]

    def handle(self, message: AgentMessage, db: Session, bus):
        record_agent_belief(db, self.name, message)
        action = message.content.get("action")
        if message.performative == "REQUEST" and action == "check_event_slot":
            venue = db.get(Venue, int(message.content["venue_id"]))
            if venue is None:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "Venue does not exist"}, conversation_id=message.conversation_id)
            start, end = as_datetime(message.content["start_at"]), as_datetime(message.content["end_at"])
            conflicts = self._conflicts(db, venue.id, start, end, message.content.get("event_id"))
            if conflicts:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "Venue time slot conflicts with another activity", "conflicts": conflicts},
                                    conversation_id=message.conversation_id)
            return AgentMessage(sender=self.name, receiver=message.sender, performative="AGREE",
                                content={"reason": "Venue slot is available", "turnaround_buffer_minutes": self.TURNAROUND_BUFFER_MINUTES},
                                conversation_id=message.conversation_id)

        if message.performative == "REQUEST" and action == "evaluate_hire":
            request = db.get(VenueHireRequest, int(message.content["request_id"]))
            if request is None:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "Venue hire request not found"}, conversation_id=message.conversation_id)
            venue = db.get(Venue, request.venue_id)
            reason = None
            if not venue or not venue.open_for_hire:
                reason = "The selected venue is not open for private hire"
            elif request.guest_count > venue.capacity:
                reason = f"Guest count exceeds venue capacity ({venue.capacity})"
            else:
                conflicts = self._conflicts(db, venue.id, request.start_at, request.end_at)
                if conflicts:
                    reason = "The requested time conflicts with another venue activity"
            if reason:
                alternatives = self._alternatives(db, venue, request.start_at, request.end_at, request.guest_count) if venue else []
                request.status = "escalated" if alternatives else "rejected"
                request.decision_reason = reason
                request.alternatives_json = json.dumps(alternatives)
                db.flush()
                bus.inform_notification(self.name, {
                    "user_id": request.user_id, "title": "Venue hire request needs attention",
                    "message": reason + (" Alternative times or venues are available." if alternatives else " No suitable alternatives were found."),
                    "request_id": request.id,
                }, db, message.conversation_id)
                if alternatives:
                    return AgentMessage(sender=self.name, receiver=message.sender, performative="PROPOSE",
                                        content={"reason": reason, "alternatives": alternatives, "request_id": request.id},
                                        conversation_id=message.conversation_id)
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": reason, "alternatives": [], "request_id": request.id},
                                    conversation_id=message.conversation_id)
            request.quoted_price = self._price(venue, request.start_at, request.end_at)
            request.status = "approved"
            request.decision_reason = "Automatically approved: venue is available and capacity is sufficient"
            db.flush()
            bus.inform_notification(self.name, {
                "user_id": request.user_id, "title": "Venue hire confirmed",
                "message": f"Your request for {venue.name} has been approved. Estimated hire price: {request.quoted_price:.2f}.",
                "request_id": request.id,
            }, db, message.conversation_id)
            return AgentMessage(sender=self.name, receiver=message.sender, performative="AGREE",
                                content={"request_id": request.id, "price": request.quoted_price, "reason": request.decision_reason},
                                conversation_id=message.conversation_id)

        if message.performative == "REQUEST" and action == "admin_hire_decision":
            request = db.get(VenueHireRequest, int(message.content["request_id"]))
            if request is None or request.status not in {"escalated", "pending"}:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "Request is not awaiting an administrator decision"}, conversation_id=message.conversation_id)
            decision = message.content.get("decision")
            if decision == "approve":
                venue = db.get(Venue, request.venue_id)
                conflicts = self._conflicts(db, venue.id, request.start_at, request.end_at)
                if conflicts or request.guest_count > venue.capacity or not venue.open_for_hire:
                    return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                        content={"reason": "Request can no longer be approved because availability or capacity changed", "conflicts": conflicts},
                                        conversation_id=message.conversation_id)
                request.status = "approved"
                request.quoted_price = self._price(venue, request.start_at, request.end_at)
            elif decision == "reject":
                request.status = "rejected"
            else:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "Decision must be approve or reject"}, conversation_id=message.conversation_id)
            request.decision_reason = message.content.get("reason") or f"Administrator {decision}d the request"
            db.flush()
            bus.inform_notification(self.name, {
                "user_id": request.user_id, "title": f"Venue hire {request.status}",
                "message": request.decision_reason, "request_id": request.id,
            }, db, message.conversation_id)
            return AgentMessage(sender=self.name, receiver=message.sender, performative="AGREE",
                                content={"request_id": request.id, "status": request.status, "price": request.quoted_price},
                                conversation_id=message.conversation_id)
        return None
