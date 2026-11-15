# EventSphere — IT4123 Agent Based Computing Practical (ICA 02)

EventSphere is a mobile-friendly university event discovery, booking, digital ticketing and venue-hire web application. It is implemented as a FastAPI application with a vanilla HTML/CSS/JavaScript frontend served from the same origin. Five cooperating agents make booking, venue, ticket, waitlist and notification decisions through structured JSON messages.

## 1. Features implemented

### Part A — compulsory functional requirements

- **Accounts and roles:** attendee registration and sign-in; PBKDF2-SHA256 password hashing; `admin`, `staff` and `attendee` roles; protected admin and scanner API routes.
- **Venue management:** admin can create and edit venues with name, location, capacity, description, image URL, hourly hire rate and private-hire availability.
- **Event management:** admin can create draft or published events, edit event details, publish drafts and cancel published events. Ticket-category capacity is checked against venue capacity. `VenueAgent` checks clashes with a 30-minute turnaround buffer.
- **Browsing and booking:** responsive event cards; keyword search and category filters; ticket quantities per category; maximum six tickets per booking; atomic inventory updates to prevent overselling; simulated payment references; booking history and stated cancellation/refund rules.
- **Digital tickets:** unguessable random ticket codes, individual QR image endpoint, attendee ticket page, staff QR camera scanner with manual code fallback, one-time admission checks, and clear rejection reasons.
- **Private venue hire:** attendee submits venue, date/time, purpose and guest count. `VenueAgent` automatically approves routine requests, calculates the quote, or proposes available alternative times/venues and escalates requests for admin action.
- **Organiser dashboard:** event sales, simulated revenue, check-ins and ticket capacity, plus a readable agent-message trace and persistent agent belief summaries.

### Part B — compulsory multi-agent design

- Five agents: `VenueAgent`, `BookingAgent`, `TicketAgent`, `WaitlistAgent` and `NotificationAgent`.
- Agents exchange Pydantic-validated JSON messages through `MessageBus`; supported performatives are `REQUEST`, `AGREE`, `REFUSE`, `PROPOSE`, `INFORM` and `FAILURE`.
- Messages include sender, receiver, performative, content and conversation ID. Message logs are persisted and shown to admins.
- Each agent records persistent state (message count, last action, sender, performative, conversation ID and timestamp). Business state such as bookings, tickets, venue requests, waitlist entries and hold expiry is stored in the relational database.
- `NotificationAgent` reacts to `INFORM`, `PROPOSE` and `REFUSE` messages. `WaitlistAgent` performs a scheduled expiry sweep without requiring user input.
- Organiser cancellation closes any active waitlist entries and releases timed holds; event bookings receive full refunds.

### Optional advanced behaviour included

- FIFO waitlist with limited-duration seat holds, live countdown in the attendee interface, periodic expiry and automatic re-offer.
- Admin dashboard exposes agent state and message history.

No real payment gateway or card details are used. The QR camera library is loaded from a pinned public CDN; manual code entry remains available if the library or camera is unavailable.

## 2. Technology stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI |
| Validation | Pydantic 2 |
| ORM/database | SQLAlchemy 2; SQLite locally; PostgreSQL in production |
| Agent messaging | Pydantic JSON messages and in-process message bus |
| Frontend | HTML5, CSS3, vanilla JavaScript; responsive at 375 px |
| QR generation | `qrcode` and Pillow |
| QR scanning | `html5-qrcode` browser library; manual entry fallback |
| Authentication | PBKDF2-SHA256 password hashing; signed bearer tokens |
| Testing | Pytest and FastAPI TestClient/HTTPX |
| Deployment | Render blueprint (`render.yaml`) or equivalent HTTPS host |
| Version control | Git and GitHub |

## 3. Run locally on Windows

Open PowerShell in the extracted `eventsphere` folder.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
python -m app.seed_demo
uvicorn app.main:app --reload
```

Open:

- Web application: <http://127.0.0.1:8000>
- Interactive API docs: <http://127.0.0.1:8000/docs>
- Health endpoint: <http://127.0.0.1:8000/health>

Linux/macOS:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.seed_demo
uvicorn app.main:app --reload
```

The database tables are created automatically on application startup. The seed script can be re-run safely; it synchronises existing demo-account emails, roles, passwords and active status, and creates sample venue/event data only when missing.

## 4. Local demo accounts

| Role | Email | Password |
|---|---|---|
| Admin / organiser | `admin@eventsphere.example.com` | `AdminDemo!234` |
| Gate staff | `staff@eventsphere.example.com` | `StaffDemo!234` |
| Attendee | `attendee@eventsphere.example.com` | `AttendeeDemo!234` |

These are **local demonstration credentials only**. Change them before deployment. The demo emails use the reserved `example.com` domain so they pass the application’s email validation. The seed script supports `DEMO_ADMIN_EMAIL`, `DEMO_ADMIN_PASSWORD`, `DEMO_STAFF_EMAIL`, `DEMO_STAFF_PASSWORD`, `DEMO_ATTENDEE_EMAIL` and `DEMO_ATTENDEE_PASSWORD` environment variables. Re-running `python -m app.seed_demo` updates existing demo passwords/roles and migrates the previous invalid `@eventsphere.local` demo emails.

## 5. Run the tests

```powershell
pytest -q
```

The current suite contains **22 tests** and uses a separate SQLite test database at `/tmp/eventsphere_pytest.db`. It covers frontend/static delivery, role restrictions, venue/event management, venue clashes, CSRF, booking/payment/ticket issuance, overbooking refusal, check-in and duplicate scans, wrong-event scans, waitlist claim/expiry/event cancellation, venue hire decisions, notification delivery and agent traceability.

## 6. Agent goals and documented decision rules

| Agent | Goal | Main decision rules | Main message peers |
|---|---|---|---|
| `VenueAgent` | Keep venue calendars conflict-free and allocate private hire | Reject nonexistent venues; reject event overlap with a 30-minute buffer; private hire requires venue open, sufficient capacity and a free slot; automatically approve routine requests; calculate hourly price; propose usable alternatives or escalate | `NotificationAgent` and the requesting service via `MessageBus` |
| `BookingAgent` | Sell seats fairly without overselling | Reject quantities outside 1–6; event must be published and not started; allocate only when `sold + held + requested <= total`; create booking/payment reference; refund 100% at 7+ days, 50% at 2–7 days, 0% under 48 hours; full refund on organiser cancellation | `TicketAgent`, `WaitlistAgent`, `NotificationAgent` |
| `TicketAgent` | Issue and admit each ticket once | Generate `secrets.token_urlsafe(32)` codes; reject unknown, cancelled, used, wrong-event or out-of-window tickets; atomically change `valid` to `used` | `BookingAgent` and `NotificationAgent` |
| `WaitlistAgent` | Offer freed seats fairly | FIFO order; do not skip an earlier larger request; reserve seats as `quantity_held`; create a hold until `WAITLIST_HOLD_MINUTES`; expire holds on a timer and offer seats to the next eligible attendee; close all event waitlists on cancellation | `BookingAgent` and `NotificationAgent` |
| `NotificationAgent` | Notify people affected by agent decisions | React to `INFORM`, `PROPOSE` and `REFUSE`; persist notification for the specified user | Listener for agent messages |

### Message example

```json
{
  "sender": "BookingAPI",
  "receiver": "BookingAgent",
  "performative": "REQUEST",
  "content": {
    "action": "create_booking",
    "user_id": 4,
    "ticket_category_id": 12,
    "quantity": 2
  },
  "conversation_id": "generated-uuid"
}
```

Each dispatched message and each returned response is written to `agent_message_logs` with the same conversation ID. Admins can inspect these records from **Admin → Agent activity** or `/api/admin/agent-messages`.

## 7. Concurrency and inventory safety

Ticket inventory is protected by a conditional SQL `UPDATE` executed at the database layer. A booking request increments `quantity_sold` only where:

```sql
quantity_sold + quantity_held + :requested_quantity <= quantity
```

The update is the allocation decision, not a read-then-write check in Python. Concurrent requests therefore compete for the same row update; once the remaining inventory is exhausted, a later update affects zero rows and the agent refuses the request. Waitlist holds use a similar conditional update to move capacity into `quantity_held`; claiming a hold atomically transfers held seats to sold seats. PostgreSQL is used for production deployment.

## 8. Refund, venue and check-in policies

- Attendee cancellation at least seven days before event: 100% refund.
- Attendee cancellation from two to fewer than seven days before event: 50% refund.
- Attendee cancellation less than 48 hours before event: no refund.
- Organiser event cancellation: full refund for confirmed bookings; ticket codes are cancelled and outstanding waitlist holds are released.
- Event/venue-hire clash checking uses a 30-minute turnaround buffer before and after activities.
- Gate check-in opens `CHECKIN_EARLY_MINUTES` before the event and closes `CHECKIN_LATE_MINUTES` after the event ends (defaults: 180 and 120 minutes).
- Waitlist hold duration defaults to 10 minutes; expiry sweep interval defaults to 30 seconds.

## 9. API route overview

| Function | Endpoint |
|---|---|
| Register / login / session | `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me` |
| CSRF token | `GET /api/auth/csrf` |
| Public venues/events | `GET /api/venues`, `GET /api/events`, `GET /api/events/{id}` |
| Admin venues | `POST /api/admin/venues`, `PATCH /api/admin/venues/{id}` |
| Admin events | `GET /api/admin/events`, `POST /api/admin/events`, `PATCH /api/admin/events/{id}`, `POST /api/admin/events/{id}/cancel` |
| Booking | `POST /api/bookings`, `GET /api/bookings/mine`, `POST /api/bookings/{id}/cancel` |
| Tickets/check-in | `GET /api/tickets/{id}/qr`, `POST /api/staff/check-in` |
| Venue hire | `POST /api/venue-hire`, `GET /api/venue-hire/mine`, `GET /api/admin/venue-hire-requests`, `POST /api/admin/venue-hire-requests/{id}/decision` |
| Waitlist | `POST /api/waitlist`, `GET /api/waitlist/mine`, `POST /api/waitlist/claim` |
| Dashboard/agents | `GET /api/admin/dashboard`, `GET /api/admin/agent-messages`, `GET /api/admin/agent-state` |
| Notifications | `GET /api/notifications`, `POST /api/notifications/{id}/read` |

## 10. Security and configuration

- Passwords are salted and hashed with PBKDF2-HMAC-SHA256; plain-text passwords are not stored.
- API routes enforce the relevant role on the backend; hiding a frontend tab is not treated as security.
- The browser client uses bearer tokens and a double-submit CSRF cookie/header for state-changing requests.
- Production must use HTTPS, a long random `SECRET_KEY`, secure CSRF cookies, and environment variables. Never commit `.env` or credentials.
- Payments are simulated only. The application never requests bank/card information.
- For local development, keep `CSRF_COOKIE_SECURE=false`. For HTTPS production, set `CSRF_COOKIE_SECURE=true`.

Important environment variables:

| Variable | Local default / purpose |
|---|---|
| `DATABASE_URL` | `sqlite:///./eventsphere.db`; set PostgreSQL URL in production |
| `SECRET_KEY` | Development placeholder; replace with long random secret |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `120` |
| `WAITLIST_HOLD_MINUTES` | `10` |
| `WAITLIST_SWEEP_SECONDS` | `30` |
| `CHECKIN_EARLY_MINUTES` | `180` |
| `CHECKIN_LATE_MINUTES` | `120` |
| `CORS_ORIGINS` | Comma-separated allowed origins |
| `CSRF_COOKIE_SECURE` | `false` locally; `true` for HTTPS |

## 11. Deploy to Render

A starter Render blueprint is included in `render.yaml`. In your own Render account:

1. Push this project to a GitHub repository.
2. In Render, create a Blueprint deployment from that repository and provision the PostgreSQL database defined in `render.yaml`.
3. Confirm the web service receives `DATABASE_URL`; the app normalises the common `postgres://` and `postgresql://` schemes to the installed psycopg 3 driver.
4. Set a unique long `SECRET_KEY`, `CSRF_COOKIE_SECURE=true`, and the correct public origin in `CORS_ORIGINS`.
5. Deploy with build command `pip install -r requirements.txt` and start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
6. From the Render Shell, run `python -m app.seed_demo` once using secure demo-account environment variables. Replace the sample demo passwords.
7. Open the public HTTPS URL, test all roles and workflows, and keep the deployed service available for the viva.

Render service availability, plan limits and free-tier behaviour may change. Check the current Render dashboard when creating the database and web service. If you use another host, provide equivalent environment variables, PostgreSQL and HTTPS.

## 12. License

This project is distributed under the **MIT License**. See [`LICENSE`](LICENSE) for the full terms. Replace the copyright-holder placeholder in that file before publishing the repository.

The MIT License applies to the original project code and documentation that you are entitled to license. Third-party libraries, browser scripts, fonts, images and other external assets remain subject to their own licenses and terms. Do not include university-owned materials or third-party assets in this license unless you have permission to do so.