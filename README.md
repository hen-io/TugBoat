<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="banner-logo.png">
    <img src="banner-logo.png" alt="TugBoat" height="100%">
  </picture>
</p>

# TugBoat - easy management and monitoring for multi-stack Docker container setups

TugBoat manages a folder of Docker Compose stacks, one sub-folder per stack. It can update,
start, stop and health-check them, backs up a stack's folder before every update, and writes
the state of every stack to a JSON file that monitoring tools can read.

Requirements: Linux, Python 3.9+, Docker with the Compose plugin. No Python packages needed.

## Usage

```sh
sudo python3 TugBoat.py                      # overview, then pick an action and stacks
sudo python3 TugBoat.py --healthcheck        # health + new image versions, writes the status file
sudo python3 TugBoat.py --update web db      # stop -> backup -> pull -> start -> health check
sudo python3 TugBoat.py --auto               # update all stacks, no questions (cron)
sudo python3 TugBoat.py --auto --only-outdated   # ...but only stacks with a new image version
sudo python3 TugBoat.py --start web
sudo python3 TugBoat.py --stop --all
```

| Option | What it does |
| --- | --- |
| `--only-outdated` | With an update: skip stacks whose images are already up to date |
| `--skip-backup` | With an update: do not back up the stack folder first |
| `--no-image-check` | Do not ask the registries for new image versions |
| `--dry-run` | Show what would happen, change nothing, write nothing |
| `-v`, `--verbose` | Show the full output of every command |
| `--check-update`, `--self-update` | Check for / install a new TugBoat release |

Exit codes: `0` ok, `1` a stack failed or is unhealthy, `2` could not run (config, permissions,
another run in progress), `130` interrupted.

## Health check and image updates

`--healthcheck` reports each stack as `healthy`, `starting`, `unhealthy`, `stopped` or `unknown`.
It also compares the digest of every image in the compose file with the registry and reports
each image as one of:

| Status | Meaning |
| --- | --- |
| `up_to_date` | The local image is the one the registry has for that tag |
| `update_available` | The registry has a newer image, or a newer one is pulled but the containers still run the old one |
| `not_pulled` | The image exists in the registry but not locally |
| `pinned` | The image is pinned to a digest (`image@sha256:...`), so it never changes |
| `local` | Built from a Dockerfile or loaded by hand, nothing to compare with |
| `unknown` | Could not be checked; `detail` says why |

Only manifest digests are requested, so the check does not pull anything and does not count
against Docker Hub's pull limit. Private registries use the login of `docker_user`
(`docker login`). A new image never changes the exit code; only health problems do.

## Status file

Written to `status_file` after every action and health check:

```json
{
  "summary": { "stacks": 4, "healthy": 3, "unhealthy": 1, "updates_available": 1 },
  "stacks": {
    "web": {
      "health": "healthy",
      "summary": "1/1 running",
      "problems": [],
      "checked_at": "2026-10-01T20:39:37+02:00",
      "containers": [ { "name": "web-web-1", "service": "web", "state": "running", "...": "..." } ],
      "images": [
        {
          "image": "nginx:alpine",
          "services": ["web"],
          "status": "update_available",
          "local_digest": "sha256:...",
          "remote_digest": "sha256:...",
          "detail": ""
        }
      ],
      "updates_available": 1,
      "images_checked_at": "2026-10-01T20:39:38+02:00",
      "last_action": { "action": "update", "ok": true, "result": "updated", "...": "..." },
      "last_update": "2026-09-28T03:00:41+02:00",
      "last_backup": "/container-data/.backup/web/28_09_26_03_00_12"
    }
  },
  "tugboat_version": "0.2.0",
  "written_at": "2026-10-01T20:39:38+02:00"
}
```

## Configuration

Settings live in `TugBoat.conf` next to the script. Relative paths are relative to that file.

| Setting | Default | Meaning |
| --- | --- | --- |
| `container_path` | required | Folder that holds one sub-folder per stack |
| `docker_stack_up_cmd` | required | Command that pulls and starts a stack during an update |
| `docker_stack_down_cmd` | required | Command that stops a stack |
| `docker_stack_start_cmd` | `docker compose up -d` | Command for `--start` |
| `docker_stack_restore_cmd` | start command | Command that restarts the old version when a backup or update fails |
| `docker_user` | (current user) | Run docker commands as this user |
| `require_root` | `true` | Restart with sudo when not root |
| `ignore_folders` | | Comma-separated stack folders to leave alone |
| `backup` | `true` | Copy the stack folder before an update |
| `backup_path` | `<container_path>/.backup/$STACK-NAME` | Where backups go; must be outside the stack folder |
| `backup_retention` | `10` | Backups to keep per stack (`0` keeps all) |
| `status_file` | `<container_path>/tugboat.json` | Where the status file is written |
| `health_wait` | `60` | Seconds to wait for containers to become healthy after a start |
| `image_check` | `true` | Check for new image versions during a health check |
| `command_timeout` | `0` | Stop a command that runs longer than this many seconds (`0` = no limit) |
| `update_check` | `true` | Look for a new TugBoat release at start |
| `auto_update` | `true` | Install a new TugBoat release automatically |
