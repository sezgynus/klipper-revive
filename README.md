# Klipper Revive

**Klipper Revive** is a Klipper extension under development for recovering interrupted prints. This milestone provides automatic installation, low-overhead checkpointing, and **experimental, attended recovery** on Cartesian and CoreXY printers. A user must explicitly initiate and confirm every alignment stage. Never leave a recovery unattended.

## Install (standard MainsailOS / Fluidd / KIAUH layout)

Run as your normal printer user over SSH (not root):

```bash
git clone https://github.com/sezgynus/klipper-revive.git ~/klipper-revive
bash ~/klipper-revive/install.sh
```

Or use the one-line bootstrap, which clones the repository automatically:

```bash
curl -fsSL https://raw.githubusercontent.com/sezgynus/klipper-revive/master/install.sh | bash
```

The installer:

1. Links four Python modules (`klipper_revive.py`, `klipper_revive_collect.py`, `klipper_revive_store.py`, `klipper_revive_recovery.py`) into Klipper's `klippy/extras` directory (no copies).
2. Creates a managed `klipper_revive.cfg` alongside `printer.cfg` containing `[klipper_revive]`.
3. Adds `[include klipper_revive.cfg]` to `printer.cfg` **before Klipper's auto-generated `SAVE_CONFIG` block**. Re-running the installer repairs older installations that incorrectly placed the include below the block, without deleting stored probe calibration values.
4. Creates a managed `klipper_revive_update.cfg` alongside `moonraker.conf`.
5. Adds `[include klipper_revive_update.cfg]` to `moonraker.conf` if missing, registering **klipper-revive** as a Moonraker `git_repo` updater (`master`, `dev`, `managed_services: klipper`).
6. Restarts Klipper and Moonraker **only when Moonraker confirms the printer is idle** (or Klipper reports a startup configuration error). If `sudo` requires authentication, the installer prompts in the active SSH terminal, including when invoked through `curl | bash`, and rechecks printer state after the password prompt. A printing/paused/unreachable printer is never restarted automatically.

No `SAVE_VARIABLE`, extra Python package, or dedicated service is installed. A single daemon writer thread is activated **only when there is a checkpoint to write**. A symlink stays valid when Moonraker updates repository contents. **Do not edit tracked files in the Git checkout**; Moonraker requires a clean repo to update.

The module offers `KLIPPER_REVIVE_STATUS`, `KLIPPER_REVIVE_INSPECT`, `KLIPPER_REVIVE_VALIDATE` and the explicit recovery commands described below. Checkpoints are sampled at completed virtual SD G-code line boundaries when 15 seconds have elapsed, with a 1-second timer as a fallback for idle gaps. The line-boundary observer wraps Klipper's `gcode.run_script` at startup and restores it on shutdown; it never changes any command's outcome or motor state. This avoids sampling only in the middle of a command while `virtual_sdcard.cmd_from_sd` is set. A bounded two-slot checkpoint store keeps the last valid checkpoint with a SHA-256 digest and atomic replacement. All `stat`, serialization, and disk flush operations run in a worker thread. Normal printing never causes recovery motion. Physical recovery movement occurs only when the user sends recovery commands.

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

The installer and uninstaller use the same three-stage output:
**checking paths → updating managed files → applying services**.
When Moonraker confirms the printer is idle, plain `sudo -v` requests a
password **only when needed**, using sudo's normal terminal prompt, followed
by service restarts. This behaves like the Petrel installer and also works
with `curl | bash` in a regular SSH session with a controlling terminal.
The scripts never open `/dev/tty` themselves, read a password from piped
stdin, or suppress an ordinary sudo password request with `sudo -n`.

When the printer is `printing`, `paused`, unavailable, or in a shutdown
state, service restarts are postponed without a sudo prompt. In a session
where sudo cannot authenticate, installation reports a clear failure rather
than silently pretending the services were restarted. Existing files remain
installed, and the command can be run again once authentication is possible.
The `--no-restart` option skips authorization and all restarts; re-running
install/uninstall retries the safe restart even when files are unchanged.
A repeat uninstallation also restarts services to unload the old Klipper
module when the printer is idle.

The normal installer is idempotent. It refuses to replace existing foreign `klipper_revive.py` files or unowned configuration snippets. It preserves one-time backups when changing existing main configuration files (`.klipper-revive.bak`).

### Fix a probe `z_offset` error after an older installation

An earlier installer incorrectly appended `[include klipper_revive.cfg]`
**below** the `#*# <--- SAVE_CONFIG --->` header in `printer.cfg`.
Klipper rejects the autosave block when ordinary configuration lines appear
there; this can make a saved `[probe] z_offset` seem to disappear.

In Mainsail/Fluidd's configuration editor, move the
`[include klipper_revive.cfg]` line **above** the `#*# SAVE_CONFIG`
header. Do **not** change or remove any `#*#` calibration lines.
Then issue `RESTART`.

Alternatively, update your existing checkout and run the installer again:

```bash
git -C ~/klipper-revive pull --ff-only
bash ~/klipper-revive/install.sh --no-restart
```

The installer will relocate the misplaced line safely; then `RESTART`
Klipper from the UI. If a `z_offset` error remains, inspect the bottom of
`printer.cfg` and its backup `printer.cfg.klipper-revive.bak` before any
probe recalibration. Do not invent a `z_offset` value.

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

After 15–20 seconds of movement, `checkpoint=True` and `seq>=1` should be reported (exact timing depends on the first sample). The file and byte position should advance over later samples. Compare `KLIPPER_REVIVE_STATUS` `pos` with Klipper's own `M27` SD byte position at least twice 30 seconds apart: the Revive checkpoint should advance. A nonzero lag is normal due to the 15-second interval and queued moves, but a position frozen across minutes while `M27` advances indicates failure and must be fixed **before any recovery experiment**. Check the on-disk files in `~/printer_data/klipper_revive/checkpoint.[01].json`.

**New-print isolation:** selecting/resetting a virtual SD print starts a new
checkpoint session, even if the G-code filename has not changed. An older
checkpoint is hidden from STATUS, INSPECT, VALIDATE and attended recovery as
soon as the new print is selected, before the first new 15-second checkpoint
is taken. The former session is invalidated in background storage before
the new session's checkpoints are queued. A genuine interruption followed by
a Klipper restart does not itself invalidate the saved checkpoint; it remains
available until a *new* file-selection/reset event. As with every asynchronous
filesystem write, a power loss during the invalidation write may leave an
older on-disk record; do not treat this milestone as crash-proof across
immediate repeated power failures.

After stopping the test print **normally**, the checkpoint is invalidated (a completion/cancellation tombstone). To test preservation on a true interruption, use only a disposable test print: a Klipper RESTART/shutdown before normal completion should leave the last valid checkpoint on disk. On return, `KLIPPER_REVIVE_INSPECT` should show it, and `KLIPPER_REVIVE_VALIDATE` should verify its original G-code file. **Do not trigger an uncontrolled power outage or deliberate MCU failure for testing.** Use a disposable print and intentional software restart only after confirming this test will not damage hardware.

Configuration options in `klipper_revive.cfg`:

```ini
[klipper_revive]
# sample_interval: 1.0
# checkpoint_interval: 15.0
# checkpoint_dir: ~/printer_data/klipper_revive
```

Read the live module status using Moonraker's `klipper_revive` status object as well. The saved file path and modified time are validated on explicit request; inspection does not move any motors or change heaters.

### Live tuning saved in each checkpoint

When Klipper exposes the corresponding objects, each **new** checkpoint also
captures the dynamically adjusted settings below (rather than only their
configuration defaults):

| UI values | Klipper status source | Recovery command |
| --- | --- | --- |
| Pressure Advance, Smooth Time | Active `extruder` (`pressure_advance`, `smooth_time`) | `SET_PRESSURE_ADVANCE EXTRUDER=... ADVANCE=... SMOOTH_TIME=...` |
| Retraction length/speed, unretract extra length/speed | Optional `firmware_retraction` | `SET_RETRACTION RETRACT_LENGTH=... RETRACT_SPEED=... UNRETRACT_EXTRA_LENGTH=... UNRETRACT_SPEED=...` |
| Max velocity, max acceleration, square corner velocity, minimum cruise ratio | `toolhead` | `SET_VELOCITY_LIMIT VELOCITY=... ACCEL=... SQUARE_CORNER_VELOCITY=... MINIMUM_CRUISE_RATIO=...` |

The `minimum_cruise_ratio` value is stored as a fraction: the UI's **50%**
corresponds to **0.5** in the checkpoint and recovery command. These values
are read on every *scheduled checkpoint* and restored after the saved SD byte
position is selected, **before** issuing `M24`. Recording still uses the
15-second checkpoint interval; this change does not implement a checkpoint for
every G-code line or guarantee exact physical-motion synchronization.

Firmware retraction is optional; it is not saved when the module is absent.
When a checkpoint contains this setting but the module is missing at recovery,
Revive rejects recovery before heating/motion rather than silently ignoring it.
Older checkpoints lacking these fields remain loadable and do not overwrite
current tuning. The firmware retraction module's transient `is_retracted`
flag is not exposed in its public status and is **not reconstructed**;
externally controlled tool/filament state still requires operator verification.


### Experimental recovery wizard (supervised test only)

Recovery is **opt-in**: it never begins after boot by itself. After a failed print, ensure the hotend is in a safe position and axes can move. The saved machine Z is trusted *without Z homing*; this is valid only if Z has not mechanically shifted since the last checkpoint. If Z motors lost steps or the gantry moved, **do not start recovery**.

1. Inspect the checkpoint and verify that its file matches the actual print:

   ```gcode
   KLIPPER_REVIVE_STATUS
   KLIPPER_REVIVE_INSPECT
   KLIPPER_REVIVE_VALIDATE
   ```

2. With an operator watching, issue `KLIPPER_REVIVE_RECOVER`. It waits for saved bed/hotend temperatures, assigns the saved machine Z **without physical movement**, lifts Z by the configured 10 mm, homes XY, then moves to the saved XY point at the lifted height. There is no automatic resume.
3. With the nozzle still 10 mm above the printed surface, adjust and approve XY:

   ```gcode
   KLIPPER_REVIVE_XY_ADJUST X=0.1
   KLIPPER_REVIVE_XY_ADJUST Y=-0.1
   KLIPPER_REVIVE_CONFIRM_XY
   ```

4. `CONFIRM_XY` makes a slow Z approach to the checkpoint machine height (default 2 mm/s). If necessary make small Z adjustments, then approve Z:

   ```gcode
   KLIPPER_REVIVE_Z_ADJUST Z=0.05
   KLIPPER_REVIVE_CONFIRM_Z
   ```

5. **Verify the externally selected filament/tool and any third-party plugin state yourself**. Third-party internal state cannot be reconstructed automatically. Confirm only when safe:

   ```gcode
   KLIPPER_REVIVE_RESUME EXTERNAL_STATE_OK=1
   ```

   Revive now resumes at **10% feedrate** by default using `M220 S10`,
   regardless of the previous checkpoint's speed override. Override it
   explicitly with `SPEED` (percentage, 1–200):

   ```gcode
   KLIPPER_REVIVE_RESUME EXTERNAL_STATE_OK=1 SPEED=25
   ```

   This is a feedrate percentage, **not** a change to the stored
   `max_velocity` or `max_accel`. The selected percentage remains active
   until the print or operator changes it (for example `M220 S100` or the
   Mainsail speed slider). There is no automatic return to 100%.

This restores standard captured coordinate/extrusion modes, speeds, bed mesh **only if a named persisted profile is available**, regular/generic fan speeds, saved active extruder and bed/nozzle targets. It selects the same G-code file (including a file in a virtual SD subdirectory) without starting the print, seeks to the recorded byte position, and only then starts virtual SD printing. Klipper's `M23` command cannot search subdirectories; recovery uses the same subdirectory-aware file loader as `SDCARD_PRINT_FILE` without its automatic start. This internal file reset is not treated as a *new* print session, so it does not invalidate the very checkpoint being recovered. Any saved dynamic motion limits, pressure advance, and firmware retraction parameters are validated and restored **before** the first resumed SD G-code command. The next queued G-code may repeat part of an already printed segment.

### Live XY/Z adjustment after recovery

The same Revive alignment commands remain usable after
`KLIPPER_REVIVE_RESUME`, **only while the Revive-resumed virtual SD print is
actively printing**:

```gcode
KLIPPER_REVIVE_XY_ADJUST X=0.1 Y=-0.1
KLIPPER_REVIVE_Z_ADJUST Z=0.05
```

During XY/Z alignment, these commands still use the existing manual guided
moves. During a running recovered print they use Klipper's
`SET_GCODE_OFFSET X_ADJUST=... Y_ADJUST=... MOVE=1` and
`SET_GCODE_OFFSET Z_ADJUST=... MOVE=1` instead, with bounded move speeds.
This updates the G-code coordinate origin as well as the nozzle position;
there is no independent `toolhead.manual_move` in the print queue. Deltas
remain **incremental** and accumulate with offsets set in the earlier
alignment wizard. The configured `max_xy_adjust` (default ±5 mm per axis)
and `max_z_adjust` (default ±2 mm) bounds still apply to the **total**
Revive offset, including pre-resume changes. The live commands reject
non-printing, paused, cancelled, and unrelated new print sessions.
An unsuccessful live offset move disables subsequent live Revive offsets
rather than risk losing cumulative-offset accounting.

These are physical changes performed during printing. Apply small steps
while supervising the print; moving the nozzle into previously printed
material can damage the part or toolhead. Any new checkpoint also captures
the Klipper-adjusted logical origin. Offsets applied through non-Revive
macros or UI controls are not included in the Revive session's cumulative
offset counter.

At any alignment stage `KLIPPER_REVIVE_CANCEL` stops the wizard **without movement**. Since Klipper's Z reference was manually assigned, restart Klipper before normal printing after cancellation. If the actual nozzle seems lower than expected, do not confirm or jog downward.

Default recovery config (add options to `klipper_revive.cfg`):

```ini
[klipper_revive]
# recovery_z_hop: 10
# recovery_xy_speed: 60
# recovery_z_speed: 2
# max_xy_adjust: 5
# max_z_adjust: 2
```

**Limitations:** Cartesian/CoreXY only, no automatic XY/Z collision detection, no verification of hidden states owned by third-party MMU/toolchanger plugins, and no automatic reconstruction of temporary adaptive meshes that have no persisted profile. Printer-specific `G28` macros and homing overrides may move additional axes; check their behavior before using this experimental wizard.

### Updating

Use **Machine > Update Manager > klipper-revive** in Mainsail/Fluidd. Moonraker updates the checkout and restarts the matched Klipper service after an update. The integration targets the `master` branch on the `dev` channel. Before the first semantic version tag, some UI clients may not show version/update controls.

The installer **must be run once** to create local symlinks/config entries; Moonraker's deprecated `install_script` update-manager property does not re-run extension installers on updates.

### Uninstall

```bash
bash ~/klipper-revive/uninstall.sh
```

Accepts the same path and `--no-restart` flags. Removes only its owned symlinks, managed snippets, and include lines, leaving the repository, checkpoint files, and unrelated user configurations untouched.

**RAM is not freed when Python files or symlinks are removed.** Klipper has already imported the module into its *running* Python process. The uninstaller now attempts a **full systemd service process restart** for Klipper and Moonraker once Moonraker confirms the printer is idle. A repeat `uninstall.sh` also retries the restart even if there are no files left to delete, fixing older versions that silently skipped restart after removing their files.

When Klipper has a startup/configuration `error`, Moonraker cannot query `print_stats`; the uninstaller treats that startup error as not printing and restarts safely. During `printing` or `paused`, or when Klipper is `shutdown` from an emergency stop, `startup`, `disconnected`, or Moonraker is unavailable, **automatic restarts are forbidden**. The script prints the manual restart commands; run them only after confirming no print is active. Re-running uninstall after the printer becomes idle is supported:

```bash
bash ~/klipper-revive/uninstall.sh
```

If the old uninstaller already removed everything and memory is still high, once you have confirmed there is no running or paused print:

```bash
sudo systemctl restart klipper
sudo systemctl restart moonraker
```

Klipper `RESTART` / `FIRMWARE_RESTART` G-code commands **do not reload host Python source**; use a **service** restart to ensure no old module remains resident. If service restart fails or Moonraker cannot confirm idle status, uninstall will explicitly report the pending restart rather than claiming RAM was released.

To compare actual private/shared RAM instead of virtual address-space size, sample the process *after* a normal idle start:

```bash
pid=$(systemctl show -p MainPID --value klipper)
sudo grep -E '^(Rss|Pss|Private_Clean|Private_Dirty):' "/proc/$pid/smaps_rollup"
```

Compare equivalent Klipper idle states before/after installation and after removal. Checkpoint files in `~/printer_data/klipper_revive/` are persistent **disk data**, not resident RAM, and are deliberately preserved.


## Safety and development

**Early experimental version — not yet hardware-validated.** A recovery may damage a print or toolhead if physical Z changed during power loss, the saved motion state is ahead of actual movement, or custom homing/macros act differently. Test only on supervised, disposable prints at low speeds; do not depend on this to save valuable prints.

Local checks:

```bash
bash -n install.sh uninstall.sh
python3 -m py_compile klipper_extras/*.py scripts/setup.py
python3 -m unittest discover -s tests -v
```

Moonraker update-manager reference: https://moonraker.readthedocs.io/en/latest/configuration/#git-repo-configuration
