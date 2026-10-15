<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="logo.png">
    <img src="logo.png" alt="TugBoat" height="250em">
  </picture>
</p>

# TugBoat - Management and monitoring solution for docker stacks

Manage docker compose stacks, backup stacks before updates and monitor stacks!

## Install

Requirements: 
  * Python 3.9+, 
  * Docker with the Compose plugin
  * cron.

The installer installs anything that is missing (Docker, Compose plugin, cron, Python) on apt, dnf and pacman systems, and TugBoat installs missing Docker or cron itself when it starts. Set `TUGBOAT_INSTALL_DEPS=0` for the installer, or `install_dependencies: false` in `TugBoat.conf`, to turn that off.

TugBoat and its `TugBoat.conf` are installed in the stacks folder. To set that folder (default `/container-data`) and the docker user (default: the user who ran `sudo`) yourself:

```sh
wget -qO- https://raw.githubusercontent.com/hen-io/TugBoat/main/install.sh | sudo env TUGBOAT_CONTAINER_PATH=/container-data TUGBOAT_DOCKER_USER=henrik sh
```

```sh
wget -qO install.sh https://raw.githubusercontent.com/hen-io/TugBoat/main/install.sh
sudo sh install.sh
```

```sh
wget -qO- https://raw.githubusercontent.com/hen-io/TugBoat/main/install.sh | sudo sh
```
## Use

| Command | What it does |
| --- | --- |
| `sudo tugboat` | Overview, then pick an action and stacks from a menu |
| `sudo tugboat --healthcheck` | Health and new image versions; writes the status file |
| `sudo tugboat --update web db` | Stop, back up, pull, start and health-check the stacks `web` and `db` |
| `sudo tugboat --auto --only-outdated` | Update only the stacks that have a new image, without questions |
| `sudo tugboat --start web` | Start a stack |
| `sudo tugboat --stop web` | Stop a stack |
| `sudo tugboat --restart web` | Stop and start a stack |
| `sudo tugboat --list-backups web` | List the backups of a stack |
| `sudo tugboat --rollback web` | Restore a backup of a stack (pick one from a list; the current state is backed up first). Add `--to NAME` to choose one, `--auto` for the newest, `--skip-backup` to skip the safety backup |
| `sudo tugboat --help` | All options |
| `sudo tugboat --check-update` | Check for new TugBoat update |
| `sudo tugboat --self-update` | Install new TugBoat update |
| `sudo tugboat --remove web` | Remove a stack's containers and networks (`docker compose down --remove-orphans`); files and volumes stay |
| `sudo tugboat --install` | Set up the health checks: the `tugboat-live` service, or the cron job (`healthcheck_mode`) |
| `sudo tugboat --uninstall` | Remove the `tugboat-live` service and the health check cron job |
| `sudo tugboat --watch` | Keep the status file up to date live and run the health checks (what the `tugboat-live` service runs) |
| `sudo tugboat --soft-reset` | Forget what TugBoat found by itself (image checks, icons, error-action records); settings, backups and history stay |
| `sudo tugboat --set-config live_interval 5` | Change one setting in `TugBoat.conf` (checked before it is saved; only the name shows the current value) |
| `sudo tugboat --run-fixes` | Run `.TugBoat/bin/fixes.sh` now |
| `sudo tugboat --stop --stack web` | Name stacks with `--stack` instead of after the action |
| `sudo tugboat --stop --all` | Run the action on all stacks (on its own: update all) |
| `sudo tugboat --auto` | No questions: update all stacks unless named |
| `sudo tugboat --update --all -j 2` | Handle this many stacks at the same time (`--parallel N`) |
| `sudo tugboat --update web --skip-backup` | Update without backing up the stack first |
| `sudo tugboat --healthcheck --no-error-actions` | Do not run the error actions from the stacks' `TugBoat.stack` files |
| `sudo tugboat --healthcheck --no-image-check` | Do not check the registries for new image versions |
| `sudo tugboat --healthcheck --check-images` | Check the registries now, even if `image_check_interval` has not passed |
| `sudo tugboat --update web -v` | Show the full command output live (`--verbose`) |
| `sudo tugboat --update web --dry-run` | Show what would happen without doing anything |
| `tugboat --version` | Show the TugBoat version |
## Status file

Every health check and action writes `tugboat.json` (by default in your container folder) with the health of every stack, its containers, whether newer images are available and the result of the last action.

## Configuration

Settings are in `TugBoat.conf` in the scripts root folder. Missing settings are added with their default, and when a new version changes a default, a setting that still has the old default is moved to the new one (settings you changed yourself are never touched).

| Setting | Default | Meaning |
| --- | --- | --- |
| `container_path` | `/container-data` | Folder that holds one sub-folder per stack |
| `docker_user` | (none) | Run docker commands as this user |
| `backup` / `backup_retention` | `true` / `15` | Back up a stack before updating, and how many backups to keep (`0` = keep all) |
| `backup_large_mb` / `backup_large_retention` | `250` / `5` | If a stack's newest backup is bigger than this many MB, keep only this many backups instead of `backup_retention` (`0` = off) |
| `ignore_folders` | (none) | Comma-separated stack folders to leave alone |
| `image_prune` | `all` | After an update, delete: `all` = every unused image (`docker image prune -a -f`, once after all stacks), `old` = only the stack's previous images, `off` = nothing |
| `image_check_interval` | `60` | Minutes between checks for new images |
| `auto_update` | `true` | Install new TugBoat versions automatically |
| `healthcheck_mode` | `live` | `live`: the `tugboat-live` systemd service (starts at boot) updates the status file as soon as a container changes and runs the health checks, no cron job; `cron`: a cron job runs the health checks (also used when there is no systemd) |
| `live_mode` / `live_interval` | `events` / `10` | Live mode: react to Docker events, or `interval`: check every `live_interval` seconds |
| `manage_cron` | `true` | Let TugBoat add, update and remove its cron job to match `healthcheck_mode` |
| `install_dependencies` | `true` | Install missing Docker, Compose plugin and cron (apt, dnf, pacman) |
| `healthcheck_interval` | `1` | Minutes between full health checks: image versions, icons, error actions (cron: 1-59, or whole hours: 60, 120 ...) |
| `update_check_seconds` | `1800` | Seconds between checks for a new TugBoat release (checked when TugBoat starts) |
| `registry_timeout` | `10` | Seconds to wait for an image registry when checking for new images |

## TugBoat.stack

Every stack folder gets a `TugBoat.stack` file. TugBoat adds any setting that is missing (and converts the old layout); values you set are kept. A file that cannot be read is left as it is and reported.

| Setting | Default | Meaning |
| --- | --- | --- |
| `stack.enabled` | `true` | `false` = TugBoat ignores this stack and drops it from the status file |
| `stack.priority` | `1` | Lower numbers start first and stop last; stacks with the same number run side by side |
| `stack.icons.stack` | (found) | Icon of the stack: an http(s) address, a file in the stack folder or an icon name |
| `stack.icons.containers.<name>` | (found) | Icon of one container, same values as above |
| `stack.on_error` | (none) | List of rules that run when the stack is not running or unhealthy |
| `on_error[].name` | (none) | Name of the rule, shown in the status file and webhooks |
| `on_error[].trigger_on` | `not_running, unhealthy` | When the rule runs |
| `on_error[].cooldown` | `1h` | Time before the rule may run again (`30s`, `10m`, `1h`) |
| `on_error[].actions` | (none) | Steps, run one at a time; stops as soon as the stack is healthy |
| `- restart:` | | Restart the stack; options `retries` (default `1`) and `retry_delay` |
| `- webhook: URL` | | POST the stack's state as JSON; the URL can use `{stack}` `{host}` `{health}` `{trigger}` `{rule}` `{summary}` `{error}` |
| `- delay: 5s` | | Wait |
| `- stack: restart other` | | `start`, `stop`, `restart`, `update` or `remove` another stack (or `all`) |
| `- script: ./fix.sh` | | Run a script in the stack folder (gets `TUGBOAT_STACK`, `TUGBOAT_HEALTH`, `TUGBOAT_PROBLEMS` ...) |
| `- reboot-host: true` | | Stop all stacks and reboot the host |
