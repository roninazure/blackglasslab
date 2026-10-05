<div align="center">

# ◈ PARALLAX

### **Prediction-Market Intelligence Engine**

**Discover → Reconcile → Score → Certify → Publish**

<p>
  <img src="https://img.shields.io/badge/STATUS-LIVE-22c55e?style=for-the-badge&labelColor=111827" alt="LIVE"/>
  <img src="https://img.shields.io/badge/MODE-UNATTENDED-2563eb?style=for-the-badge&labelColor=111827" alt="UNATTENDED"/>
  <img src="https://img.shields.io/badge/SPORTS-NFL_%7C_MLB-7c3aed?style=for-the-badge&labelColor=111827" alt="NFL MLB"/>
  <img src="https://img.shields.io/badge/EXECUTION-READ_ONLY-f59e0b?style=for-the-badge&labelColor=111827" alt="READ ONLY"/>
  <img src="https://img.shields.io/badge/ALERTS-ntfy-06b6d4?style=for-the-badge&labelColor=111827" alt="ntfy"/>
</p>

**SWARM AXIS // PRODUCTION INTELLIGENCE**

> Find the edge. Prove the edge. Publish only what survives.

</div>

---

## ◈ Mission

PARALLAX is Swarm Axis's production prediction-market intelligence system.

It scans supported venues, reconciles market identity against authoritative sports schedules, scores opportunities, applies execution and publication gates, emits a sanitized customer feed, and delivers qualified alerts through **ntfy**.

The mandate is commercial:

> **Maximize executable opportunity yield without sacrificing trust.**

PARALLAX is not an engineering showcase. Architecture exists to serve signal quality, speed, reliability, customer value, and realized-return potential.

---

## ◈ Production Command Deck

| Surface | Current state |
|---|---|
| **Runtime** | macOS `launchd` |
| **Service** | `com.swarmedge.parallax-sports` |
| **Operating mode** | Unattended |
| **Primary sports lanes** | NFL + MLB |
| **Alerts** | ntfy |
| **Execution** | Read-only; no real-money order placement |
| **Deployed production SHA** | `82dafc21d09089893153f6342658df44eb91b998` |
| **Current GitHub main** | advances independently through tested source/docs cleanup |
| **Live data branch** | `parallax-live-data` |
| **Rollback posture** | two known-good generations retained |

**Important:** the deployed production release and GitHub `main` are intentionally distinct concepts. Production runs an immutable tested SHA. `main` may advance with documentation or cleanup work before a new deployment is approved.

---

## ◈ The PARALLAX Decision Chain

```text
                 ┌─────────────────────────────┐
                 │      MARKET VENUES          │
                 │   Kalshi · Polymarket US    │
                 └──────────────┬──────────────┘
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │        DISCOVERY            │
                 │ bounded · rate-aware        │
                 └──────────────┬──────────────┘
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │      NORMALIZATION          │
                 │ venue data → common model   │
                 └──────────────┬──────────────┘
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │    IDENTITY RECONCILIATION  │
                 │ authoritative game/slate    │
                 └──────────────┬──────────────┘
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │      EVIDENCE + MODEL       │
                 │ probability · confidence    │
                 └──────────────┬──────────────┘
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │      ECONOMIC GATES         │
                 │ price · fees · execution    │
                 └──────────────┬──────────────┘
                                │
                       BUY / WATCH / PASS
                                │
                                ▼
                 ┌─────────────────────────────┐
                 │  PUBLICATION CERTIFICATION  │
                 │ fail closed by default      │
                 └──────────────┬──────────────┘
                                │
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
      ┌─────────────────────┐       ┌─────────────────────┐
      │ SANITIZED CUSTOMER  │       │      NTFY ALERT     │
      │        FEED         │       │ qualified events    │
      └─────────────────────┘       └─────────────────────┘
```

The browser/customer feed is the **post-certification boundary**. If a BUY appears in the customer feed, upstream PARALLAX has already admitted it through the publication path.

---

## ◈ What Makes PARALLAX Different

### 01 // No forced BUYs

PARALLAX does not relax thresholds to make the product look busy. A quiet system is preferable to fabricated confidence.

### 02 // Fail closed

Missing identity, stale data, incomplete evidence, unsafe execution state, or invalid publication state must not become a customer BUY.

### 03 // Venue isolation

A failure at one venue must not suppress a separately certified opportunity from another healthy venue.

### 04 // Natural acceptance

The strongest proof of an alerting or publication change is the **next naturally qualifying BUY** through the unattended pipeline—not a manufactured test signal.

### 05 // Rate limits are signals

HTTP 429 means stop and cool down. PARALLAX does not hammer an unhealthy provider to preserve the appearance of activity.

### 06 // Customer contract over internal plumbing

The public feed contains the customer representation of a certified play. Internal certification flags are not required to leak into the browser schema.

---

## ◈ Sports Intelligence Lanes

### NFL // Hardened

NFL P0 hardening is complete.

The lane includes:

- fast refresh relative to the supervisor cadence
- authoritative schedule and market reconciliation
- stale BUY invalidation
- executable-book enforcement
- venue-isolated certification
- restart-safe market state
- downstream customer publication only after certification

### MLB // Hardened

MLB uses authoritative slate identity before a play can become publication eligible.

Protection includes:

- city-form team canonicalization
- venue naming reconciliation
- freshness checks
- market-state checks
- evidence/model gates
- publication certification

A market does not become actionable merely because a venue lists it.

### CFB // Preserved

CFB code remains in the repository, but it is outside the current frozen NFL/MLB production-change scope.

Do not modify CFB incidentally during NFL or MLB work.

---

## ◈ Alert Fabric // ntfy

PARALLAX uses **ntfy** as the production notification provider.

```text
PARALLAX
   │
   ▼
Alert Store
   │
   ▼
Deduplication
   │
   ▼
Global Cooldown / Retry Policy
   │
   ▼
ntfy
   │
   ▼
Operator / Customer Notification
```

Alert state persists in:

```text
~/Library/Application Support/SwarmEdge/state/parallax_inbox.sqlite
```

Current protections include:

- stable BUY identity across ordinary quote drift
- delivery persistence
- deduplication
- retry handling
- HTTP 429 classification
- **300-second global ntfy cooldown**
- queue preservation during cooldown
- bounded retry behavior
- alert failures that do not crash the sports scanner

**Do not add a second notification provider.** Alert defects belong in the existing PARALLAX → ntfy path.

---

## ◈ Production Runtime

```text
~/Library/Application Support/SwarmEdge/
├── releases/<git-sha>/      immutable source releases
├── venvs/<git-sha>/         release-specific environments
├── state/                   SQLite + runtime state
├── logs/                    operational logs
└── parallax-alerts.env      alert configuration
```

The active service is:

```text
/Library/LaunchDaemons/com.swarmedge.parallax-sports.plist
```

Retained production generations:

```text
82dafc21...   CURRENT PRODUCTION
48fe365d...   ROLLBACK #1
f3009fe6...   ROLLBACK #2
```

Production releases are immutable. A dirty working tree is never the deployment source.

---

## ◈ Read-Only Production Verification

```bash
sudo launchctl print system/com.swarmedge.parallax-sports | \
grep -E 'state =|pid =|program =|PARALLAX_RELEASE_SHA'
```

Expected:

```text
state = running
program = .../venvs/<deployed-sha>/bin/python
PARALLAX_RELEASE_SHA = <deployed-sha>
```

Local source checkout:

```bash
cd "$HOME/swarm-runtime/swarm-edge"
git status --short
git branch --show-current
git rev-parse HEAD
```

A clean checkout may be ahead of the currently deployed production SHA. That is normal until an explicit deployment occurs.

---

## ◈ Repository Topology

### `main`

Authoritative application source and documentation.

### `parallax-live-data`

Automation-owned customer/live-data branch.

It changes frequently. **Do not merge it into `main` merely because GitHub presents a “Compare & pull request” banner.**

### `review/nfl-p0-offline`

Preserved tested NFL P0 baseline retained until all workflow and operational references are retired.

Historical generations may be retained through `archive-*` tags instead of active branches.

---

## ◈ Release Discipline

PARALLAX follows:

```text
BUILD
  │
  ▼
VERIFY
  │
  ▼
ATTACK
  │
  ▼
IMPROVE
  │
  ▼
RE-VERIFY
  │
  ▼
FREEZE
```

For production work:

- prefer the smallest deterministic change
- inspect before editing
- avoid broad refactors during incidents
- test the exact failure mode
- preserve rollback
- deploy the exact tested SHA
- verify the running process and release SHA
- freeze once the defect is proven fixed

---

## ◈ Production Invariants

> These are not suggestions. They are operating law.

1. **No forced BUYs.**
2. **No routine manual provider scans.**
3. **429 means stop.**
4. **Venue failures stay isolated.**
5. **Unsafe or incomplete data fails closed.**
6. **Customer feed is post-certification.**
7. **Working production is not changed without a concrete reason.**
8. **Every deployment preserves rollback.**
9. **No real-money execution from the current sports runtime.**
10. **Revenue value outranks architectural novelty.**

---

## ◈ 2026-10-05 ntfy Hardening

A production alert incident exposed two failure modes:

- BUY identity changed with normal quote movement, creating duplicate alert identities.
- HTTP 429 handling initially cooled individual deliveries while allowing other queued BUYs to continue hitting ntfy.

The production fix:

- stabilized BUY identity
- explicitly classified HTTP 429
- added a global ntfy cooldown
- preserved new BUYs in the queue during cooldown
- stopped additional pending delivery traversal after rate limiting

After deployment, repeated 429 traffic stopped.

Acceptance remains deliberately conservative: the final proof is a naturally qualifying BUY through the unattended production path.

---

## ◈ Testing Philosophy

A passing test suite is necessary, not sufficient.

PARALLAX acceptance combines:

```text
UNIT / REGRESSION TESTS
          +
RUNTIME VERIFICATION
          +
NATURAL PRODUCTION BEHAVIOR
          =
ACCEPTANCE
```

Use development/test environments for pytest. Do not install test tooling into the production venv solely to run validation.

---

## ◈ Revenue Doctrine

PARALLAX exists to create commercially useful intelligence.

Engineering priority is determined by impact on:

| Priority | Outcome |
|---|---|
| **1** | Executable opportunity yield |
| **2** | Signal quality |
| **3** | Timeliness |
| **4** | Customer trust |
| **5** | Capital efficiency |
| **6** | Realized-return potential |
| **7** | Delivery reliability |

If work does not materially improve one of these outcomes, it should not displace revenue work.

---

## ◈ Near-Term Flight Plan

1. Keep NFL and MLB running unattended.
2. Observe the next naturally qualifying BUY end to end.
3. Measure executable opportunity yield instead of raw scan volume.
4. Continue surgical removal of legacy dependencies only when current code no longer needs them.
5. Improve customer-facing value where it supports conversion, retention, or better decisions.
6. Introduce a visible Research / Challenger agent only after the revenue path remains stable and the role has measurable commercial value.

---

<div align="center">

## ◈ SWARM AXIS

**PARALLAX is not built to look active.  
It is built to be right often enough, fast enough, and disciplined enough to matter.**

`DISCOVER // SCORE // CERTIFY // PUBLISH // ALERT`

**Revenue first. Evidence always. Fail closed.**

</div>
