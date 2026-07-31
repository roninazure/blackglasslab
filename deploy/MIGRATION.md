# Swarm Edge runtime migration preflight

The target layout is:

```text
~/src/swarm-edge/
~/Library/Application Support/SwarmEdge/{bin,releases/<sha>,current,venvs/<sha>,config,state,run}/
~/Library/Logs/SwarmEdge/
~/Library/Application Support/SwarmEdgeBackups/
```

`migration_preflight.py provision` creates directories with mode `0700`.
Runtime configuration, SQLite backups, and copied databases are mode `0600`.
The preflight never installs or bootstraps launchd.

## Cutover commands (attended, future)

```sh
launchctl print gui/$(id -u)/com.swarmedge.runner
launchctl kill SIGTERM gui/$(id -u)/com.swarmedge.runner
lsof -nP -a -p <launchd-reported-pid> # must show no writers
cp .../rendered/com.swarmedge.runner.plist "$HOME/Library/LaunchAgents/com.swarmedge.runner.plist"
ln -sfn ".../releases/<sha>" "$HOME/Library/Application Support/SwarmEdge/current"
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.swarmedge.runner.plist"
launchctl kickstart -k gui/$(id -u)/com.swarmedge.runner
```

The commands above are documentation only in Gate 1; they were not executed.

## Rollback commands (attended, future)

```sh
launchctl kill SIGTERM gui/$(id -u)/com.swarmedge.runner
ln -sfn ".../releases/<previous-sha>" "$HOME/Library/Application Support/SwarmEdge/current"
cp .../previous/com.swarmedge.runner.plist "$HOME/Library/LaunchAgents/com.swarmedge.runner.plist"
cp .../backups/runs.sqlite.pre-cutover .../state/runs.sqlite
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.swarmedge.runner.plist"
launchctl kickstart -k gui/$(id -u)/com.swarmedge.runner
```

Rollback retains the old checkout, plist, release, and pre-cutover database.
