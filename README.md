# Academy Fee Manager

An automated fee tracking and reminder system for small tuition academies.

Small academies usually track fees on paper or in Excel and chase parents by hand every month. This project replaces that routine: the owner records students once, and the system generates monthly fees, sends WhatsApp reminders to parents who haven't paid, and sends a receipt when a payment is recorded.

> **Status:** working portfolio project. WhatsApp runs in Meta's test mode and payments are recorded manually. See [Limitations](#limitations).

<!-- Add screenshots here: dashboard, monthly report, and a WhatsApp reminder (blur phone numbers) -->

---

## What it does

| When | What happens |
| 1st of the month, 9 AM | Fee records are generated for every student |
| 1st, 10th and 20th, 10 AM | Parents with unpaid fees get a WhatsApp reminder |
| Owner clicks "Mark paid" | The fee is recorded and the parent gets a receipt |
| Any time | The owner views a monthly report, overall and per student |

**Behaviour worth noting**

- **Siblings get one message.** Students are grouped by parent phone number, so a parent with three children receives one reminder listing all three and a combined total.
- **Overdue fees carry over.** An unpaid month keeps appearing in reminders and reports, oldest first, until it is settled.
- **Paid families stop receiving reminders.** Only unpaid fees are fetched.

---

## Architecture

```
                         +---------------------------+
   Owner's browser  ---> |  FastAPI                  | <---> SQLite (academy.db)
   (dashboard)           |  data + business rules    |
                         +-------------+-------------+
                           ^           |
         "who owes money?" |           | "a fee was paid"
          (API key)        |           v  (webhook)
                         +-------------+-------------+
                         |  n8n                      |
                         |  schedules + messaging    |
                         +-------------+-------------+
                                       |
                                       v
                         WhatsApp Cloud API (Meta) ---> parent's phone
```

**The core idea: FastAPI knows things, n8n does things at the right time.**
FastAPI owns the data and the rules. It never decides when to send anything. n8n owns the clock and the messaging. It holds no data of its own and always asks the API.

### The three n8n workflows

Each workflow has exactly one trigger, so each does one job:

| Workflow | Trigger | Job |
|---|---|---|
| **Generate monthly fees** | Schedule: 1st, 9 AM | `POST /fees/generate/{month}` |
| **Reminders** | Schedule: 1st, 10th, 20th at 10 AM | `GET /families/unpaid`, build one message per family, send via WhatsApp |
| **Receipts** | Webhook: called by FastAPI on payment | Build a receipt message and send via WhatsApp |

Exports are in [`n8n-workflows/`](n8n-workflows/).

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI | Automatic request validation and interactive docs at `/docs` |
| Database | SQLite via SQLAlchemy | Single file, no setup. Swapping to PostgreSQL needs little code change |
| Validation | Pydantic | Rejects bad input before any route code runs |
| Automation | n8n | Visual workflows, built-in scheduling, run history per execution |
| Messaging | WhatsApp Cloud API | The channel parents actually read |
| Auth | bcrypt, signed session cookies, API key | See [Authentication](#authentication-one-api-two-kinds-of-caller) |
| Frontend | Plain HTML and JavaScript | Served by FastAPI itself, so no build step and no CORS setup |

---

## Data model

Three ideas, two tables:

- **`students`**: name, class, parent phone, monthly fee.
- **`fee_records`**: one row per student per month, with `amount_due`, `status` (`unpaid` or `paid`) and `paid_on`.
- A **unique constraint** on `(student_id, month)` means a student can never be billed twice for the same month, even if the code has a bug.
- `amount_due` is **copied** from the student's fee when the record is created, so raising a fee later does not rewrite history.
- Months are stored as text like `2026-10`. In that format, text order equals date order, so "this month and earlier" is a simple `month <= '2026-10'` comparison.

---

## API overview

| Method | Route | Purpose |
|---|---|---|
| POST | `/students` | Add a student |
| GET | `/students` | List students |
| POST | `/fees/generate/{month}` | Create fee records for a month (safe to repeat) |
| GET | `/fees?month=` | All fees for a month |
| POST | `/fees/{fee_id}/pay` | Mark one fee paid, notify n8n |
| GET | `/families/unpaid?month=` | Unpaid fees up to a month, grouped by family |
| POST | `/families/pay` | Settle several fees at once (all or nothing) |
| GET | `/report?month=` | Billed, collected, pending, overdue, outstanding |
| GET | `/report/students?month=` | Per-student breakdown |
| POST | `/login`, `/logout` | Owner session |

Open `http://127.0.0.1:8000/docs` for the interactive version.

---

## Design decisions

### Authentication: one API, two kinds of caller

The API has two very different callers, and one login method cannot serve both:

| Caller | Proves identity with | Why |
|---|---|---|
| **The academy owner** (browser) | Password, then a signed session cookie | A person can type a password and stays logged in for 12 hours |
| **n8n** (automation) | Secret key in an `X-API-Key` header | A scheduled workflow at 10 AM cannot type a password or hold a browser session |

With only a password login, the dashboard would be protected but every n8n workflow would fail with `401 Not authorized`. With only an API key, the owner would have to paste a secret into the browser.

A single dependency, `require_access`, runs before every protected route and lets a request through if **either** door is valid. Protecting a route means adding one line to its decorator, and the route logic does not change.

- The password is never stored, only a bcrypt hash, kept in `.env`.
- The session cookie is signed, so it cannot be forged or edited.
- API keys are compared with `secrets.compare_digest`, which avoids leaking information through timing.
- Secrets live in `.env`, which is excluded from version control.

**A lesson from debugging this:** there are two separate secrets in the system, and they fail in different places. The API key fails at n8n's HTTP Request node, and the Meta token fails at the WhatsApp node. Checking which node is red tells you which secret to fix.

**Trade-off:** the API key is a shared secret, so anyone who has it has the same access as n8n. A stronger design would issue each machine caller its own key so one can be revoked without affecting the others.

### Fee generation is idempotent

`POST /fees/generate/{month}` checks whether a record already exists before creating one, and the database's unique constraint backs that up. Running it twice creates nothing new, so a retry or a double click is harmless.

### Family payment is all or nothing

`POST /families/pay` works in two passes. Pass one validates every fee (exists, not already paid) and changes nothing. Pass two updates all of them and commits once. If any fee is invalid, none are marked paid.

### Notifications never block a payment

After a payment is saved, the call to n8n runs as a background task. If n8n is slow or offline, the owner's click still succeeds, because recording money must never depend on sending a message.

### One event format

Both payment routes send n8n the same shape: a list of children and a total. A single payment is simply a family of one, so the receipt workflow has a single code path.

### Grouping belongs in the API

Families are formed by normalized parent phone (`03001234567` and `+92 300 1234567` match). The API returns one item per family, so n8n only has to send one message per item and holds no grouping logic.

---

## Setup

**Requirements:** Python 3.10+, Node.js (to run n8n), a Meta developer account for WhatsApp.

### 1. Install

```bash
git clone <https://github.com/shahveerahmad/academy-fee-manager.git>
cd academy-fees
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

### 2. Create `.env`

Generate a password hash and two random secrets:

```bash
python -c "import bcrypt; print(bcrypt.hashpw(b'YourPassword', bcrypt.gensalt()).decode())"
python -c "import secrets; print(secrets.token_hex(32))"
```

Run the second command twice, once for the session secret and once for the API key. Then create `.env`:

```
OWNER_PASSWORD_HASH='<bcrypt hash>'
SESSION_SECRET='<random string>'
N8N_API_KEY='<another random string>'
```

Keep the single quotes, because a bcrypt hash contains `$` characters.

### 3. Run the API

```bash
uvicorn main:app --reload
```

The database file `academy.db` is created automatically on first start. Log in at `http://127.0.0.1:8000/login`.

### 4. Run n8n and import the workflows

```bash
npm install -g n8n
n8n start
```

Open `http://localhost:5678`, then:

1. Import the three files from `n8n-workflows/`.
2. Create a **Header Auth** credential named `X-API-Key` with your `N8N_API_KEY` as the value. Attach it to the HTTP Request nodes that call the API.
3. Create a **WhatsApp account** credential with your Meta access token and Business Account ID.
4. In each workflow's settings, set the timezone (for example `Asia/Karachi`).
5. **Publish** all three workflows. Schedules and production webhooks only run from the published version.
6. Make sure `N8N_RECEIPT_URL` in `main.py` points to the receipt workflow's **Production URL** (`/webhook/...`, not `/webhook-test/...`).

### 5. WhatsApp test setup

1. Create an app at `developers.facebook.com` with the WhatsApp use case.
2. On **API Setup**, note the Phone Number ID and Business Account ID, and generate an access token.
3. Add your own number under **To** and verify it.
4. Send "hi" to the test number so free-text messages are allowed.

Test tokens expire after 24 hours and must be regenerated and updated in the n8n credential.

---

## Limitations

These are deliberate scope decisions, not oversights.

- **WhatsApp is in Meta's test mode.** Only a small list of verified recipients can receive messages, the access token lasts 24 hours, and free-text messages work only within 24 hours of the recipient messaging the test number. A real deployment needs Meta business verification, a dedicated sender number, approved message templates for reminders, and a permanent system-user token.
- **No online payments.** The owner records payments manually. A payment gateway such as JazzCash requires a merchant account. The webhook pattern used for receipts is the same one a gateway callback would use.
- **Runs locally.** If the machine is asleep or off at 9 or 10 AM, that run is skipped. A real deployment would host FastAPI and n8n on an always-on server.
- **Authentication is minimal.** One owner account, no rate limiting on login attempts, HTTP only. Production would add rate limiting, HTTPS and secure cookies.
- **No dashboard button for family payment.** `POST /families/pay` exists and works, but the dashboard only marks single fees paid.
- **Reminder opt-in is not modelled.** WhatsApp expects recipients to have agreed to receive messages. An academy would collect that consent at admission.
- **Schema changes need a migration tool.** Tables are created with `create_all`, which never alters existing tables. Alembic would be the next step.

## Possible next steps

- Dashboard button to settle a whole family
- Payment gateway integration, with the owner entering their own merchant credentials
- **Assisted payment confirmation.** Match a parent's payment screenshot against the owner's payment SMS by amount and time, auto-confirming single matches and sending ambiguous ones to the owner for review. Not built because the match fields depend on what the payment provider's SMS actually contains (a shared transaction ID cannot be assumed), and manual marking already works
- Per-caller API keys that can be revoked individually
- Message templates and a permanent Meta token for production
- Docker setup for one-command deployment
- Cash-flow report (money received in a month, by `paid_on`) alongside the current per-month billing view

---

## Project structure

```
academy-fees/
├── main.py            # routes and business logic
├── models.py          # database tables
├── database.py        # database connection
├── auth.py            # password check and access dependency
├── dashboard.html     # owner dashboard
├── login.html         # login page
├── n8n-workflows/     # exported n8n workflows (JSON)
├── requirements.txt
├── .env               # secrets (not committed)
└── .gitignore
```

## License

Add a license of your choice (MIT is a common default for portfolio projects).
