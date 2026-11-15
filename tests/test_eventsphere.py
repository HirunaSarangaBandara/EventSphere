from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import User
from app.security import hash_password
from app.seed_demo import ensure_user
from app.models import AgentMessageLog, Booking, Event, Ticket, TicketCategory, Venue, WaitlistEntry
from app.agents import create_message_bus
from app.agents.messages import AgentMessage
from .conftest import auth


def create_venue(client, admin_token, capacity=100):
    response = client.post("/api/admin/venues", headers=auth(admin_token), json={
        "name": "Test Hall", "location": "Campus", "capacity": capacity,
        "description": "Test venue", "hourly_hire_rate": 1000, "open_for_hire": True,
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_event(client, admin_token, venue_id, quantity=20, days=10, title="Test Workshop"):
    start = datetime.now(timezone.utc) + timedelta(days=days)
    response = client.post("/api/admin/events", headers=auth(admin_token), json={
        "title": title, "category": "Workshop", "venue_id": venue_id,
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=2)).isoformat(),
        "description": "Test event", "status": "published",
        "ticket_categories": [{"name": "Standard", "price": 1000, "quantity": quantity}],
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_attendee_can_register_and_login(client):
    registered = client.post("/api/auth/register", json={
        "email": "new@example.com", "full_name": "New Attendee", "password": "SecurePass!123"
    })
    assert registered.status_code == 201
    assert registered.json()["user"]["role"] == "attendee"
    assert "password_hash" not in registered.json()["user"]
    login = client.post("/api/auth/login", json={"email": "new@example.com", "password": "SecurePass!123"})
    assert login.status_code == 200
    assert login.json()["token_type"] == "bearer"


def test_demo_seed_repairs_existing_admin_and_staff_credentials(client):
    db = SessionLocal()
    try:
        demo_users = [
            ("admin", "admin@eventsphere.example.com", "Old Admin", "OldPass!123", "AdminDemo!234", "admin@eventsphere.local"),
            ("staff", "staff@eventsphere.example.com", "Old Staff", "OldPass!456", "StaffDemo!234", "staff@eventsphere.local"),
        ]
        for role, email, old_name, old_password, new_password, legacy_email in demo_users:
            existing = User(
                email=legacy_email,
                full_name=old_name,
                role="attendee",  # Ensure the seed also repairs an incorrect role.
                password_hash=hash_password(old_password),
                is_active=False,
            )
            db.add(existing)
            db.commit()

            repaired = ensure_user(
                db, email.upper(), f"EventSphere {role.title()}", role, new_password,
                legacy_email=legacy_email,
            )
            db.commit()
            assert repaired.role == role
            assert repaired.is_active is True
            assert repaired.full_name == f"EventSphere {role.title()}"

            response = client.post("/api/auth/login", json={"email": email, "password": new_password})
            assert response.status_code == 200, response.text
            assert response.json()["user"]["role"] == role

            # Re-running the seed with the same credentials remains safe.
            ensure_user(db, email, f"EventSphere {role.title()}", role, new_password)
            db.commit()
    finally:
        db.close()


def test_attendee_is_blocked_from_admin_routes(client, accounts):
    response = client.post("/api/admin/venues", headers=auth(accounts["attendee"]["token"]), json={
        "name": "Forbidden Hall", "location": "Campus", "capacity": 10, "hourly_hire_rate": 100,
    })
    assert response.status_code == 403


def test_admin_can_create_and_update_venue(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    updated = client.patch(f"/api/admin/venues/{venue_id}", headers=auth(accounts["admin"]["token"]), json={"capacity": 150})
    assert updated.status_code == 200
    assert updated.json()["capacity"] == 150


def test_event_ticket_quantity_cannot_exceed_venue_capacity(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"], capacity=10)
    start = datetime.now(timezone.utc) + timedelta(days=4)
    response = client.post("/api/admin/events", headers=auth(accounts["admin"]["token"]), json={
        "title": "Too Many Tickets", "category": "Seminar", "venue_id": venue_id,
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(),
        "status": "published", "ticket_categories": [{"name": "General", "price": 100, "quantity": 11}],
    })
    assert response.status_code == 422


def test_venue_agent_rejects_overlapping_events(client, accounts):
    token = accounts["admin"]["token"]
    venue_id = create_venue(client, token)
    create_event(client, token, venue_id, title="First Event")
    start = datetime.now(timezone.utc) + timedelta(days=10, hours=1)
    response = client.post("/api/admin/events", headers=auth(token), json={
        "title": "Overlapping Event", "category": "Concert", "venue_id": venue_id,
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(),
        "status": "published", "ticket_categories": [{"name": "General", "price": 500, "quantity": 10}],
    })
    assert response.status_code == 409
    assert "conflict" in response.json()["detail"].lower()


def test_csrf_double_submit_token_is_required_when_cookie_is_set(client, accounts):
    token_response = client.get("/api/auth/csrf")
    assert token_response.status_code == 200
    csrf_token = token_response.json()["csrf_token"]
    blocked = client.post("/api/admin/venues", headers=auth(accounts["admin"]["token"]), json={
        "name": "CSRF Hall", "location": "Campus", "capacity": 10, "hourly_hire_rate": 100,
    })
    assert blocked.status_code == 403
    allowed_headers = auth(accounts["admin"]["token"])
    allowed_headers["X-CSRF-Token"] = csrf_token
    allowed = client.post("/api/admin/venues", headers=allowed_headers, json={
        "name": "CSRF Hall", "location": "Campus", "capacity": 10, "hourly_hire_rate": 100,
    })
    assert allowed.status_code == 201


def test_booking_creates_simulated_payment_and_unique_ticket_codes(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    event = create_event(client, accounts["admin"]["token"], venue_id)
    category_id = event["ticket_categories"][0]["id"]
    response = client.post("/api/bookings", headers=auth(accounts["attendee"]["token"]), json={
        "ticket_category_id": category_id, "quantity": 2,
    })
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "confirmed"
    assert data["payment_reference"].startswith("SIM-")
    assert len(data["tickets"]) == 2
    assert len({ticket["code"] for ticket in data["tickets"]}) == 2


def test_booking_agent_refuses_overbooking(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"], capacity=1)
    event = create_event(client, accounts["admin"]["token"], venue_id, quantity=1)
    category_id = event["ticket_categories"][0]["id"]
    first = client.post("/api/bookings", headers=auth(accounts["attendee"]["token"]), json={"ticket_category_id": category_id, "quantity": 1})
    second = client.post("/api/bookings", headers=auth(accounts["other"]["token"]), json={"ticket_category_id": category_id, "quantity": 1})
    assert first.status_code == 201
    assert second.status_code == 409
    db = SessionLocal()
    category = db.get(TicketCategory, category_id)
    assert category.quantity_sold == 1
    assert category.quantity_sold + category.quantity_held <= category.quantity
    db.close()


def test_ticket_agent_admits_once_and_rejects_second_scan(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    event = create_event(client, accounts["admin"]["token"], venue_id, days=0.05)
    category_id = event["ticket_categories"][0]["id"]
    booked = client.post("/api/bookings", headers=auth(accounts["attendee"]["token"]), json={"ticket_category_id": category_id, "quantity": 1}).json()
    code = booked["tickets"][0]["code"]
    scan = {"code": code, "event_id": event["id"]}
    first = client.post("/api/staff/check-in", headers=auth(accounts["staff"]["token"]), json=scan)
    second = client.post("/api/staff/check-in", headers=auth(accounts["staff"]["token"]), json=scan)
    assert first.status_code == 200
    assert first.json()["accepted"] is True
    assert second.status_code == 409
    assert "already" in second.json()["detail"].lower()


def test_ticket_agent_rejects_ticket_for_wrong_event(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    event1 = create_event(client, accounts["admin"]["token"], venue_id, title="Event One")
    # A second event at a later time in the same venue.
    start = datetime.now(timezone.utc) + timedelta(days=20)
    event2_response = client.post("/api/admin/events", headers=auth(accounts["admin"]["token"]), json={
        "title": "Event Two", "category": "Seminar", "venue_id": venue_id,
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(),
        "status": "published", "ticket_categories": [{"name": "General", "price": 100, "quantity": 10}],
    })
    assert event2_response.status_code == 201
    event2 = event2_response.json()
    booked = client.post("/api/bookings", headers=auth(accounts["attendee"]["token"]), json={
        "ticket_category_id": event1["ticket_categories"][0]["id"], "quantity": 1,
    }).json()
    response = client.post("/api/staff/check-in", headers=auth(accounts["staff"]["token"]), json={
        "code": booked["tickets"][0]["code"], "event_id": event2["id"],
    })
    assert response.status_code == 409
    assert "different event" in response.json()["detail"].lower()


def test_waitlist_agent_offers_freed_seat_and_booking_can_be_claimed(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"], capacity=1)
    event = create_event(client, accounts["admin"]["token"], venue_id, quantity=1, days=12)
    category_id = event["ticket_categories"][0]["id"]
    booking = client.post("/api/bookings", headers=auth(accounts["attendee"]["token"]), json={
        "ticket_category_id": category_id, "quantity": 1,
    }).json()
    joined = client.post("/api/waitlist", headers=auth(accounts["other"]["token"]), json={
        "ticket_category_id": category_id, "quantity": 1,
    })
    assert joined.status_code == 201
    entry_id = joined.json()["entry_id"]
    cancelled = client.post(f"/api/bookings/{booking['id']}/cancel", headers=auth(accounts["attendee"]["token"]))
    assert cancelled.status_code == 200
    db = SessionLocal()
    entry = db.get(WaitlistEntry, entry_id)
    assert entry.status == "offered"
    db.close()
    claimed = client.post("/api/waitlist/claim", headers=auth(accounts["other"]["token"]), json={"entry_id": entry_id})
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["booking"]["issued_tickets"] == 1
    db = SessionLocal()
    category = db.get(TicketCategory, category_id)
    assert category.quantity_sold == 1
    assert category.quantity_held == 0
    db.close()


def test_venue_hire_agent_automatically_approves_available_request(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    start = datetime.now(timezone.utc) + timedelta(days=30)
    response = client.post("/api/venue-hire", headers=auth(accounts["attendee"]["token"]), json={
        "venue_id": venue_id, "start_at": start.isoformat(),
        "end_at": (start + timedelta(hours=3)).isoformat(),
        "purpose": "Club farewell", "guest_count": 50,
    })
    assert response.status_code == 201
    assert response.json()["status"] == "approved"
    assert response.json()["quoted_price"] == 3000


def test_notification_agent_records_booking_notification(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    event = create_event(client, accounts["admin"]["token"], venue_id)
    client.post("/api/bookings", headers=auth(accounts["attendee"]["token"]), json={
        "ticket_category_id": event["ticket_categories"][0]["id"], "quantity": 1,
    })
    notifications = client.get("/api/notifications", headers=auth(accounts["attendee"]["token"]))
    assert notifications.status_code == 200
    assert any(row["title"] == "Booking confirmed" for row in notifications.json())


def test_agent_messages_are_logged_and_traceable(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    event = create_event(client, accounts["admin"]["token"], venue_id)
    client.post("/api/bookings", headers=auth(accounts["attendee"]["token"]), json={
        "ticket_category_id": event["ticket_categories"][0]["id"], "quantity": 1,
    })
    dashboard = client.get("/api/admin/dashboard", headers=auth(accounts["admin"]["token"]))
    assert dashboard.status_code == 200
    assert dashboard.json()["recent_agent_messages"]
    state_names = {row["agent"] for row in dashboard.json()["agent_states"]}
    assert {"BookingAgent", "TicketAgent", "NotificationAgent"}.issubset(state_names)
    assert any(row["sender"] == "BookingAgent" or row["receiver"] == "BookingAgent"
               for row in dashboard.json()["recent_agent_messages"])


def test_waitlist_expiry_sweep_is_proactive_agent_action(accounts):
    db = SessionLocal()
    user = accounts["attendee"]["user"]
    venue = Venue(name="Sweep Hall", location="Campus", capacity=5, hourly_hire_rate=100, open_for_hire=True)
    db.add(venue)
    db.flush()
    event = Event(title="Sweep Event", category="Workshop", venue_id=venue.id,
                  start_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=5), end_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=5, hours=1),
                  description="", status="published", created_by=accounts["admin"]["user"].id)
    db.add(event)
    db.flush()
    category = TicketCategory(event_id=event.id, name="General", price=10, quantity=1, quantity_sold=0, quantity_held=1)
    db.add(category)
    db.flush()
    entry = WaitlistEntry(user_id=user.id, event_id=event.id, ticket_category_id=category.id,
                          quantity=1, status="offered", hold_expires_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1))
    db.add(entry)
    db.commit()
    bus = create_message_bus()
    response = bus.send(AgentMessage(sender="Scheduler", receiver="WaitlistAgent", performative="REQUEST",
                                     content={"action": "sweep_expired"}), db)
    assert response.performative.value == "INFORM"
    db.refresh(entry)
    db.refresh(category)
    assert entry.status == "expired"
    assert category.quantity_held == 0
    db.close()


def test_homepage_serves_mobile_frontend_and_static_assets(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "Explore events" in page.text
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "BarcodeDetector" in script.text


def test_admin_can_list_drafts_but_attendees_cannot(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    start = datetime.now(timezone.utc) + timedelta(days=20)
    created = client.post("/api/admin/events", headers=auth(accounts["admin"]["token"]), json={
        "title": "Draft Symposium", "category": "Seminar", "venue_id": venue_id,
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=1)).isoformat(),
        "description": "Draft only", "status": "draft",
        "ticket_categories": [{"name": "General", "price": 0, "quantity": 20}],
    })
    assert created.status_code == 201, created.text
    admin_rows = client.get("/api/admin/events", headers=auth(accounts["admin"]["token"]))
    assert admin_rows.status_code == 200
    assert any(row["title"] == "Draft Symposium" for row in admin_rows.json())
    attendee_rows = client.get("/api/admin/events", headers=auth(accounts["attendee"]["token"]))
    assert attendee_rows.status_code == 403
    public_rows = client.get("/api/events")
    assert all(row["title"] != "Draft Symposium" for row in public_rows.json())


def test_admin_can_publish_a_draft_event(client, accounts):
    venue_id = create_venue(client, accounts["admin"]["token"])
    start = datetime.now(timezone.utc) + timedelta(days=25)
    created = client.post("/api/admin/events", headers=auth(accounts["admin"]["token"]), json={
        "title": "Publish Me", "category": "Workshop", "venue_id": venue_id,
        "start_at": start.isoformat(), "end_at": (start + timedelta(hours=2)).isoformat(),
        "status": "draft", "ticket_categories": [{"name": "Standard", "price": 100, "quantity": 10}],
    }).json()
    published = client.patch(f"/api/admin/events/{created['id']}", headers=auth(accounts["admin"]["token"]), json={"status": "published"})
    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert any(row["id"] == created["id"] for row in client.get("/api/events").json())


def test_attendee_cannot_use_staff_scanner(client, accounts):
    response = client.post("/api/staff/check-in", headers=auth(accounts["attendee"]["token"]), json={"code": "random-code-123", "event_id": 1})
    assert response.status_code == 403


def test_venue_agent_escalates_conflicting_hire_with_alternatives(client, accounts):
    token = accounts["admin"]["token"]
    venue_id = create_venue(client, token)
    event = create_event(client, token, venue_id, days=40, title="Venue blocking event")
    start = datetime.fromisoformat(event["start_at"].replace("Z", "+00:00"))
    request = client.post("/api/venue-hire", headers=auth(accounts["attendee"]["token"]), json={
        "venue_id": venue_id, "start_at": start.isoformat(),
        "end_at": (start + timedelta(hours=1)).isoformat(),
        "purpose": "Club reception", "guest_count": 25,
    })
    assert request.status_code == 201
    assert request.json()["status"] == "escalated"
    assert request.json()["agent_decision"] == "PROPOSE"
    assert request.json()["alternatives"]


def test_event_cancellation_closes_waitlist_and_releases_active_hold(client, accounts):
    token = accounts["admin"]["token"]
    venue_id = create_venue(client, token, capacity=1)
    event = create_event(client, token, venue_id, quantity=1, days=18)
    category_id = event["ticket_categories"][0]["id"]
    booking = client.post("/api/bookings", headers=auth(accounts["attendee"]["token"]), json={
        "ticket_category_id": category_id, "quantity": 1,
    }).json()
    waiting = client.post("/api/waitlist", headers=auth(accounts["other"]["token"]), json={
        "ticket_category_id": category_id, "quantity": 1,
    }).json()
    client.post(f"/api/bookings/{booking['id']}/cancel", headers=auth(accounts["attendee"]["token"]))
    cancelled = client.post(f"/api/admin/events/{event['id']}/cancel", headers=auth(token))
    assert cancelled.status_code == 200
    db = SessionLocal()
    entry = db.get(WaitlistEntry, waiting["entry_id"])
    category = db.get(TicketCategory, category_id)
    assert entry.status == "cancelled"
    assert category.quantity_held == 0
    db.close()
