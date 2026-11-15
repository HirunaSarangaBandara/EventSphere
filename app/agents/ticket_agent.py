import secrets
from datetime import timedelta
from sqlalchemy import update
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Booking, Event, Ticket, utcnow
from .messages import AgentMessage
from .base import record_agent_belief


class TicketAgent:
    name = "TicketAgent"

    def handle(self, message: AgentMessage, db: Session, bus):
        record_agent_belief(db, self.name, message)
        action = message.content.get("action")
        if message.performative == "REQUEST" and action == "issue_tickets":
            booking = db.get(Booking, int(message.content["booking_id"]))
            if booking is None or booking.status != "confirmed":
                return AgentMessage(sender=self.name, receiver=message.sender, performative="FAILURE",
                                    content={"reason": "Cannot issue tickets for a missing or inactive booking"}, conversation_id=message.conversation_id)
            tickets = []
            for _ in range(booking.quantity):
                code = secrets.token_urlsafe(32)
                ticket = Ticket(booking_id=booking.id, event_id=booking.event_id, code=code, status="valid")
                db.add(ticket)
                tickets.append(code)
            db.flush()
            return AgentMessage(sender=self.name, receiver=message.sender, performative="INFORM",
                                content={"booking_id": booking.id, "issued_count": len(tickets)}, conversation_id=message.conversation_id)

        if message.performative == "REQUEST" and action == "check_in":
            code = str(message.content.get("code", ""))
            event_id = int(message.content.get("event_id", 0))
            ticket = db.query(Ticket).filter(Ticket.code == code).first()
            if ticket is None:
                reason = "Ticket code was not found"
                accepted = False
            elif ticket.event_id != event_id:
                reason = "Ticket belongs to a different event"
                accepted = False
            elif ticket.status == "cancelled":
                reason = "Ticket has been cancelled"
                accepted = False
            elif ticket.status == "used":
                reason = "Ticket has already been admitted"
                accepted = False
            else:
                event = db.get(Event, event_id)
                now = utcnow()
                if event is None or event.status == "cancelled":
                    reason, accepted = "Event is cancelled or does not exist", False
                elif now < event.start_at - timedelta(minutes=settings.checkin_early_minutes):
                    reason, accepted = "Check-in has not opened yet", False
                elif now > event.end_at + timedelta(minutes=settings.checkin_late_minutes):
                    reason, accepted = "Check-in window has closed", False
                else:
                    result = db.execute(update(Ticket).where(
                        Ticket.id == ticket.id, Ticket.status == "valid"
                    ).values(status="used", checked_in_at=now))
                    if result.rowcount == 1:
                        reason, accepted = "Ticket admitted successfully", True
                    else:
                        reason, accepted = "Ticket has already been admitted", False
                    db.flush()
            bus.inform_notification(self.name, {
                "title": "Ticket check-in", "message": reason,
                "ticket_code_suffix": code[-6:], "check_in_accepted": accepted,
            }, db, message.conversation_id)
            return AgentMessage(sender=self.name, receiver=message.sender,
                                performative="AGREE" if accepted else "REFUSE",
                                content={"accepted": accepted, "reason": reason}, conversation_id=message.conversation_id)
        return None
