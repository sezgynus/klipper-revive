# Klipper Revive

**Klipper Revive** is a Klipper extension under development for recovering interrupted prints. This first milestone provides the **installer and updater integration only**. It does not yet create checkpoints, move the toolhead, or resume prints.

## Install (standard MainsailOS / Fluidd / KIAUH layout)

Run as your normal printer user over SSH (not root):

```bash
git clone https://github.com/sezgynus/klipper-revive.git ~/klipper-revive
bash ~/klipper-revive/install.sh
```

Or use the one-line bootstrap, which clones the repository automatically:

```bash
curl -fsSL https://raw.githubusercontent.com/sezgynus/klipper-revive/main/install.sh | bash
```

The installer:

1. Links `~/klipper-revive/klipper_extras/klipper_revive.py` into `~/klipper/klippy/extras/klipper_revive.py` (no copy).
2. Creates a managed `klipper_revive.cfg` alongside `printer.cfg` containing `[klipper_revive]`.
3. Adds `[include klipper_revive.cfg]` to `printer.cfg` if missing.
4. Creates a managed `klipper_revive_update.cfg` alongside `moonraker.conf`.
5. Adds `[include klipper_revive_update.cfg]` to `moonraker.conf` if missing, registering **klipper-revive** as a Moonraker `git_repo` updater (`main`, `dev`, `managed_services: klipper`).
6. Restarts Klipper and Moonraker **only when Moonraker confirms the printer is idle**. A printing/paused/unreachable printer is never restarted automatically.

No `SAVE_VARIABLE`, extra Python package, dedicated service, or permanent background process is installed. A symlink stays valid when Moonraker updates repository contents. **Do not edit tracked files in the Git checkout**; Moonraker requires a clean repo to update.

The Klipper module currently implements only `KLIPPER_REVIVE_STATUS`, reporting that the project is a scaffold. It has no timers, checkpoint writes, or motion changes.

### Custom installations

For nonstandard paths or multiple printers, run:

```bash
bash ~/klipper-revive/install.sh \
  --klipper-dir /path/to/klipper \
  --printer-data-dir /path/to/printer_data \
  --klipper-service klipper \
  --moonraker-service moonraker \
  --moonraker-url http://127.0.0.1:7125
```

Use `--printer-config /path/to/printer.cfg` and `--moonraker-config /path/to/moonraker.conf` for independent overrides. `--no-restart` installs without restarting services. If the installer skips a restart, restart **Klipper and Moonraker only when the printer is idle**.

The normal installer is idempotent. It refuses to replace existing foreign `klipper_revive.py` files or unowned configuration snippets. It preserves one-time backups when changing existing main configuration files (`.klipper-revive.bak`).

### Updating

Use **Machine > Update Manager > klipper-revive** in Mainsail/Fluidd. Moonraker updates the checkout and restarts the matched Klipper service after an update. The integration targets the `main` branch on the `dev` channel. Before the first semantic version tag, some UI clients may not show version/update controls.

The installer **must be run once** to create local symlinks/config entries; Moonraker's deprecated `install_script` update-manager property does not re-run extension installers on updates.

### Uninstall

```bash
bash ~/klipper-revive/uninstall.sh
```

Accepts the same path and `--no-restart` flags. Removes only its owned symlink, managed snippets, and include lines, while leaving the repository and unrelated user configurations untouched. Restart Klipper/Moonraker when idle if the script skips the restart.

## Safety and development

This milestone is **not a working recovery feature**. Do not rely on it to protect prints yet. The future recovery engine will use independent checkpoint storage, require user confirmation for XY/Z alignment, and prioritize negligible overhead during normal printing.

Local checks:

```bash
bash -n install.sh uninstall.sh
python3 -m unittest discover -s tests -v
```

Moonraker update-manager reference: https://moonraker.readthedocs.io/en/latest/configuration/#git-repo-configuration
