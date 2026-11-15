import secrets
from datetime import timedelta
from sqlalchemy import update
from sqlalchemy.orm import Session
from ..models import Booking, Event, Payment, Ticket, TicketCategory, WaitlistEntry, utcnow
from .messages import AgentMessage
from .base import record_agent_belief


class BookingAgent:
    name = "BookingAgent"
    MAX_TICKETS_PER_BOOKING = 6

    def _create_booking(self, message: AgentMessage, db: Session, bus) -> AgentMessage:
        content = message.content
        user_id = int(content["user_id"])
        category_id = int(content["ticket_category_id"])
        quantity = int(content["quantity"])
        waitlist_entry_id = content.get("waitlist_entry_id")
        if quantity < 1 or quantity > self.MAX_TICKETS_PER_BOOKING:
            return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                content={"reason": f"A booking must contain between 1 and {self.MAX_TICKETS_PER_BOOKING} tickets"}, conversation_id=message.conversation_id)
        category = db.get(TicketCategory, category_id)
        if category is None:
            return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                content={"reason": "Ticket category does not exist"}, conversation_id=message.conversation_id)
        event = db.get(Event, category.event_id)
        if event is None or event.status != "published":
            return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                content={"reason": "Event is not available for booking"}, conversation_id=message.conversation_id)
        if event.start_at <= utcnow():
            return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                content={"reason": "Bookings are closed because the event has started"}, conversation_id=message.conversation_id)
        if waitlist_entry_id:
            entry = db.get(WaitlistEntry, int(waitlist_entry_id))
            if entry is None or entry.user_id != user_id or entry.ticket_category_id != category_id or entry.status != "offered":
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "The waitlist offer is not valid for this attendee"}, conversation_id=message.conversation_id)
            if entry.hold_expires_at is None or entry.hold_expires_at <= utcnow() or entry.quantity != quantity:
                return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                    content={"reason": "The waitlist seat hold has expired or no longer matches"}, conversation_id=message.conversation_id)
            allocation = db.execute(update(TicketCategory).where(
                TicketCategory.id == category_id, TicketCategory.quantity_held >= quantity
            ).values(
                quantity_held=TicketCategory.quantity_held - quantity,
                quantity_sold=TicketCategory.quantity_sold + quantity,
            ))
        else:
            allocation = db.execute(update(TicketCategory).where(
                TicketCategory.id == category_id,
                TicketCategory.quantity_sold + TicketCategory.quantity_held + quantity <= TicketCategory.quantity,
            ).values(quantity_sold=TicketCategory.quantity_sold + quantity))
        if allocation.rowcount != 1:
            db.rollback()
            return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                content={"reason": "Not enough tickets are available; no tickets were sold"}, conversation_id=message.conversation_id)
        # Refresh the category after the atomic inventory update.
        db.expire(category)
        total = round(category.price * quantity, 2)
        booking = Booking(
            reference="ES-" + secrets.token_hex(6).upper(), user_id=user_id,
            event_id=event.id, ticket_category_id=category.id, quantity=quantity,
            total_amount=total, status="confirmed",
        )
        db.add(booking)
        db.flush()
        payment = Payment(booking_id=booking.id, reference="SIM-" + secrets.token_hex(8).upper(), amount=total, status="simulated_paid")
        db.add(payment)
        db.flush()
        ticket_response = bus.send(AgentMessage(
            sender=self.name, receiver="TicketAgent", performative="REQUEST",
            content={"action": "issue_tickets", "booking_id": booking.id},
            conversation_id=message.conversation_id,
        ), db)
        if ticket_response is None or ticket_response.performative == "FAILURE":
            booking.status = "failed"
            db.execute(update(TicketCategory).where(TicketCategory.id == category.id).values(quantity_sold=TicketCategory.quantity_sold - quantity))
            db.flush()
            return AgentMessage(sender=self.name, receiver=message.sender, performative="FAILURE",
                                content={"reason": "Ticket issuance failed; booking was rolled back"}, conversation_id=message.conversation_id)
        bus.inform_notification(self.name, {
            "user_id": user_id, "title": "Booking confirmed",
            "message": f"Your booking {booking.reference} for {event.title} is confirmed. Simulated payment reference: {payment.reference}.",
            "booking_id": booking.id,
        }, db, message.conversation_id)
        return AgentMessage(sender=self.name, receiver=message.sender, performative="AGREE",
                            content={"booking_id": booking.id, "booking_reference": booking.reference,
                                     "payment_reference": payment.reference, "amount": total,
                                     "issued_tickets": quantity}, conversation_id=message.conversation_id)

    def _cancel_booking(self, message: AgentMessage, db: Session, bus) -> AgentMessage:
        booking = db.get(Booking, int(message.content["booking_id"]))
        requester_id = int(message.content["user_id"])
        if booking is None or booking.user_id != requester_id:
            return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                content={"reason": "Booking was not found for this attendee"}, conversation_id=message.conversation_id)
        if booking.status != "confirmed":
            return AgentMessage(sender=self.name, receiver=message.sender, performative="REFUSE",
                                content={"reason": "Only confirmed bookings can be cancelled"}, conversation_id=message.conversation_id)
        event = db.get(Event, booking.event_id)
        force_full = bool(message.content.get("event_cancelled", False))
        days_until_event = (event.start_at - utcnow()).total_seconds() / 86400
        if force_full:
            refund = booking.total_amount
            policy = "Full refund because the organiser cancelled the event"
        elif days_until_event >= 7:
            refund = booking.total_amount
            policy = "100% refund when cancelled at least 7 days before the event"
        elif days_until_event >= 2:
            refund = round(booking.total_amount * 0.5, 2)
            policy = "50% refund when cancelled 2 to 7 days before the event"
        else:
            refund = 0.0
            policy = "No refund when cancelled less than 48 hours before the event"
        booking.status = "cancelled"
        booking.refund_amount = refund
        payment = db.query(Payment).filter(Payment.booking_id == booking.id).first()
        if payment:
            payment.status = "refunded" if refund == booking.total_amount else ("partially_refunded" if refund > 0 else "not_refundable")
        db.query(Ticket).filter(Ticket.booking_id == booking.id).update({"status": "cancelled"}, synchronize_session=False)
        db.execute(update(TicketCategory).where(TicketCategory.id == booking.ticket_category_id).values(
            quantity_sold=TicketCategory.quantity_sold - booking.quantity
        ))
        db.flush()
        bus.inform_notification(self.name, {
            "user_id": booking.user_id, "title": "Booking cancelled",
            "message": f"Booking {booking.reference} was cancelled. Refund: {refund:.2f}. {policy}",
            "booking_id": booking.id,
        }, db, message.conversation_id)
        if not force_full and event.status != "cancelled":
            bus.send(AgentMessage(sender=self.name, receiver="WaitlistAgent", performative="REQUEST",
                                  content={"action": "offer_freed", "ticket_category_id": booking.ticket_category_id,
                                           "freed_quantity": booking.quantity},
                                  conversation_id=message.conversation_id), db)
        return AgentMessage(sender=self.name, receiver=message.sender, performative="AGREE",
                            content={"booking_id": booking.id, "status": "cancelled", "refund_amount": refund,
                                     "refund_policy": policy}, conversation_id=message.conversation_id)

    def handle(self, message: AgentMessage, db: Session, bus):
        record_agent_belief(db, self.name, message)
        action = message.content.get("action")
        if message.performative == "REQUEST" and action in {"create_booking", "create_from_waitlist"}:
            return self._create_booking(message, db, bus)
        if message.performative == "REQUEST" and action == "cancel_booking":
            return self._cancel_booking(message, db, bus)
        if message.performative == "REQUEST" and action == "refund_event_bookings":
            event_id = int(message.content["event_id"])
            bookings = db.query(Booking).filter(Booking.event_id == event_id, Booking.status == "confirmed").all()
            total_refunded = 0.0
            for booking in bookings:
                followup = AgentMessage(sender=message.sender, receiver=self.name, performative="REQUEST",
                                        content={"action": "cancel_booking", "booking_id": booking.id,
                                                 "user_id": booking.user_id, "event_cancelled": True},
                                        conversation_id=message.conversation_id)
                self._cancel_booking(followup, db, bus)
                total_refunded += booking.total_amount
            # Closing an event also closes outstanding waitlist entries and
            # releases any unclaimed timed holds through the message bus.
            bus.send(AgentMessage(sender=self.name, receiver="WaitlistAgent", performative="REQUEST",
                                  content={"action": "cancel_event_waitlist", "event_id": event_id},
                                  conversation_id=message.conversation_id), db)
            return AgentMessage(sender=self.name, receiver=message.sender, performative="INFORM",
                                content={"event_id": event_id, "bookings_refunded": len(bookings), "total_refunded": round(total_refunded, 2)},
                                conversation_id=message.conversation_id)
        return None
