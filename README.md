# Email Scouting API

This is the backend for Email Scouting (`scout.eliteachieversteam.com`). It is MailClean's email-verification and lead-scouting functionality, rebuilt so that the **only identity is the Elite Firebase account**.

```
Elite Firebase user (project eliteachievers-b8782)
        ↓  signs in on the Email Scouting frontend
Firebase ID token  →  Authorization: Bearer <token>
        ↓
Email Scouting API: firebase_admin.auth.verify_id_token()
        checks the signature, audience/issuer (eliteachievers-b8782), expiry, and revocation
        ↓
Firebase UID  →  users/{uid} in the Elite Firestore (read-only)
        checks the status (banned/suspended), identity verification, and isAdmin
        ↓
the member's scouting data in MongoDB (user_id = Firebase UID)
```

There is no MailClean account, password, JWT, signup, or users collection.

## Run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # fill in values; the service-account JSON stays on the server
uvicorn app.main:app --port 8000
```

Checks: `ruff check app tests` · `mypy app tests` · `pytest`

To run the integration tests against the Firebase emulators, which exercise real tokens with no auth mocks:

```bash
firebase emulators:start --only auth,firestore --project demo-elite &
FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:9099 FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 FIREBASE_PROJECT_ID=demo-elite pytest
```

## Access rules

These mirror the Elite frontend gate.

| Elite `users/{uid}` | Member endpoints | Admin endpoints |
|---|---|---|
| no or invalid or expired or revoked token | 401 | 401 |
| token is valid, but there's no Elite profile | 403 `Elite profile not found` | 403 |
| `status: "banned"` | 403 `Account banned` | 403 |
| `status: "suspended"` | 403 `Account suspended` | 403 |
| no photo, or `identityVerification.status` ∉ {verified, manual_review} | 403 `Identity verification required on Elite` | 403 |
| active and verified | ✅ | 403 `Admin access required` |
| `isAdmin: true` | ✅ (never locked out) | ✅ |

Admin status comes from Elite's `users/{uid}.isAdmin`. That is the same check the Elite VPS API uses.

## Endpoints

`/health` is the only public endpoint. Everything else needs a verified token.

| Access | Endpoints |
|---|---|
| Member | `GET /dashboard/me`, `POST /verify/single`, `POST /verify/bulk`, `GET /results/{id}`, `GET /results/{id}/export`, `GET /leads/countries`, `POST /leads/claim-and-verify`, `GET /leads/status/{id}`, `GET /leads/history`, `GET /leads/results/{id}`, `GET /leads/export/{id}`, `POST /scout/log` |
| Admin | `POST /admin/leads/upload/{code}`, `GET /admin/leads/stats`, `DELETE /admin/leads/delete/{code}`, `DELETE /admin/leads/delete-available/{code}`, `GET /admin/leads/sample/{code}`, `GET /admin/leads/jobs`, `GET /dashboard/admin`, `GET /dashboard/admin/user/{uid}`, `GET /admin/scout-logs` |

Jobs are private to their owner. A member who requests someone else's job gets 403 `Not your job`.

See `docs/MIGRATION.md` for what changed from MailClean.
