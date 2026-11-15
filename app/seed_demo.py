"""Create local demo accounts. Run with: python -m app.seed_demo"""
import os
from .database import SessionLocal, init_db
from .models import User, Venue, Event, TicketCategory
from .security import hash_password, verify_password
from .main import to_utc_naive
from datetime import datetime, timedelta, timezone


def ensure_user(db, email: str, name: str, role: str, password: str, legacy_email: str | None = None):
    """Create a demo account or repair its configured credentials and role.

    The previous implementation left an existing account unchanged. As a result,
    re-running the seed command after a password was changed (or after the first
    seed used different DEMO_* variables) still printed the default credentials
    even though those credentials were no longer valid.
    """
    normalized_email = email.strip().lower()
    user = db.query(User).filter(User.email == normalized_email).first()
    # Migrate the old invalid .local demo addresses to EmailStr-compatible
    # example.com addresses without losing any user-owned records.
    if user is None and legacy_email:
        user = db.query(User).filter(User.email == legacy_email.strip().lower()).first()
    if user is None:
        user = User(
            email=normalized_email,
            full_name=name,
            role=role,
            password_hash=hash_password(password),
            is_active=True,
        )
        db.add(user)
        db.flush()
        return user

    # Keep the demo identity usable after a prior seed with stale credentials.
    user.email = normalized_email
    user.full_name = name
    user.role = role
    user.is_active = True
    if not verify_password(password, user.password_hash):
        user.password_hash = hash_password(password)
    db.flush()
    return user


def main():
    init_db()
    db = SessionLocal()
    try:
        admin = ensure_user(
            db, os.getenv("DEMO_ADMIN_EMAIL", "admin@eventsphere.example.com"),
            "EventSphere Admin", "admin", os.getenv("DEMO_ADMIN_PASSWORD", "AdminDemo!234"),
            legacy_email="admin@eventsphere.local",
        )
        ensure_user(
            db, os.getenv("DEMO_STAFF_EMAIL", "staff@eventsphere.example.com"),
            "Gate Staff", "staff", os.getenv("DEMO_STAFF_PASSWORD", "StaffDemo!234"),
            legacy_email="staff@eventsphere.local",
        )
        attendee = ensure_user(
            db, os.getenv("DEMO_ATTENDEE_EMAIL", "attendee@eventsphere.example.com"),
            "Demo Attendee", "attendee", os.getenv("DEMO_ATTENDEE_PASSWORD", "AttendeeDemo!234"),
            legacy_email="attendee@eventsphere.local",
        )
        venue = db.query(Venue).filter(Venue.name == "Main University Hall").first()
        if venue is None:
            venue = Venue(name="Main University Hall", location="University Campus", capacity=500,
                          description="Multi-purpose university event hall", hourly_hire_rate=5000,
                          open_for_hire=True)
            db.add(venue)
            db.flush()
        if not db.query(Event).filter(Event.title == "EventSphere Demo Workshop").first():
            start = datetime.now(timezone.utc) + timedelta(days=14)
            event = Event(title="EventSphere Demo Workshop", category="Workshop", venue_id=venue.id,
                          start_at=to_utc_naive(start), end_at=to_utc_naive(start + timedelta(hours=2)),
                          description="A sample event for demonstrating EventSphere booking and ticketing.",
                          status="published", created_by=admin.id)
            db.add(event)
            db.flush()
            db.add(TicketCategory(event_id=event.id, name="Standard", price=500, quantity=100))
        db.commit()
        print("Demo data is ready. The configured demo accounts are active:")
        print(f"Admin email: {os.getenv('DEMO_ADMIN_EMAIL', 'admin@eventsphere.example.com')}")
        print(f"Staff email: {os.getenv('DEMO_STAFF_EMAIL', 'staff@eventsphere.example.com')}")
        print(f"Attendee email: {os.getenv('DEMO_ATTENDEE_EMAIL', 'attendee@eventsphere.example.com')}")
        print("The passwords are the DEMO_*_PASSWORD values from the environment.")
        print("When no overrides are configured, the README's default demo passwords are used.")
        print("Re-running this command updates existing demo-account passwords and roles to match these values.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
