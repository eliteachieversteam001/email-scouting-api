# Email Scouting API

This is the backend for Email Scouting (`scout.eliteachieversteam.com`). It takes the useful functionality from MailClean (email verification, lead scouting, dashboards, imports and exports) and runs it entirely on the **Elite Firebase project `eliteachievers-b8782`**.

```
Elite Firebase user
      ↓  signs in on the Email Scouting frontend
Firebase ID token  →  Authorization: Bearer <token>
      ↓
FastAPI  →  Firebase Admin SDK verify_id_token()
      (checks signature, audience/issuer = eliteachievers-b8782, expiry, revocation)
      ↓
Firebase UID  →  Elite users/{uid} (read-only: status, identity, isAdmin)
      ↓
Firestore queries scoped to that UID  (scouting_jobs, scouting_leads, …)
```

| Layer | Used for |
|---|---|
| Firebase Authentication | The only identity. There are no Email Scouting accounts, passwords, JWTs or signup. |
| Firestore | All Email Scouting data, written server-side through the Admin SDK |
| Firebase Admin SDK | Token verification and Firestore access (server-side credentials only) |
| Verification engine | MailClean logic, unchanged: format, DNS, MX, SMTP, disposable, catch-all, classification |
| Firebase Storage | Not needed. CSV imports are parsed in memory and exports are generated on the fly. |

There is **no MongoDB**.

## Firestore data

All Email Scouting data lives in these collections. Each per-member document stores the verified `userId` (Firebase UID).

| Collection | Contents |
|---|---|
| `scouting_pools/{CODE}` | Lead pools (admin-managed) |
| `scouting_leads/{sha1(CODE:email)}` | Leads; `claimedBy` is the member's UID |
| `scouting_jobs/{id}` + `results/{n}` | Claim-and-verify jobs |
| `email_verifications/{id}` + `results/{n}` | Bulk CSV verification jobs |
| `scouting_history/{id}` | Verification and outreach events |
| `scouting_usage/{uid}` + `days/{YYYY-MM-DD}` | Usage totals and daily series (`_platform` holds platform totals) |

These collections are server-only. Elite's security rules don't open them to browsers. `firestore.indexes.json` lists the composite indexes to deploy. `docs/MIGRATION.md` has the full MailClean → Firestore mapping.

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # the service-account JSON stays on the server, outside the repo
uvicorn app.main:app --port 8000
```

## Tests

```bash
scripts/test.sh             # starts the Firebase Auth + Firestore emulators and runs the full suite
ruff check app tests && mypy app tests
```

- **Persistence tests** run against the real Firestore emulator, which is wiped before each test.
- **Authentication tests** use real Firebase Admin SDK verification:
  - RS256 tokens signed locally: valid, expired, forged, from the wrong project, garbage
  - Auth emulator tokens with no mocks
- A plain `pytest` without the emulators runs only the pure tests and skips the rest.

## Access rules

These mirror the Elite frontend gate.

| Elite `users/{uid}` | Member endpoints | Admin endpoints |
|---|---|---|
| Missing, invalid, expired or revoked token | 401 | 401 |
| Firebase or Firestore unreachable | 503 (fails closed) | 503 |
| Valid token, but no Elite profile | 403 | 403 |
| `status: "banned"` or `"suspended"` | 403 | 403 |
| No photo, or `identityVerification.status` ∉ {verified, manual_review} | 403 | 403 |
| Active and verified | ✅ | 403 |
| `isAdmin: true` (the same flag the Elite VPS API uses) | ✅ | ✅ |

`/health` is the only public endpoint. Jobs are owner-only: requesting another member's job returns 403.
