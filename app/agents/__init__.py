from .base import MessageBus
from .booking_agent import BookingAgent
from .notification_agent import NotificationAgent
from .ticket_agent import TicketAgent
from .venue_agent import VenueAgent
from .waitlist_agent import WaitlistAgent


def create_message_bus() -> MessageBus:
    return MessageBus([
        VenueAgent(), BookingAgent(), TicketAgent(), WaitlistAgent(), NotificationAgent(),
    ])
