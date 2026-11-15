import os
os.environ["DATABASE_URL"] = "sqlite:////tmp/eventsphere_pytest.db"
os.environ["SECRET_KEY"] = "pytest-secret-key-change-before-deployment"
os.environ["WAITLIST_SWEEP_SECONDS"] = "3600"

import pytest
from fastapi.testclient import TestClient
from app.database import Base, engine, SessionLocal
from app.main import app
from app.models import User
from app.security import hash_password, create_access_token


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def accounts():
    db = SessionLocal()
    admin = User(email="admin@test.example.com", full_name="Admin Test", role="admin", password_hash=hash_password("AdminPass!234"))
    staff = User(email="staff@test.example.com", full_name="Staff Test", role="staff", password_hash=hash_password("StaffPass!234"))
    attendee = User(email="attendee@test.example.com", full_name="Attendee Test", role="attendee", password_hash=hash_password("AttendeePass!234"))
    other = User(email="other@test.example.com", full_name="Other Test", role="attendee", password_hash=hash_password("OtherPass!234"))
    db.add_all([admin, staff, attendee, other])
    db.commit()
    for user in (admin, staff, attendee, other):
        db.refresh(user)
    result = {
        "admin": {"user": admin, "token": create_access_token(admin)},
        "staff": {"user": staff, "token": create_access_token(staff)},
        "attendee": {"user": attendee, "token": create_access_token(attendee)},
        "other": {"user": other, "token": create_access_token(other)},
    }
    db.close()
    return result


def auth(token):
    return {"Authorization": f"Bearer {token}"}
