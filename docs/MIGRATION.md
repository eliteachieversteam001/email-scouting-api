# From MailClean to the Email Scouting API

Source: `hamzaibnzakir/mailclean-api` (FastAPI). I read every module in full before porting: `main.py`, `auth.py`, `routes_leads.py`, `routes_dashboard.py`, `routes_gmail.py`, `verifier.py`, `config.py`.

## Authentication: removed

| MailClean | Status |
|---|---|
| `users` Mongo collection (email, bcrypt password, role, status) | **Removed.** Accounts live in Elite Firebase. |
| `POST /auth/signup`, `POST /auth/login`, `GET /auth/me` | **Removed.** Users sign up and sign in on Elite. |
| HS256 JWT (`JWT_SECRET`, `create_token`, `decode_token`) | **Removed.** Replaced by Firebase ID token verification. |
| `seed_main_admin` with the default password `admin123` | **Removed.** |
| `role: user/admin/main_admin` | **Replaced** by Elite `users/{uid}.isAdmin`. |
| `status: pending/approved/suspended/banned` | **Replaced** by Elite `status` plus identity verification. |
| `GET /admin/users`, `PUT /admin/users/{id}/status`, `PUT /admin/users/{id}/role` | **Removed.** Members are managed in the Elite admin console. |
| `require_approved`, `require_admin` | Kept as dependencies, now backed by Firebase and the Elite profile. |

## Gmail and tracking: not ported

The Email Scouting frontend never calls these endpoints:

- `/auth/google/*`
- `/gmail/*`
- `/pixel/*`
- `/scout/create-track`
- `/scout/tracks`
- `/admin/tracks`
- `/admin/gmail/stats`

They also had hardcoded redirect and pixel URLs pointing at `brainboxecomlab.com`, and they stored Google OAuth refresh tokens. Outreach in the frontend works by copying BCC batches, with `POST /scout/log` recording each batch. That flow is kept and needs no Gmail access.

## Functionality: kept, with the same response shapes

The following work the same way, keyed by Firebase UID:

- Single and multi-address verification
- Bulk CSV verification and category export
- Lead pools
- Claim-and-verify jobs, including the 45-minute abandonment rule, the 3,000-per-day claim limit, and the 100–2,000 claim size
- Job status, history, results and export
- `/dashboard/me` statistics and 30-day series
- Outreach batch logging
- Admin pool upload, stats, sample, delete, and the job list
- The admin dashboard

The verifier logic (format, domain, MX, SMTP, catch-all, then classify into DELIVERS / UNKNOWN / BOUNCE) is unchanged.

## Fixed along the way

- **Bulk jobs were in process memory with no owner check.** Any approved user who knew a job ID could read another user's results, and a restart lost every job. Bulk jobs are now stored in `verify_jobs` and `verify_results` and scoped to the owner's UID. A failed or stale job returns 404 with a friendly message.
- **Exception text was stored in results and sent to clients** (`str(e)`). It is now replaced with a generic message.
- **A catch-all 500 handler** now makes sure no stack traces or database errors reach clients.
- **CORS was `allow_origins=["*"]`.** It is now an explicit allow-list, with credentials disabled because the API uses bearer tokens only.
- **Input limits added:** CSV upload size, the pool-code format, up to 25 addresses per single-verify request, and bounds on `/scout/log` fields.
- **Swagger/OpenAPI docs** are disabled in production.

## Data (MongoDB): application records only

| Collection | Purpose | Identity field |
|---|---|---|
| `leads` | Admin-uploaded pools | `claimed_by` = Firebase UID, `uploaded_by` = admin UID |
| `lead_jobs` | Claim-and-verify jobs | `user_id` = Firebase UID |
| `verify_jobs` + `verify_results` | Bulk CSV jobs (**new**) | `user_id` = Firebase UID |
| `verify_logs`, `scout_logs` | Dashboard statistics | `user_id` = Firebase UID |
| `country_meta` | Pool display names | — |

`user_name` and `user_email` on these records are display snapshots for admin views. They are never used for identity.

### Existing MailClean data

- **Lead pools:** the `leads` documents can be reused as they are.
- **Old per-user records** (`lead_jobs`, `verify_logs`, `scout_logs`): these reference MailClean Mongo user IDs, and those users no longer exist. Start with a fresh database, or run a one-off script that maps each old user's email to a Firebase UID (`auth.get_user_by_email`) and rewrites `user_id`. That script has **not** been run.
- **Collections to drop** once the old service is retired: `users`, `gmail_tokens`, `gmail_sends`, `batch_tracks`, `open_events`.
