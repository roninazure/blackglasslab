# PARALLAX / Development name - Swarm Edge

PARALLAX is Swarm Axis's production prediction-market intelligence system. It scans supported venues, scores market opportunities, applies fail-closed publication rules, publishes sanitized customer feeds, and sends qualified alerts through **ntfy**.

The commercial objective is simple: find real, executable market opportunities with enough edge to matter, surface them quickly, and avoid wasting capital, API budget, or operator time.

> **Production principle:** revenue and executable opportunity yield come before architecture work. Do not add complexity unless it improves signal quality, timeliness, reliability, or commercial value.

---

## Current production state

As of **2026-10-05**:

- Production code baseline: `82dafc21d09089893153f6342658df44eb91b998`
- Authoritative code branch: `main`
- Live generated-data branch: `parallax-live-data`
- Preserved review/rollback branch: `review/nfl-p0-offline`
- Runtime: macOS `launchd`
- Production service: `com.swarmedge.parallax-sports`
- Alert provider: **ntfy**
- Current production focus: **NFL + MLB**
- Real-money execution: **not part of this runtime**

The checked-out production code and GitHub `main` are expected to remain aligned. Runtime data is deliberately separated from application code.

---

## Repository model

### `main`

Authoritative application code.

Code changes belong here only after they are tested and safe for production.

### `parallax-live-data`

Automation-owned live feed/data branch.

It changes frequently because unattended publishing updates customer-facing data. **Do not merge this branch into `main` simply because GitHub offers a “Compare & pull request” button.**

### `review/nfl-p0-offline`

Preserved tested baseline used during the NFL P0 hardening work. It currently matches the production-tested code line and is retained until no workflow or operational dependency requires it.

Historical feature work that was intentionally retired from the active branch list is preserved with `archive-*` tags.

---

## What PARALLAX does

The production path is:

```text
venue discovery
    ↓
market normalization
    ↓
authoritative schedule / identity reconciliation
    ↓
evidence + model scoring
    ↓
economic / execution gates
    ↓
BUY / WATCH / PASS
    ↓
publication certification
    ↓
sanitized customer feed
    ↓
ntfy alerting for eligible events
```

The browser/customer feed is the **post-certification boundary**. Internal certification fields are not required in the customer payload once the publisher has admitted a play.

PARALLAX is an intelligence and publication system. It does not place trades from this production sports runtime.

---

## Supported venue behavior

PARALLAX currently works across supported Kalshi and Polymarket US market data paths.

Important safety behavior:

- venue failures are isolated
- one unhealthy venue must not suppress a separately certified BUY from a healthy venue
- rate limits are treated as a stop condition, not a reason to hammer the provider
- stale quotes must not remain actionable
- executable-book requirements fail closed
- public/customer data is sanitized before publication

### Kalshi

Kalshi discovery is rate-limited and bounded. Public requests use explicit request ceilings and lock out the client after a detected rate-limit event.

### Polymarket US

Polymarket US acquisition has its own health and entitlement behavior. Authentication or entitlement problems are treated as that venue's problem and must not suppress healthy Kalshi output.

---

## Sports lanes

### NFL

NFL P0 hardening is complete.

Key production behavior includes:

- fast refresh relative to the supervisor cadence
- stale BUYs become non-actionable
- BUY publication is isolated from failures on another venue
- executable-market state is reconciled across refreshes/restarts
- customer-feed publication is downstream of certification

The production supervisor wakes frequently; NFL refresh operates on a faster cadence than the slower sports lanes.

### MLB

MLB uses authoritative schedule identity and market-to-game reconciliation before a BUY can become publication eligible.

The MLB path includes protection against ambiguous city-form names and venue naming differences. Official slate identity is used to canonicalize teams before evidence certification.

A BUY must survive freshness, market state, schedule identity, evidence/model, and publication gates before it can appear as an actionable customer play.

### CFB

CFB code and historical work exist in the repository, but CFB is not part of the current frozen NFL/MLB production-change scope. Do not modify CFB incidentally while working on NFL or MLB.

---

## BUY alerting and ntfy

PARALLAX uses the existing **ntfy** integration for outbound production notifications.

Alert state is persisted in:

```text
~/Library/Application Support/SwarmEdge/state/parallax_inbox.sqlite
```

The alert pipeline provides:

- stable BUY identity so normal quote movement does not create duplicate BUY alerts
- delivery persistence and deduplication
- retry handling
- global ntfy cooldown after HTTP 429
- bounded retry attempts
- fail-safe behavior so alert delivery errors do not crash the sports scan

The current 429 protection uses a **300-second global cooldown**. When ntfy rate-limits the system, additional pending BUYs remain queued rather than being fired repeatedly.

Do not add a second notification provider. Notification defects should be traced through the existing:

```text
PARALLAX → alert store → ntfy transport → delivery state
```

The final live acceptance proof for any alerting change should be a **natural qualifying BUY** delivered by the unattended pipeline. Do not manufacture a BUY or run provider scans merely to create an alert.

---

## Operational invariants

These rules are deliberate:

1. **No forced BUYs.** Scoring thresholds are not relaxed to make the system look active.
2. **No routine manual scans.** Normal NFL/MLB operation is unattended.
3. **Rate limit means stop.** Do not repeatedly retry a provider that is returning 429.
4. **Venue isolation.** One venue failure must not suppress a certified BUY from another healthy venue.
5. **Fail closed.** Missing mapping, stale data, incomplete evidence, or unsafe execution state must not become a customer BUY.
6. **Customer feed is post-certification.** Internal certification metadata does not have to leak into the browser schema.
7. **Production first.** Do not change working production without a concrete regression or revenue-backed reason.
8. **Rollback stays available.** Deployment changes must preserve a known-good rollback path.

---

## Production deployment layout

Runtime assets live under:

```text
~/Library/Application Support/SwarmEdge/
```

Current deployment structure:

```text
releases/<git-sha>/    immutable release source
venvs/<git-sha>/       release-specific Python environment
state/                 SQLite/runtime state
logs/                  runtime logs
parallax-alerts.env    alert configuration
```

Current retained release generations:

```text
82dafc21...   current production
48fe365d...   immediate rollback
f3009fe6...   second rollback
```

The active system LaunchDaemon is:

```text
/Library/LaunchDaemons/com.swarmedge.parallax-sports.plist
```

Two rollback plist generations are intentionally retained.

---

## Verify production

Read-only verification:

```bash
sudo launchctl print system/com.swarmedge.parallax-sports | \
grep -E 'state =|pid =|program =|PARALLAX_RELEASE_SHA'
```

Expected characteristics:

- `state = running`
- program path under the current release venv
- `PARALLAX_RELEASE_SHA` matches the intended deployment

Check the local source checkout:

```bash
cd "$HOME/swarm-runtime/swarm-edge"
git status --short
git branch --show-current
git rev-parse HEAD
```

The normal clean state is:

```text
(no status output)
main
<current production-tested SHA>
```

---

## Testing

Use an existing development/test environment rather than installing test packages into the production venv.

Focused production regression testing should cover the subsystem being changed plus the neighboring sports lanes most likely to regress.

For the 2026-10-05 ntfy/global-cooldown release, the final focused regression gate was:

```text
161 passed
```

Do not interpret test count alone as production acceptance. Runtime behavior must still be verified naturally after deployment.

---

## Development and deployment discipline

Use this sequence:

```text
BUILD
  ↓
VERIFY
  ↓
ATTACK
  ↓
IMPROVE
  ↓
RE-VERIFY
  ↓
FREEZE
```

For production work:

- prefer the smallest deterministic change
- inspect before editing
- avoid broad refactors during incidents
- test the exact failure mode
- preserve rollback
- deploy the tested SHA
- verify the running process and release SHA
- freeze when the defect is proven fixed

Do not use the dirty/working checkout as an ad hoc production deployment source. Production releases are immutable SHA-addressed directories.

---

## Public feed

The customer-facing feed is a sanitized artifact produced after upstream certification.

For a play to appear as a customer BUY, the upstream scanner/publisher path must have already admitted it. Internal fields such as `publication_eligible` or internal verdict structures may be intentionally absent from the customer schema.

The frontend should consume the customer contract, not attempt to recreate internal certification logic.

---

## Alert incident note: 2026-10-05

A production ntfy incident exposed two problems:

1. active BUY identity was too sensitive to normal quote changes, creating repeated alert identities
2. HTTP 429 handling cooled down individual deliveries but initially allowed other distinct BUYs to continue hitting ntfy

The current production fix:

- stabilizes active BUY identity
- classifies HTTP 429 explicitly
- applies a global ntfy cooldown
- leaves newly queued BUYs pending during cooldown
- stops traversing additional pending deliveries once ntfy returns 429

After deployment, repeated 429 traffic stopped. Existing queued deliveries are allowed to remain pending; they are not manually replayed to manufacture success.

---

## Revenue mandate

PARALLAX is a commercial product, not an infrastructure demonstration.

Engineering work should be prioritized by whether it improves one or more of:

- executable opportunity yield
- signal quality
- timeliness
- customer trust
- capital efficiency
- realized return potential
- reliability of delivery

If a feature does not materially improve one of those outcomes, it should not displace revenue work.

---

## Near-term agenda

1. Keep NFL/MLB running unattended and observe the next natural qualifying BUY end to end.
2. Measure executable opportunity yield rather than raw scan volume.
3. Audit whether `review/nfl-p0-offline` is still referenced anywhere.
4. Improve customer-facing value only where it supports conversion, retention, or better decisions.
5. Add a visible Research/Challenger agent later, after the revenue path remains stable and the agent has a defined commercial role.

---

## Project ownership

PARALLAX is developed under **Swarm Axis**.

The system should remain easy to operate, easy to roll back, and difficult to fool into publishing low-confidence or stale BUYs.
