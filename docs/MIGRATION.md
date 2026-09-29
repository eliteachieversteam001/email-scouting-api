# MailClean → Email Scouting API migration

The source is `hamzaibnzakir/mailclean-api`, used for its **functionality only**. I read every module before porting: `main.py`, `auth.py`, `routes_leads.py`, `routes_dashboard.py`, `routes_gmail.py`, `verifier.py`, `config.py`.

The target stack is the Elite Firebase project **`eliteachievers-b8782`**:

- **Firebase Authentication:** the only identity.
- **Firestore:** all Email Scouting data, read and written server-side via the Admin SDK.
- **Firebase Storage:** **not needed**. CSV imports are parsed in memory from the upload, and CSV exports are generated on the fly from Firestore. Nothing file-like is kept.

There is **no MongoDB**: no driver, no connection string, no collections.

## Migration map

Every MongoDB collection in MailClean, plus its in-memory job dict, maps as follows.

| # | OLD (MailClean) | Used for | NEW (Firestore, project `eliteachievers-b8782`) |
|---|---|---|---|
| 1 | `users` (email, bcrypt `password`, `role`, `status`, `emails_verified`, `emails_scouted`, `batches_sent`, `last_active`, `gmail_accounts`) | Login, roles, approval, counters | **Removed.** Identity is Firebase Auth (UID). Status, admin and identity are read from Elite `users/{uid}` (**read-only**; never written). Counters move to `scouting_usage` (row 8). |
| 2 | `leads` (email, country, domain, status, claimed_by, claimed_by_job, claimed_at, verified_result, uploaded_at) with a unique index on (email, country) | Lead pools and claims | `scouting_leads/{sha1(COUNTRY:email)}`. The deterministic ID replaces the unique index. Fields: `email`, `country`, `domain`, `status` (available/claimed/expired), `claimedBy` (**Firebase UID**), `claimedByJob`, `claimedAt`, `verifiedResult`, `verifiedAt`, `uploadedBy` (admin UID), `uploadedAt`. |
| 3 | `country_meta` (code, name, flag) | Custom pool names | `scouting_pools/{CODE}`, with fields `code`, `name`, `flag`, `createdBy`, `createdAt`, `updatedAt`. Each pool now has a document, so pools can be listed without `distinct()`. |
| 4 | `lead_jobs` (user_id, user_name, user_email, country, amount, status, progress, total, `results[]`, created_at, completed_at) | Claim-and-verify jobs | `scouting_jobs/{autoId}`: `userId` (**Firebase UID**), `userName`, `country`, `amount`, `status`, `progress`, `total`, `createdAt`, `completedAt`. Results go in the subcollection `scouting_jobs/{id}/results/{000001…}`, which avoids Firestore's 1 MiB document limit. |
| 5 | in-memory `jobs = {}` in `main.py` (bulk CSV jobs; lost on restart; **no owner check**) | Bulk verification | `email_verifications/{autoId}`: `userId` (**Firebase UID**), `status`, `progress`, `total`, `createdAt`, `completedAt`. Results go in `email_verifications/{id}/results/{000001…}`. Jobs are now persistent and owner-only. |
| 6 | `verify_logs` (user_id, email_count, source, sent_at) | Verification usage events | `scouting_history/{autoId}` with `type: "verification"`, `userId`, `source` (single/single_multi/bulk/scout_leads), `emailCount`, `createdAt`. Totals also go to `scouting_usage` (row 8). |
| 7 | `scout_logs` (user_id, batch_number, total_batches, email_count, subject, sent_at) | Outreach batches (Scout Mode) | `scouting_history/{autoId}` with `type: "outreach"`, `userId`, `batchNumber`, `totalBatches`, `emailCount`, `subject`, `createdAt`. |
| 8 | Mongo `$sum` aggregations over `verify_logs`/`scout_logs`, plus the `users` counters | Dashboard statistics and 30-day series | `scouting_usage/{uid}` holds totals (`verified`, `scouted`, `batches`, `claimed`). `scouting_usage/{uid}/days/{YYYY-MM-DD}` holds daily counts. Both are maintained with atomic `Increment`s. Platform totals live under the reserved ID `scouting_usage/_platform`. A dashboard needs 1 document read plus ≤31 day documents instead of scanning logs. |
| 9 | `gmail_tokens`, `gmail_sends` | Gmail OAuth sending | **Not migrated.** The Email Scouting frontend doesn't use it, the OAuth redirects were hardcoded to `brainboxecomlab.com`, and it stored Google refresh tokens. |
| 10 | `batch_tracks`, `open_events` | Tracking pixel | **Not migrated.** Unused by the frontend, and the pixel URL was hardcoded to the old domain. |

### Lists

MailClean had **no lists storage**. The frontend's Lists page is built from completed `scouting_jobs` grouped by pool, and that keeps working unchanged. A dedicated `email_lists` collection with create/rename/delete endpoints would be new functionality and would also need frontend changes. It is intentionally not invented here; it can be added later as `email_lists/{autoId}` (`userId`, `name`, `leadIds[]`, timestamps).

### Ownership

- Every per-member document stores `userId` (or `claimedBy`) set to the UID from the **verified** ID token.
- Reads check ownership. Another member's job returns 403 `Not your job`.
- The client never supplies a UID.
- `userName` is a display snapshot for admin views only.

### Security rules: explicitly server-only

The explicit deny rules are on the **`email-scouting-firestore-rules`** branch of `eliteachieversteam001/elitesucessachievers`. That change is additive only; no existing Elite rule was modified.

```
match /scouting_pools/{document=**}      { allow read, write: if false; }
match /scouting_leads/{document=**}      { allow read, write: if false; }
match /scouting_jobs/{document=**}       { allow read, write: if false; }
match /email_verifications/{document=**} { allow read, write: if false; }
match /scouting_history/{document=**}    { allow read, write: if false; }
match /scouting_usage/{document=**}      { allow read, write: if false; }
```

- **Browsers / client SDK** (signed out, member, record owner, staff, admin): **denied**, including the `results/` and `days/` subcollections.
- **This API through the Firebase Admin SDK:** **allowed**. The Admin SDK bypasses rules.
- **Verification:** the Elite rules suite passes 119/119, including 10 new server-only tests. This API's own suite passes 150/150 with those rules loaded in the emulator.

### Indexes

The composite indexes in `firestore.indexes.json` have been merged into Elite's `firestore.indexes.json` on the same branch (13 existing indexes plus 7 new ones). Deploy them before the API goes live. The emulator doesn't require them.

### Existing MailClean data

Pools can be re-imported through the admin upload endpoint by exporting the old `leads` as CSV. Per-user history in MailClean is keyed by Mongo user IDs, which no longer exist, so it isn't carried over.

## Authentication: removed

- The `users` collection, bcrypt passwords, `JWT_SECRET` and HS256 JWTs
- `/auth/signup`, `/auth/login` and `/auth/me`
- `seed_main_admin` (default password `admin123`)
- `role` and `main_admin`
- `/admin/users*`

These are replaced by Firebase ID token verification (Admin SDK) and Elite `users/{uid}`. Suspended and banned members are blocked, identity verification is required, and admin comes from `isAdmin`.

## Functionality: kept, with the same response shapes

- Verification engine, unchanged: format, domain/DNS, MX, SMTP, disposable, catch-all, and the DELIVERS/UNKNOWN/BOUNCE classification
- Single and multi-address verification
- Bulk CSV verification and export
- Pools
- Claim-and-verify: 100–2000 per claim, 3000 per day, one active job at a time, and the 45-minute abandonment release
- Status, history, results and export
- Dashboard statistics and series
- Outreach logging
- Admin pool upload, stats, sample, delete and job list
- Admin dashboards
