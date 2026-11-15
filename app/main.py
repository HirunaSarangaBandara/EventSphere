import asyncio
import io
import json
import secrets
import hmac
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Any

import qrcode
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .agents import create_message_bus
from .agents.messages import AgentMessage
from .config import settings
from .database import SessionLocal, get_db, init_db
from .models import (
    AgentMessageLog, AgentState, Booking, Event, Notification, Payment, Ticket, TicketCategory,
    User, Venue, VenueHireRequest, WaitlistEntry, utcnow,
)
from .schemas import (
    AdminHireDecision, BookingRequest, CheckInRequest, EventCreate, EventUpdate,
    LoginRequest, UserCreate, VenueCreate, VenueHireCreate, VenueUpdate,
    WaitlistClaim, WaitlistJoin,
)
from .security import create_access_token, get_current_user, hash_password, require_roles, verify_password



def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class DoubleSubmitCSRFMiddleware(BaseHTTPMiddleware):
    """Validate the double-submit token whenever a browser CSRF cookie is present."""
    async def dispatch(self, request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            cookie_token = request.cookies.get("csrf_token")
            if cookie_token:
                header_token = request.headers.get("X-CSRF-Token", "")
                if not hmac.compare_digest(cookie_token, header_token):
                    return JSONResponse(status_code=403, content={"detail": "CSRF token is missing or invalid"})
        return await call_next(request)


def run_waitlist_sweep() -> None:
    db = SessionLocal()
    try:
        bus = create_message_bus()
        bus.send(AgentMessage(sender="Scheduler", receiver="WaitlistAgent", performative="REQUEST",
                              content={"action": "sweep_expired"}), db)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


async def waitlist_sweep_loop():
    while True:
        await asyncio.sleep(max(10, settings.waitlist_sweep_seconds))
        await asyncio.to_thread(run_waitlist_sweep)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sweep_task = asyncio.create_task(waitlist_sweep_loop())
    try:
        yield
    finally:
        sweep_task.cancel()
        with suppress(asyncio.CancelledError):
            await sweep_task


app = FastAPI(
    title="EventSphere API",
    description="Mobile-friendly university event booking and venue hire platform powered by cooperating software agents.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)
app.add_middleware(DoubleSubmitCSRFMiddleware)


def get_bus():
    return create_message_bus()


def event_dict(event: Event) -> dict[str, Any]:
    categories = []
    for category in event.ticket_categories:
        left = max(0, category.quantity - category.quantity_sold - category.quantity_held)
        categories.append({
            "id": category.id, "name": category.name, "price": category.price,
            "quantity": category.quantity, "quantity_sold": category.quantity_sold,
            "quantity_held": category.quantity_held, "tickets_left": left,
        })
    return {
        "id": event.id, "title": event.title, "category": event.category,
        "venue_id": event.venue_id, "venue_name": event.venue.name,
        "venue_location": event.venue.location, "venue_capacity": event.venue.capacity,
        "start_at": event.start_at, "end_at": event.end_at,
        "description": event.description, "status": event.status,
        "ticket_categories": categories,
    }


def booking_dict(booking: Booking, db: Session) -> dict:
    event = db.get(Event, booking.event_id)
    category = db.get(TicketCategory, booking.ticket_category_id)
    payment = db.query(Payment).filter(Payment.booking_id == booking.id).first()
    tickets = db.query(Ticket).filter(Ticket.booking_id == booking.id).all()
    return {
        "id": booking.id, "reference": booking.reference, "event_id": booking.event_id,
        "event_title": event.title if event else None,
        "ticket_category": category.name if category else None,
        "quantity": booking.quantity, "total_amount": booking.total_amount,
        "refund_amount": booking.refund_amount, "status": booking.status,
        "created_at": booking.created_at,
        "payment_reference": payment.reference if payment else None,
        "tickets": [{"id": ticket.id, "code": ticket.code, "status": ticket.status} for ticket in tickets],
    }


@app.get("/")
def root():
    """Serve the mobile-friendly EventSphere single-page web client."""
    return FileResponse(Path(__file__).resolve().parent.parent / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/auth/csrf")
def issue_csrf_token(response: Response):
    token = secrets.token_urlsafe(32)
    response.set_cookie("csrf_token", token, httponly=False, secure=settings.csrf_cookie_secure,
                        samesite="strict", path="/", max_age=3600)
    return {"csrf_token": token}


# ------------------------------ Authentication ------------------------------
@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    email = payload.email.lower()
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "An account with this email already exists")
    user = User(email=email, full_name=payload.full_name.strip(), password_hash=hash_password(payload.password), role="attendee")
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user": {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role},
            "access_token": create_access_token(user), "token_type": "bearer"}


@app.post("/api/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(func.lower(User.email) == payload.email.lower()).first()
    if user is None or not verify_password(payload.password, user.password_hash) or not user.is_active:
        raise HTTPException(401, "Incorrect email or password")
    return {"access_token": create_access_token(user), "token_type": "bearer",
            "user": {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role}}


@app.get("/api/auth/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role}


# --------------------------------- Venues ------------------------------------
@app.get("/api/venues")
def list_venues(db: Session = Depends(get_db)):
    venues = db.query(Venue).order_by(Venue.name).all()
    return [{"id": v.id, "name": v.name, "location": v.location, "capacity": v.capacity,
             "description": v.description, "image_url": v.image_url,
             "hourly_hire_rate": v.hourly_hire_rate, "open_for_hire": v.open_for_hire} for v in venues]


@app.post("/api/admin/venues", status_code=201)
def create_venue(payload: VenueCreate, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    venue = Venue(**payload.model_dump())
    db.add(venue)
    db.commit()
    db.refresh(venue)
    return {"id": venue.id, **payload.model_dump()}


@app.patch("/api/admin/venues/{venue_id}")
def update_venue(venue_id: int, payload: VenueUpdate, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    venue = db.get(Venue, venue_id)
    if venue is None:
        raise HTTPException(404, "Venue not found")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(venue, key, value)
    db.commit()
    db.refresh(venue)
    return {"id": venue.id, "name": venue.name, "location": venue.location, "capacity": venue.capacity,
            "description": venue.description, "image_url": venue.image_url,
            "hourly_hire_rate": venue.hourly_hire_rate, "open_for_hire": venue.open_for_hire}


# --------------------------------- Events ------------------------------------
@app.get("/api/events")
def list_events(
    search: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(Event).options(joinedload(Event.venue), joinedload(Event.ticket_categories)).filter(Event.status == "published")
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(Event.title.ilike(term) | Event.description.ilike(term))
    if category:
        query = query.filter(func.lower(Event.category) == category.lower())
    return [event_dict(event) for event in query.order_by(Event.start_at.asc()).all()]


@app.get("/api/events/{event_id}")
def get_event(event_id: int, db: Session = Depends(get_db)):
    event = db.query(Event).options(joinedload(Event.venue), joinedload(Event.ticket_categories)).filter(Event.id == event_id).first()
    if event is None or event.status != "published":
        raise HTTPException(404, "Published event not found")
    return event_dict(event)


@app.get("/api/admin/events")
def admin_list_events(db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    """Return drafts, published and cancelled events to authorised organisers."""
    rows = db.query(Event).options(joinedload(Event.venue), joinedload(Event.ticket_categories)).order_by(Event.start_at.asc()).all()
    return [event_dict(event) for event in rows]


@app.post("/api/admin/events", status_code=201)
def create_event(payload: EventCreate, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    venue = db.get(Venue, payload.venue_id)
    if venue is None:
        raise HTTPException(404, "Venue not found")
    if sum(item.quantity for item in payload.ticket_categories) > venue.capacity:
        raise HTTPException(422, f"Total ticket quantity cannot exceed venue capacity ({venue.capacity})")
    start, end = to_utc_naive(payload.start_at), to_utc_naive(payload.end_at)
    bus = get_bus()
    decision = bus.send(AgentMessage(sender="EventService", receiver="VenueAgent", performative="REQUEST",
                                     content={"action": "check_event_slot", "venue_id": venue.id,
                                              "start_at": start.isoformat(), "end_at": end.isoformat()}), db)
    if decision is None or decision.performative != "AGREE":
        db.rollback()
        reason = decision.content.get("reason", "Venue agent did not approve this time slot") if decision else "Venue agent did not respond"
        raise HTTPException(409, reason)
    event = Event(title=payload.title, category=payload.category, venue_id=venue.id,
                  start_at=start, end_at=end, description=payload.description,
                  status=payload.status, created_by=admin.id)
    db.add(event)
    db.flush()
    for item in payload.ticket_categories:
        db.add(TicketCategory(event_id=event.id, **item.model_dump()))
    db.commit()
    db.refresh(event)
    return event_dict(db.query(Event).options(joinedload(Event.venue), joinedload(Event.ticket_categories)).filter(Event.id == event.id).first())


@app.patch("/api/admin/events/{event_id}")
def update_event(event_id: int, payload: EventUpdate, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    event = db.query(Event).options(joinedload(Event.venue), joinedload(Event.ticket_categories)).filter(Event.id == event_id).first()
    if event is None:
        raise HTTPException(404, "Event not found")
    changes = payload.model_dump(exclude_unset=True)
    new_venue_id = changes.get("venue_id", event.venue_id)
    new_start = to_utc_naive(changes["start_at"]) if changes.get("start_at") else event.start_at
    new_end = to_utc_naive(changes["end_at"]) if changes.get("end_at") else event.end_at
    if new_end <= new_start:
        raise HTTPException(422, "Event end time must be after start time")
    venue = db.get(Venue, new_venue_id)
    if venue is None:
        raise HTTPException(404, "Venue not found")
    if sum(c.quantity for c in event.ticket_categories) > venue.capacity:
        raise HTTPException(422, "Ticket quantity exceeds the new venue capacity")
    if any(k in changes for k in ("venue_id", "start_at", "end_at")):
        decision = get_bus().send(AgentMessage(sender="EventService", receiver="VenueAgent", performative="REQUEST",
                                               content={"action": "check_event_slot", "venue_id": venue.id,
                                                        "start_at": new_start.isoformat(), "end_at": new_end.isoformat(),
                                                        "event_id": event.id}), db)
        if decision is None or decision.performative != "AGREE":
            db.rollback()
            raise HTTPException(409, decision.content.get("reason", "Venue conflict") if decision else "Venue agent did not respond")
    for key, value in changes.items():
        if key in {"start_at", "end_at"} and value is not None:
            value = to_utc_naive(value)
        setattr(event, key, value)
    db.commit()
    db.refresh(event)
    return event_dict(db.query(Event).options(joinedload(Event.venue), joinedload(Event.ticket_categories)).filter(Event.id == event.id).first())


@app.post("/api/admin/events/{event_id}/cancel")
def cancel_event(event_id: int, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(404, "Event not found")
    if event.status == "cancelled":
        raise HTTPException(409, "Event is already cancelled")
    event.status = "cancelled"
    response = get_bus().send(AgentMessage(sender="EventService", receiver="BookingAgent", performative="REQUEST",
                                           content={"action": "refund_event_bookings", "event_id": event_id}), db)
    db.commit()
    return {"event_id": event_id, "status": "cancelled", "refund_summary": response.content if response else {}}


# -------------------------------- Bookings -----------------------------------
@app.post("/api/bookings", status_code=201)
def create_booking(payload: BookingRequest, db: Session = Depends(get_db), user: User = Depends(require_roles("attendee"))):
    response = get_bus().send(AgentMessage(sender="BookingAPI", receiver="BookingAgent", performative="REQUEST",
                                           content={"action": "create_booking", "user_id": user.id,
                                                    "ticket_category_id": payload.ticket_category_id,
                                                    "quantity": payload.quantity}), db)
    if response is None or response.performative != "AGREE":
        db.rollback()
        raise HTTPException(409, response.content.get("reason", "Booking was refused") if response else "Booking agent did not respond")
    db.commit()
    booking = db.get(Booking, int(response.content["booking_id"]))
    return booking_dict(booking, db)


@app.get("/api/bookings/mine")
def my_bookings(db: Session = Depends(get_db), user: User = Depends(require_roles("attendee"))):
    bookings = db.query(Booking).filter(Booking.user_id == user.id).order_by(Booking.created_at.desc()).all()
    return [booking_dict(booking, db) for booking in bookings]


@app.post("/api/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("attendee"))):
    response = get_bus().send(AgentMessage(sender="BookingAPI", receiver="BookingAgent", performative="REQUEST",
                                           content={"action": "cancel_booking", "booking_id": booking_id,
                                                    "user_id": user.id}), db)
    if response is None or response.performative != "AGREE":
        db.rollback()
        raise HTTPException(409, response.content.get("reason", "Cancellation refused") if response else "Booking agent did not respond")
    db.commit()
    return response.content


# ---------------------------- Digital ticketing -----------------------------
@app.get("/api/tickets/{ticket_id}/qr")
def ticket_qr(ticket_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "Ticket not found")
    booking = db.get(Booking, ticket.booking_id)
    if user.role not in {"admin", "staff"} and (booking is None or booking.user_id != user.id):
        raise HTTPException(403, "You cannot view this ticket")
    image = qrcode.make(ticket.code)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/staff/check-in")
def check_in(payload: CheckInRequest, db: Session = Depends(get_db), staff: User = Depends(require_roles("staff", "admin"))):
    response = get_bus().send(AgentMessage(sender="GateScanner", receiver="TicketAgent", performative="REQUEST",
                                           content={"action": "check_in", "code": payload.code,
                                                    "event_id": payload.event_id}), db)
    db.commit()
    if response is None:
        raise HTTPException(500, "Ticket agent did not respond")
    if response.performative != "AGREE":
        raise HTTPException(409, response.content.get("reason", "Ticket rejected"))
    return response.content


# ------------------------------- Venue hire ----------------------------------
@app.post("/api/venue-hire", status_code=201)
def create_hire_request(payload: VenueHireCreate, db: Session = Depends(get_db), user: User = Depends(require_roles("attendee"))):
    venue = db.get(Venue, payload.venue_id)
    if venue is None:
        raise HTTPException(404, "Venue not found")
    request = VenueHireRequest(user_id=user.id, venue_id=venue.id,
                               start_at=to_utc_naive(payload.start_at), end_at=to_utc_naive(payload.end_at),
                               purpose=payload.purpose, guest_count=payload.guest_count, status="pending")
    db.add(request)
    db.flush()
    response = get_bus().send(AgentMessage(sender="VenueHireAPI", receiver="VenueAgent", performative="REQUEST",
                                           content={"action": "evaluate_hire", "request_id": request.id}), db)
    db.commit()
    db.refresh(request)
    return {"request_id": request.id, "status": request.status, "quoted_price": request.quoted_price,
            "reason": request.decision_reason,
            "alternatives": json.loads(request.alternatives_json or "[]"),
            "agent_decision": response.performative.value if response else "NO_RESPONSE"}


@app.get("/api/venue-hire/mine")
def my_hire_requests(db: Session = Depends(get_db), user: User = Depends(require_roles("attendee"))):
    requests = db.query(VenueHireRequest).filter(VenueHireRequest.user_id == user.id).order_by(VenueHireRequest.created_at.desc()).all()
    return [{"id": r.id, "venue_id": r.venue_id, "start_at": r.start_at, "end_at": r.end_at,
             "purpose": r.purpose, "guest_count": r.guest_count, "quoted_price": r.quoted_price,
             "status": r.status, "reason": r.decision_reason,
             "alternatives": json.loads(r.alternatives_json or "[]")} for r in requests]


@app.get("/api/admin/venue-hire-requests")
def admin_hire_requests(status_filter: str | None = Query(default="escalated", alias="status"),
                        db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    query = db.query(VenueHireRequest)
    if status_filter:
        query = query.filter(VenueHireRequest.status == status_filter)
    return [{"id": r.id, "user_id": r.user_id, "venue_id": r.venue_id, "start_at": r.start_at,
             "end_at": r.end_at, "purpose": r.purpose, "guest_count": r.guest_count,
             "quoted_price": r.quoted_price, "status": r.status, "reason": r.decision_reason,
             "alternatives": json.loads(r.alternatives_json or "[]")} for r in query.order_by(VenueHireRequest.created_at).all()]


@app.post("/api/admin/venue-hire-requests/{request_id}/decision")
def decide_hire(request_id: int, payload: AdminHireDecision, db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    response = get_bus().send(AgentMessage(sender="AdminDashboard", receiver="VenueAgent", performative="REQUEST",
                                           content={"action": "admin_hire_decision", "request_id": request_id,
                                                    "decision": payload.decision, "reason": payload.reason}), db)
    if response is None or response.performative != "AGREE":
        db.rollback()
        raise HTTPException(409, response.content.get("reason", "Decision was refused") if response else "Venue agent did not respond")
    db.commit()
    return response.content


# -------------------------------- Waitlist -----------------------------------
@app.post("/api/waitlist", status_code=201)
def join_waitlist(payload: WaitlistJoin, db: Session = Depends(get_db), user: User = Depends(require_roles("attendee"))):
    response = get_bus().send(AgentMessage(sender="WaitlistAPI", receiver="WaitlistAgent", performative="REQUEST",
                                           content={"action": "join_waitlist", "user_id": user.id,
                                                    "ticket_category_id": payload.ticket_category_id,
                                                    "quantity": payload.quantity}), db)
    if response is None or response.performative != "AGREE":
        db.rollback()
        raise HTTPException(409, response.content.get("reason", "Waitlist request refused") if response else "Waitlist agent did not respond")
    db.commit()
    return response.content


@app.get("/api/waitlist/mine")
def my_waitlist(db: Session = Depends(get_db), user: User = Depends(require_roles("attendee"))):
    entries = db.query(WaitlistEntry).filter(WaitlistEntry.user_id == user.id).order_by(WaitlistEntry.created_at.desc()).all()
    return [{"id": e.id, "event_id": e.event_id, "ticket_category_id": e.ticket_category_id,
             "quantity": e.quantity, "status": e.status, "hold_expires_at": e.hold_expires_at} for e in entries]


@app.post("/api/waitlist/claim")
def claim_waitlist_offer(payload: WaitlistClaim, db: Session = Depends(get_db), user: User = Depends(require_roles("attendee"))):
    response = get_bus().send(AgentMessage(sender="WaitlistAPI", receiver="WaitlistAgent", performative="REQUEST",
                                           content={"action": "claim_offer", "user_id": user.id,
                                                    "entry_id": payload.entry_id}), db)
    if response is None or response.performative != "AGREE":
        db.rollback()
        raise HTTPException(409, response.content.get("reason", "Offer could not be claimed") if response else "Waitlist agent did not respond")
    db.commit()
    return response.content


# ------------------------------ Admin dashboard -----------------------------
@app.get("/api/admin/dashboard")
def admin_dashboard(db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    events = db.query(Event).all()
    stats = []
    for event in events:
        categories = db.query(TicketCategory).filter(TicketCategory.event_id == event.id).all()
        sold = sum(category.quantity_sold for category in categories)
        held = sum(category.quantity_held for category in categories)
        revenue = db.query(func.coalesce(func.sum(Payment.amount), 0)).join(Booking, Booking.id == Payment.booking_id).filter(
            Booking.event_id == event.id, Booking.status == "confirmed"
        ).scalar() or 0
        checkins = db.query(Ticket).filter(Ticket.event_id == event.id, Ticket.status == "used").count()
        stats.append({"event_id": event.id, "title": event.title, "status": event.status,
                      "tickets_sold": sold, "tickets_held": held, "capacity": sum(c.quantity for c in categories),
                      "simulated_revenue": round(float(revenue), 2), "check_ins": checkins})
    messages = db.query(AgentMessageLog).order_by(AgentMessageLog.created_at.desc()).limit(20).all()
    agent_states = db.query(AgentState).order_by(AgentState.key).all()
    return {"events": stats, "agent_states": [
        {"agent": state.key, "beliefs": json.loads(state.value_json), "updated_at": state.updated_at}
        for state in agent_states
    ], "recent_agent_messages": [
        {"conversation_id": m.conversation_id, "sender": m.sender, "receiver": m.receiver,
         "performative": m.performative, "content": json.loads(m.content_json), "created_at": m.created_at}
        for m in messages
    ]}


@app.get("/api/admin/agent-messages")
def agent_messages(conversation_id: str | None = None, limit: int = Query(100, ge=1, le=500),
                   db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    query = db.query(AgentMessageLog)
    if conversation_id:
        query = query.filter(AgentMessageLog.conversation_id == conversation_id)
    rows = query.order_by(AgentMessageLog.created_at.desc()).limit(limit).all()
    return [{"id": row.id, "conversation_id": row.conversation_id, "sender": row.sender,
             "receiver": row.receiver, "performative": row.performative,
             "content": json.loads(row.content_json), "created_at": row.created_at} for row in rows]


@app.get("/api/admin/agent-state")
def agent_states(db: Session = Depends(get_db), admin: User = Depends(require_roles("admin"))):
    rows = db.query(AgentState).order_by(AgentState.key).all()
    return [{"agent": row.key, "beliefs": json.loads(row.value_json), "updated_at": row.updated_at} for row in rows]


@app.get("/api/notifications")
def notifications(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Notification).filter(Notification.user_id == user.id).order_by(Notification.created_at.desc()).limit(100).all()
    return [{"id": row.id, "title": row.title, "message": row.message,
             "is_read": row.is_read, "created_at": row.created_at} for row in rows]


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Notification not found")
    row.is_read = True
    db.commit()
    return {"id": row.id, "is_read": True}


# Static frontend assets. The API and browser application share the same HTTPS origin.
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static"), name="static")
