# Server review (2026-09-18)

Update (2026-09-19): the staff web transport now runs in its own process behind the gateway. The transactional API and domain operations still share one database and deployment contract with it; the decision below remains applicable to further domain splits.

## Decision

Keep one transactional API as a **modular monolith**. The inference service and
training worker already run separately with independent resource limits. Moving
auth, moderation, scoring and device state into separate services would replace
local database transactions with distributed consistency and retry problems.
Split those only if independent deployment, ownership or measured load calls
for it. Web and JSON transports now use the same moderation operation.

## Verified improvements

- Rotation of refresh tokens uses a conditional update to consume a token once,
  including requests in different processes. Web logout revokes its server-side
  session; staff session management uses the same session table. Blocked staff
  cannot perform privileged actions or enter the web console, while blocked
  regular users can still see and manage their account.
- Coordinates reject NaN, infinity and out-of-range values before proximity
  checks. Review boxes reject non-finite and out-of-bounds values.
- Rate limits group all resource IDs under a stable allowance and ignore forged
  leftmost proxy hops; metrics have fixed labels for unmatched paths and methods.
  The in-process limiter is an additional guard, not a distributed quota.
- Moderation is serialized on the review and user records; score writes lock the
  user before changing the balance. Training claims use PostgreSQL `SKIP LOCKED`.
- Deposit history fetches up to 100 sessions with their review and composter
  in one query instead of a query per session.
- File uploads and ML downloads have byte and pixel limits; malformed ML
  responses yield manual review. Error details from the ML service are logged
  internally rather than returned to clients.
- The public reverse-proxy body limit now allows the 32 MiB firmware uploads
  that the web console accepts; the handler retains its own 32 MiB file cap.
- Device deletion clears foreign-key dependents, retaining incidents without
  the deleted device and points without the deleted review.
- Runtime dependencies were refreshed after a vulnerability audit. CI runs
  lint, tests, migration checks, dependency audit and PostgreSQL tests. Docker
  build context excludes local environment files and caches.

## Operational follow-up

Before deployment, stage the images and run an Android/ESP32/web smoke test
against PostgreSQL, S3 and the actual ML service. The unit suite mocks S3 and
does not measure production throughput or verify on-device behavior. Keep
backup restoration exercises separate from the archive integrity check; a
successful checksum is not proof that recovery works. Consider shared
rate-limiting state only when scaling API workers beyond one process.
