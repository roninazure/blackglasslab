# Swarm Edge deployment contract

This directory contains versioned deployment inputs. Nothing here is installed
or activated by the application branch.

## Render and install (future cutover)

Render `launchd/com.swarmedge.runner.plist.in` by replacing every `@NAME@`
placeholder with an absolute path from the release manifest. Validate the
rendered file with `plutil -lint`, then install it with `launchctl bootstrap`
and verify `launchctl print gui/$(id -u)/com.swarmedge.runner`. The cutover
procedure must stop the old job, verify its final PID, bootstrap the rendered
job, and confirm the release manifest before traffic is enabled.

The current checkout-relative deployment remains untouched until that
cutover. Do not copy runtime state into a release; use the configured absolute
runtime paths at migration time.

## Release manifest

Generate a deterministic manifest without exposing configuration values:

```sh
python scripts/deployment_manifest.py generate \
  --output deploy/manifest.json \
  --runtime-env .env.runtime \
  --plist deploy/launchd/com.swarmedge.runner.plist \
  --lock requirements.lock
```

The manifest records the Git SHA, Python version, and SHA-256 digests of the
dependency lock, runtime environment file, and launchd plist. `validate`
recomputes those values and fails on drift.
