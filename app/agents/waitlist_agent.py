from datetime import timedelta
from sqlalchemy import update
from sqlalchemy.orm import Session
from ..config import settings
from ..models import Event, TicketCategory, WaitlistEntry, utcnow
from .messages import AgentMessage
from .base import record_agent_belief


class WaitlistAgent:
    name = "WaitlistAgent"

    def _offer_next(self, category_id: int, available_count: int, db: Session, bus, conversation_id: str) -> int:
        category = db.get(TicketCategory, category_id)
        if category is None or available_count <= 0:
            return 0
        entries = db.query(WaitlistEntry).filter(
            WaitlistEntry.ticket_category_id == category_id,
            WaitlistEntry.status == "waiting",
        ).order_by(WaitlistEntry.created_at.asc(), WaitlistEntry.id.asc()).all()
        for entry in entries:
            if entry.quantity > available_count:
                # Preserve queue order: do not skip an older request for a smaller one.
                break
            result = db.execute(update(TicketCategory).where(
                TicketCategory.id == category_id,
                TicketCategory.quantity_sold + TicketCategory.quantity_held + entry.quantity <= TicketCategory.quantity,
            ).values(quantity_held=TicketCategory.quantity_held + entry.quantity))
            if result.rowcount != 1:
                break
            entry.status = "offered"
            entry.hold_expires_at = utcnow() + timedelta(minutes=settings.waitlist_hold_minutes)
            available_count -= entry.quantity
            bus.inform_notification(self.name, {
                "user_id": entry.user_id, "title": "Tickets available from the waitlist",
                "message": f"{entry.quantity} ticket(s) are held for you. Claim before {entry.hold_expires_at.isoformat()} UTC.",
                "waitlist_entry_id": entry.id, "hold_expires_at": entry.hold_expires_at.isoformat(),
            }, db, conversation_id)
            # Only one active offer per category at a time. Remaining availability is offered after claim/expiry.
            break
        db.flush()
        return available_count

    def handle(self, message: AgentMessage, db: Session, bus):
        record_agent_belief(db, self.name, message)
        action = message.content.get("action")
        if message.performative == "REQUEST" and action == "join_waitlist":
            category = db.get(TicketCategory, int(message.content["ticket_category_id"]))
            if category is None:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "Ticket category does not exist"}, conversation_id=message.conversation_id)
            event = db.get(Event, category.event_id)
            quantity = int(message.content["quantity"])
            if event is None or event.status != "published" or event.start_at <= utcnow():
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "Waitlist is not available for this event"}, conversation_id=message.conversation_id)
            if category.quantity - category.quantity_sold - category.quantity_held >= quantity:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "Tickets are still available; book directly instead of joining the waitlist"}, conversation_id=message.conversation_id)
            existing = db.query(WaitlistEntry).filter(
                WaitlistEntry.user_id == int(message.content["user_id"]),
                WaitlistEntry.ticket_category_id == category.id,
                WaitlistEntry.status.in_(["waiting", "offered"]),
            ).first()
            if existing:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "You already have an active waitlist entry for this category", "entry_id": existing.id}, conversation_id=message.conversation_id)
            entry = WaitlistEntry(user_id=int(message.content["user_id"]), event_id=event.id,
                                  ticket_category_id=category.id, quantity=quantity, status="waiting")
            db.add(entry)
            db.flush()
            bus.inform_notification(self.name, {
                "user_id": entry.user_id, "title": "Added to event waitlist",
                "message": f"You have joined the waitlist for {event.title}.", "waitlist_entry_id": entry.id,
            }, db, message.conversation_id)
            return AgentMessage(sender=self.name, receiver=message.sender, performative="AGREE",
                                content={"entry_id": entry.id, "status": entry.status, "position": len(db.query(WaitlistEntry).filter(
                                    WaitlistEntry.ticket_category_id == category.id, WaitlistEntry.status == "waiting").all())},
                                conversation_id=message.conversation_id)

        if message.performative == "REQUEST" and action == "offer_freed":
            category_id = int(message.content["ticket_category_id"])
            self._offer_next(category_id, int(message.content["freed_quantity"]), db, bus, message.conversation_id)
            return AgentMessage(sender=self.name, receiver=message.sender, performative="INFORM",
                                content={"ticket_category_id": category_id, "action": "waitlist_offer_sweep_completed"},
                                conversation_id=message.conversation_id)

        if message.performative == "REQUEST" and action == "claim_offer":
            entry = db.get(WaitlistEntry, int(message.content["entry_id"]))
            user_id = int(message.content["user_id"])
            if entry is None or entry.user_id != user_id or entry.status != "offered":
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "No active waitlist offer was found"}, conversation_id=message.conversation_id)
            if entry.hold_expires_at is None or entry.hold_expires_at <= utcnow():
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "The waitlist offer has expired"}, conversation_id=message.conversation_id)
            response = bus.send(AgentMessage(sender=self.name, receiver="BookingAgent", performative="REQUEST",
                                             content={"action": "create_from_waitlist", "user_id": user_id,
                                                      "ticket_category_id": entry.ticket_category_id,
                                                      "quantity": entry.quantity, "waitlist_entry_id": entry.id},
                                             conversation_id=message.conversation_id), db)
            if response and response.performative == "AGREE":
                entry.status = "claimed"
                entry.hold_expires_at = None
                db.flush()
                return AgentMessage(sender=self.name, receiver=message.sender, performative="AGREE",
                                    content={"entry_id": entry.id, "booking": response.content}, conversation_id=message.conversation_id)
            return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                content={"reason": response.content.get("reason", "The booking could not be completed") if response else "Booking agent did not respond"},
                                conversation_id=message.conversation_id)

        if message.performative == "REQUEST" and action == "cancel_event_waitlist":
            event_id = int(message.content["event_id"])
            entries = db.query(WaitlistEntry).filter(
                WaitlistEntry.event_id == event_id,
                WaitlistEntry.status.in_(["waiting", "offered"]),
            ).all()
            released_holds = 0
            for entry in entries:
                if entry.status == "offered":
                    result = db.execute(update(TicketCategory).where(
                        TicketCategory.id == entry.ticket_category_id,
                        TicketCategory.quantity_held >= entry.quantity,
                    ).values(quantity_held=TicketCategory.quantity_held - entry.quantity))
                    if result.rowcount == 1:
                        released_holds += entry.quantity
                entry.status = "cancelled"
                entry.hold_expires_at = None
                bus.inform_notification(self.name, {
                    "user_id": entry.user_id, "title": "Event waitlist closed",
                    "message": "The event was cancelled, so its waitlist and any temporary ticket hold have been closed.",
                    "waitlist_entry_id": entry.id,
                }, db, message.conversation_id)
            db.flush()
            return AgentMessage(sender=self.name, receiver=message.sender, performative="INFORM",
                                content={"event_id": event_id, "entries_cancelled": len(entries),
                                         "held_tickets_released": released_holds},
                                conversation_id=message.conversation_id)

        if message.performative == "REQUEST" and action == "sweep_expired":
            expired = db.query(WaitlistEntry).filter(
                WaitlistEntry.status == "offered", WaitlistEntry.hold_expires_at <= utcnow()
            ).all()
            total_expired = 0
            for entry in expired:
                db.execute(update(TicketCategory).where(TicketCategory.id == entry.ticket_category_id).values(
                    quantity_held=TicketCategory.quantity_held - entry.quantity
                ))
                entry.status = "expired"
                entry.hold_expires_at = None
                total_expired += 1
                bus.inform_notification(self.name, {
                    "user_id": entry.user_id, "title": "Waitlist hold expired",
                    "message": "Your temporary ticket hold expired and the seats were offered to the next attendee.",
                    "waitlist_entry_id": entry.id,
                }, db, message.conversation_id)
                self._offer_next(entry.ticket_category_id, entry.quantity, db, bus, message.conversation_id)
            db.flush()
            return AgentMessage(sender=self.name, receiver=message.sender, performative="INFORM",
                                content={"expired_holds": total_expired}, conversation_id=message.conversation_id)
        return None
