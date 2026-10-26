# Klipper Revive

**Klipper Revive** is a Klipper extension under development for recovering interrupted prints. This milestone provides the installer, a **read-only low-overhead checkpoint monitor**, and recovery checkpoint validation. It does **not** move the toolhead or resume interrupted prints yet.

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

1. Links three Python modules (`klipper_revive.py`, `klipper_revive_collect.py`, `klipper_revive_store.py`) into Klipper's `klippy/extras` directory (no copies).
2. Creates a managed `klipper_revive.cfg` alongside `printer.cfg` containing `[klipper_revive]`.
3. Adds `[include klipper_revive.cfg]` to `printer.cfg` if missing.
4. Creates a managed `klipper_revive_update.cfg` alongside `moonraker.conf`.
5. Adds `[include klipper_revive_update.cfg]` to `moonraker.conf` if missing, registering **klipper-revive** as a Moonraker `git_repo` updater (`main`, `dev`, `managed_services: klipper`).
6. Restarts Klipper and Moonraker **only when Moonraker confirms the printer is idle**. A printing/paused/unreachable printer is never restarted automatically.

No `SAVE_VARIABLE`, extra Python package, or dedicated service is installed. A single daemon writer thread is activated **only when there is a checkpoint to write**. A symlink stays valid when Moonraker updates repository contents. **Do not edit tracked files in the Git checkout**; Moonraker requires a clean repo to update.

The module offers `KLIPPER_REVIVE_STATUS`, `KLIPPER_REVIVE_INSPECT`, and `KLIPPER_REVIVE_VALIDATE`. Sampling uses a 1-second lightweight state timer, with checkpoint writes at most every 15 seconds by default when progress changes. A bounded two-slot checkpoint store keeps the last valid checkpoint with a SHA-256 digest and atomic replacement. All `stat`, serialization, and disk flush operations run in a worker thread. No toolhead motion or print recovery is attempted in this version.

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

### Checkpoint smoke test

After installation, check `printer.cfg` includes `[include klipper_revive.cfg]`, then enter this in the Klipper console:

```gcode
KLIPPER_REVIVE_STATUS
```

Run a **disposable, supervised test print** that lasts at least 30 seconds. During the print, query:

```gcode
KLIPPER_REVIVE_STATUS
KLIPPER_REVIVE_INSPECT
```

After 15–20 seconds of movement, `checkpoint=True` and `seq>=1` should be reported (exact timing depends on the first sample). The file and byte position should advance over later samples. Check the on-disk files in `~/printer_data/klipper_revive/checkpoint.[01].json`.

After stopping the test print **normally**, the checkpoint is invalidated (a completion/cancellation tombstone). To test preservation on a true interruption, use only a disposable test print: a Klipper RESTART/shutdown before normal completion should leave the last valid checkpoint on disk. On return, `KLIPPER_REVIVE_INSPECT` should show it, and `KLIPPER_REVIVE_VALIDATE` should verify its original G-code file. **Do not trigger a power outage, force an MCU shutdown, or rely on physical recovery during this milestone.**

Configuration options in `klipper_revive.cfg`:

```ini
[klipper_revive]
# sample_interval: 1.0
# checkpoint_interval: 15.0
# checkpoint_dir: ~/printer_data/klipper_revive
```

Read the live module status using Moonraker's `klipper_revive` status object as well. The saved file path and modified time are validated on explicit request; inspection does not move any motors or change heaters.

### Updating

Use **Machine > Update Manager > klipper-revive** in Mainsail/Fluidd. Moonraker updates the checkout and restarts the matched Klipper service after an update. The integration targets the `main` branch on the `dev` channel. Before the first semantic version tag, some UI clients may not show version/update controls.

The installer **must be run once** to create local symlinks/config entries; Moonraker's deprecated `install_script` update-manager property does not re-run extension installers on updates.

### Uninstall

```bash
bash ~/klipper-revive/uninstall.sh
```

Accepts the same path and `--no-restart` flags. Removes only its owned symlink, managed snippets, and include lines, while leaving the repository and unrelated user configurations untouched. Restart Klipper/Moonraker when idle if the script skips the restart.

## Safety and development

This milestone **records checkpoints but does not resume prints**. Do not rely on it to protect prints yet. Live recovery will be implemented separately with human confirmation for XY/Z alignment.

Local checks:

```bash
bash -n install.sh uninstall.sh
python3 -m py_compile klipper_extras/*.py scripts/setup.py
python3 -m unittest discover -s tests -v
```

Moonraker update-manager reference: https://moonraker.readthedocs.io/en/latest/configuration/#git-repo-configuration
