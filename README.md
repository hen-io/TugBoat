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
wget -qO- https://raw.githubusercontent.com/hen-io/TugBoat/main/install.sh | sudo env TUGBOAT_CONTAINER_PATH=/path/to/stacks TUGBOAT_DOCKER_USER=dockeruser sh
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
## Status file

Every health check and action writes `tugboat.json` (by default in your container folder) with the health of every stack, its containers, whether newer images are available and the result of the last action.

## Configuration

Settings are in `TugBoat.conf` in the scripts root folder.

| Setting | Default | Meaning |
| --- | --- | --- |
| `container_path` | `/container-data` | Folder that holds one sub-folder per stack |
| `docker_user` | (none) | Run docker commands as this user |
| `backup` / `backup_retention` | `true` / `10` | Back up a stack before updating, and how many backups to keep (`0` = keep all) |
| `backup_large_mb` / `backup_large_retention` | `0` / `2` | If a stack's newest backup is bigger than this many MB, keep only this many backups instead of `backup_retention` (`0` = off) |
| `ignore_folders` | (none) | Comma-separated stack folders to leave alone |
| `image_check_interval` | `60` | Minutes between checks for new images |
| `auto_update` | `true` | Install new TugBoat versions automatically |
| `manage_cron` | `true` | Let TugBoat keep its cron job in order |
| `install_dependencies` | `true` | Install missing Docker, Compose plugin and cron (apt, dnf, pacman) |
| `CRON_EVERY_MINUTES` | `5` | How often the --healthcheck cron job runs  |
