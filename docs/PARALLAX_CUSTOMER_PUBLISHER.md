# PARALLAX customer publisher cutover

`scripts/parallax_customer_publisher.py` is the canonical NFL, CFB, and MLB
customer publisher. It consumes only the three `parallax.public.v1` files for
BUY decisions. The alert database is opened read-only and queried by the full
sport, normalized venue, market ID, and side key for missing display fields.
For NFL BUYs absent from that table, the sanitized title and slate supply a
matchup and scheduled start. A missing or ambiguous display identity, stale
source, count mismatch, dirty worktree, push race, or remote SHA mismatch fails
the run and records `FAILED` health.

The publisher takes one nonblocking lock and commits all three customer files
in one Git commit. The remote branch update makes them visible together. A
failed push is never retried with force; its local commit requires operator
inspection before another publication.

## Read-only check

From a release checkout, run:

```sh
/usr/bin/python3 scripts/parallax_customer_publisher.py --dry-run
```

This reads current feeds, the alert DB, and customer worktree status. It does
not fetch, write health, lock, commit, or push. It cannot attest the current
remote SHA when GitHub is unreachable. A live publish verifies the remote SHA
with `git ls-remote` after the push.

## Reviewed deployment procedure

The deployment helper is inert unless explicitly run with `--apply`. It needs
an immutable, clean checkout at the tested full SHA. Its preflight checks the
customer worktree, remote SHA, and current read-only dry run. It backs up all
four publisher plists, stops them, removes the three legacy plists, installs
one canonical plist pointing to the release checkout, starts it, and waits for
fresh healthy state with equal source/customer BUY counts. Any cutover error
restores the prior plists and previously loaded jobs. The backup directory is
printed after success.

```sh
sudo /usr/bin/python3 scripts/deploy_parallax_customer_publisher.py \
  --apply --release-dir /absolute/path/to/immutable/release \
  --sha FULL_40_CHARACTER_TESTED_COMMIT_SHA --user scottsteele
```

Manual rollback uses the printed backup path:

```sh
sudo /usr/bin/python3 scripts/deploy_parallax_customer_publisher.py \
  --rollback '/Users/scottsteele/Library/Application Support/SwarmEdge/backups/parallax-customer-cutover-YYYYMMDDTHHMMSSZ' \
  --user scottsteele
```

Rollback restores the prior publisher arrangement. Because that arrangement
contains competing writers, use it only to recover from a failed cutover and
resolve the original publication risk before leaving it unattended.

The sports scanner service `com.swarmedge.parallax-sports` is outside this
cutover and is never addressed by the helper.
