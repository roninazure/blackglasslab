# PARALLAX deployment contract

This directory contains versioned deployment inputs for the production PARALLAX sports runtime.

Nothing in this directory is activated merely by being present in a Git branch. Production cutovers are explicit, SHA-addressed, validated, and reversible.

## Production owner

`launchd/com.swarmedge.parallax-sports.plist.in` is the production owner for the unattended sports scheduler.

The scheduler entrypoint is:

```text
scripts/parallax_unattended.py
```

It owns the NFL, optional CFB, MLB, and prospective-reconciliation schedules. Each lane keeps its own business logic; the unattended scheduler owns only cadence, isolation, locking, health state, and child-process execution.

## Release layout

Production releases are immutable and named by full Git SHA:

```text
~/Library/Application Support/SwarmEdge/
  releases/<git-sha>/
  venvs/<git-sha>/
  state/
  logs/
```

Do not deploy directly from a mutable checkout.

## Package a release

Create a release from a committed SHA with the existing migration/release tooling:

```sh
python scripts/migration_preflight.py create-release \
  --source "$SOURCE" \
  --sha "$SHA" \
  --target-release "$RELEASE"
```

The release directory must remain immutable after packaging.

## Render the launchd plist

Render `launchd/com.swarmedge.parallax-sports.plist.in` with absolute paths for:

- release root
- SHA-matched venv Python
- runtime environment file
- PARALLAX state directory
- log directory
- release SHA

Validate the rendered plist before any cutover:

```sh
plutil -lint "$RENDERED_PLIST"
```

## Generate the deployment manifest

Bind the release, dependency lock, runtime environment, launchd plist, and scheduler entrypoint:

```sh
python scripts/deployment_manifest.py generate \
  --root "$RELEASE" \
  --runtime-env "$RUNTIME_ENV" \
  --plist "$RENDERED_PLIST" \
  --lock "$RELEASE/requirements.lock" \
  --entrypoint "$RELEASE/scripts/parallax_unattended.py" \
  --output "$RELEASE/deploy/manifest.json"
```

The manifest records the Git SHA, Python version, and hashes needed to detect deployment drift.

## Cutover rules

Before reloading `com.swarmedge.parallax-sports`:

1. verify the target release SHA
2. verify the SHA-matched venv exists
3. verify the rendered plist with `plutil -lint`
4. preserve the current plist as the immediate rollback
5. reload the launchd job
6. verify `state = running`
7. verify the running program path points at the target venv
8. verify `PARALLAX_RELEASE_SHA` matches the target SHA
9. verify natural unattended behavior without forcing provider scans or BUYs

Do not remove the previous known-good release until the new release is proven stable.

## Operational safety

- launchd is the process owner
- one sports lane failure must not prevent later lanes from running
- provider rate limits are stop conditions, not retry loops
- runtime state lives outside immutable release directories
- alert failures must not crash sports scans
- production changes must preserve rollback
