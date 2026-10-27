"""Klipper Revive: low-overhead checkpointing and attended recovery.

No motion occurs without an explicit user-issued recovery command.
"""
import logging
import os
import re
import uuid

from .klipper_revive_collect import StateCollector
from .klipper_revive_store import CheckpointStore, CheckpointWriter
from .klipper_revive_recovery import RecoverySession, RecoveryError


class KlipperRevive:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.sample_interval = config.getfloat('sample_interval', 1.0, above=0.)
        # Accept the former config option for upgrades, but event-based
        # checkpoints are never gated by an elapsed-time interval.
        config.getfloat('checkpoint_interval', 15.0, above=0.)
        path = config.get('checkpoint_dir',
                          os.path.expanduser('~/printer_data/klipper_revive'))
        self.store = CheckpointStore(os.path.expanduser(path))
        self.writer = CheckpointWriter(self.store)
        self.collector = None
        self.timer = None
        self._sd_dispatch_original = None
        self._sd_dispatch_wrapper = None
        self._script_depth = 0
        self.enabled = True
        self.closed_job = False
        # None means no new job has been selected since the last Klippy boot.
        # In that state a stored interrupted-print checkpoint remains usable.
        self._session_id = None
        self._recovery_reset_in_progress = False
        self.recovery = RecoverySession(
            self,
            z_hop=config.getfloat('recovery_z_hop', 10., above=0.),
            xy_speed=config.getfloat('recovery_xy_speed', 60., above=0.),
            z_speed=config.getfloat('recovery_z_speed', 2., above=0.),
            max_xy_adjust=config.getfloat('max_xy_adjust', 5., above=0.),
            max_z_adjust=config.getfloat('max_z_adjust', 2., above=0.))
        self.gcode.register_command('KLIPPER_REVIVE_RECOVER', self.cmd_recover,
                                    desc='Begin attended heated XY/Z recovery')
        self.gcode.register_command('KLIPPER_REVIVE_XY_ADJUST', self.cmd_xy_adjust)
        self.gcode.register_command('KLIPPER_REVIVE_CONFIRM_XY', self.cmd_confirm_xy)
        self.gcode.register_command('KLIPPER_REVIVE_Z_ADJUST', self.cmd_z_adjust)
        self.gcode.register_command('KLIPPER_REVIVE_CONFIRM_Z', self.cmd_confirm_z)
        self.gcode.register_command('KLIPPER_REVIVE_RESUME', self.cmd_resume)
        self.gcode.register_command('KLIPPER_REVIVE_CANCEL', self.cmd_cancel)
        self.gcode.register_command('KLIPPER_REVIVE_STATUS', self.cmd_status,
                                    desc='Show Klipper Revive checkpoint status')
        self.gcode.register_command('KLIPPER_REVIVE_INSPECT', self.cmd_inspect,
                                    desc='Inspect last saved recovery checkpoint')
        self.gcode.register_command('KLIPPER_REVIVE_VALIDATE', self.cmd_validate,
                                    desc='Validate last saved checkpoint and G-code file')
        # VirtualSD emits this immediately when loading/selecting a file,
        # before the new print has processed any G-code.
        self.printer.register_event_handler('virtual_sdcard:reset_file',
                                            self._on_file_reset)
        self.printer.register_event_handler('klippy:ready', self._on_ready)
        self.printer.register_event_handler('klippy:shutdown', self._on_stop)
        self.printer.register_event_handler('klippy:disconnect', self._on_stop)

    def _on_ready(self):
        try:
            self.collector = StateCollector(self.printer)
            self._install_sd_completion_hook()
            self.timer = self.reactor.register_timer(self._sample, self.reactor.NOW)
        except Exception:
            logging.exception('Klipper Revive disabled: required Klipper objects unavailable')
            self.enabled = False

    def _on_stop(self):
        self.enabled = False
        self.recovery.live_adjustments_enabled = False
        self._remove_sd_completion_hook()
        if self.timer is not None:
            self.reactor.unregister_timer(self.timer)
            self.timer = None
        self.writer.close()

    def _begin_job(self, invalidate_previous=True):
        """Fence off earlier print sessions, even for the same G-code path."""
        self._session_id = uuid.uuid4().hex
        self.closed_job = False
        if invalidate_previous:
            # This tombstone is queued *before* any new job's snapshots.
            # Disk writes stay off the Klipper reactor thread.
            if not self.writer.submit({
                    'schema': 1, 'status': 'invalidated',
                    'job_id': self._session_id}):
                logging.error('Klipper Revive could not invalidate old checkpoint')
                self.enabled = False

    def _on_file_reset(self):
        if self._recovery_reset_in_progress:
            # Revive reselects the *same* interrupted job before M26/M24.
            # Do not tombstone the checkpoint during its internal file load.
            return
        # A manually selected new print must not inherit Revive live offsets.
        self.recovery.live_adjustments_enabled = False
        # SDCARD_PRINT_FILE, M23 and SDCARD_RESET_FILE reset VirtualSD.
        # The old checkpoint must not be exposed even while the background
        # writer is still persisting the invalidation tombstone.
        self._begin_job()

    def _recoverable_checkpoint(self):
        checkpoint = self.writer.last_saved
        if checkpoint is None or checkpoint.get('status') != 'printing':
            return None
        # A new job is in progress: any previous job's checkpoint is
        # unavailable, irrespective of file path, size or G-code position.
        if (self._session_id is not None
                and checkpoint.get('job_id') != self._session_id):
            return None
        return checkpoint

    # Only G-code that requests movement creates a new checkpoint.
    # G10/G11 are the standard firmware retraction (E axis) commands.
    # G28 homing also moves axes. Ignore comments, M104, etc.
    _MOVEMENT_COMMAND = re.compile(
        r'^(?:N[0-9]+\s+)?(?:G0|G1|G2|G3|G10|G11|G28)(?=\s|$)',
        re.IGNORECASE)

    @classmethod
    def _is_motion_line(cls, script):
        line = script.split(';', 1)[0].strip()
        return bool(cls._MOVEMENT_COMMAND.match(line))

    def _install_sd_completion_hook(self):
        """Capture the PRE-command position at every SD movement line.

        VirtualSD updates next_file_position before dispatch but file_position
        still points at the start of the current line. Capture before calling
        the original G-code handler, so a restart reprocesses that line.
        Nested scripts are never independent virtual-SD file boundaries.
        """
        if self._sd_dispatch_wrapper is not None:
            return
        original = self.gcode.run_script

        def run_script_with_checkpoint(script):
            top_level_sd_motion = (
                self._script_depth == 0 and self.enabled
                and self.collector is not None
                and self.collector.sd.cmd_from_sd
                and self._is_motion_line(script))
            if top_level_sd_motion:
                try:
                    self._capture_motion_start(self.reactor.monotonic())
                except Exception:
                    # A failed checkpoint must not alter G-code execution.
                    logging.exception('Klipper Revive motion checkpoint failed')
                    self.enabled = False
            self._script_depth += 1
            try:
                return original(script)
            finally:
                self._script_depth -= 1

        self._sd_dispatch_original = original
        self._sd_dispatch_wrapper = run_script_with_checkpoint
        self.gcode.run_script = run_script_with_checkpoint

    def _remove_sd_completion_hook(self):
        if self._sd_dispatch_wrapper is not None:
            if self.gcode.run_script is self._sd_dispatch_wrapper:
                self.gcode.run_script = self._sd_dispatch_original
            self._sd_dispatch_wrapper = None
            self._sd_dispatch_original = None

    def _capture_motion_start(self, eventtime):
        """Submit a checkpoint for each movement G-code before dispatch."""
        if not self.enabled or self.writer.last_error is not None:
            self.enabled = False
            return
        snapshot = self.collector.capture(eventtime, at_motion_start=True)
        if snapshot is None:
            return
        if self._session_id is None:
            # Some VirtualSD implementations do not emit reset_file.
            self._begin_job(invalidate_previous=self.writer.last_saved is not None)
        if not self.enabled:
            return
        snapshot['job_id'] = self._session_id
        previous = self._recoverable_checkpoint()
        # No interval gate. Only disregard duplicate/older byte positions.
        if (previous is None or snapshot['file']['path'] != previous['file']['path']
                or snapshot['file_position'] > previous['file_position']):
            if not self.writer.submit(snapshot):
                self.enabled = False

    def _sample(self, eventtime):
        if not self.enabled or self.writer.last_error is not None:
            self.enabled = False
            return self.reactor.NEVER
        try:
            state = self.collector.stats.state
            if state == 'printing':
                self.closed_job = False
            elif state in ('complete', 'cancelled'):
                self.recovery.live_adjustments_enabled = False
                if not self.closed_job:
                    self.writer.submit({'schema': 1, 'status': state,
                                        'job_id': self._session_id})
                    self.closed_job = True
            # Keep last good checkpoint on print error/shutdown.
        except Exception:
            logging.exception('Klipper Revive sampler disabled; printing unaffected')
            self.enabled = False
            return self.reactor.NEVER
        return eventtime + self.sample_interval

    def get_status(self, eventtime):
        checkpoint = self._recoverable_checkpoint()
        return {
            'enabled': self.enabled,
            'checkpoint_available': checkpoint is not None,
            'file_position': checkpoint.get('file_position', 0) if checkpoint else 0,
            'file': checkpoint.get('file', {}).get('path', '') if checkpoint else '',
            'sequence': self.writer.sequence,
            'storage_error': self.writer.last_error or '',
            'recovery_phase': self.recovery.phase,
            'recovery_offsets': tuple(self.recovery.offsets),
        }

    def cmd_status(self, gcmd):
        s = self.get_status(self.reactor.monotonic())
        gcmd.respond_info(
            'Klipper Revive: enabled=%s checkpoint=%s seq=%d pos=%d recovery=%s offsets=%s storage_error=%s'
            % (s['enabled'], s['checkpoint_available'], s['sequence'],
               s['file_position'], s['recovery_phase'], s['recovery_offsets'],
               s['storage_error'] or 'none'))

    def cmd_inspect(self, gcmd):
        snapshot = self._recoverable_checkpoint()
        if snapshot is None:
            gcmd.respond_info('Klipper Revive: no recoverable checkpoint')
            return
        gcmd.respond_info(
            'Klipper Revive checkpoint: file=%s byte=%s machine_xyz=%s '
            'extruder=%s heater_targets=%s mesh=%s (no motion performed)'
            % (snapshot['file']['path'], snapshot['file_position'],
               snapshot['machine_position'][:3], snapshot['extruder'],
               snapshot['heater_targets'], snapshot.get('mesh_profile', '')))

    def _recover_call(self, gcmd, action):
        try:
            action()
        except RecoveryError as exc:
            raise gcmd.error(str(exc))

    def cmd_recover(self, gcmd):
        self._recover_call(gcmd, self.recovery.prepare)
        gcmd.respond_info('Revive: at XY alignment, adjust or confirm XY.')

    def cmd_xy_adjust(self, gcmd):
        dx = gcmd.get_float('X', 0.)
        dy = gcmd.get_float('Y', 0.)
        self._recover_call(gcmd, lambda: self.recovery.adjust_xy(dx, dy))
        gcmd.respond_info('Revive XY offsets: %s' % self.recovery.offsets[:2])

    def cmd_confirm_xy(self, gcmd):
        self._recover_call(gcmd, self.recovery.confirm_xy)
        gcmd.respond_info('Revive: at Z alignment; adjust or confirm Z.')

    def cmd_z_adjust(self, gcmd):
        dz = gcmd.get_float('Z', 0.)
        self._recover_call(gcmd, lambda: self.recovery.adjust_z(dz))
        gcmd.respond_info('Revive Z offset: %.3f mm' % self.recovery.offsets[2])

    def cmd_confirm_z(self, gcmd):
        self._recover_call(gcmd, self.recovery.confirm_z)
        gcmd.respond_info('Revive: position confirmed. Check external filament/tool state.'
                          ' KLIPPER_REVIVE_RESUME EXTERNAL_STATE_OK=1 to continue.')

    def cmd_resume(self, gcmd):
        acknowledge = gcmd.get_int('EXTERNAL_STATE_OK', 0, minval=0, maxval=1)
        speed = gcmd.get_float('SPEED', 10., minval=1., maxval=200.)
        self._recover_call(gcmd, lambda: self.recovery.resume(
            acknowledge == 1, gcmd=gcmd, speed_percent=speed))
        gcmd.respond_info('Revive: resumed virtual SD print at %.1f%% speed.'
                          % speed)

    def cmd_cancel(self, gcmd):
        self.recovery.cancel()
        gcmd.respond_info('Revive: cancelled. RESTART Klipper before normal operation'
                          ' to clear the temporary Z reference.')

    def validate_checkpoint(self):
        """Intended for explicit, idle-time inspection only."""
        checkpoint = self._recoverable_checkpoint()
        if checkpoint is None:
            return False, 'No recoverable print checkpoint'
        path = checkpoint['file']['path']
        sd_dir = os.path.realpath(
            self.printer.lookup_object('virtual_sdcard').sdcard_dirname)
        resolved = os.path.realpath(path)
        if os.path.commonpath([sd_dir, resolved]) != sd_dir:
            return False, 'Checkpoint file is outside virtual SD directory'
        try:
            stat = os.stat(resolved)
        except OSError as exc:
            return False, 'Checkpoint file is unavailable: %s' % exc
        if stat.st_size != checkpoint['file']['size']:
            return False, 'G-code file size changed since checkpoint'
        mtime = checkpoint['file'].get('mtime_ns')
        if mtime is not None and stat.st_mtime_ns != mtime:
            return False, 'G-code file modification time changed'
        if not 0 <= checkpoint['file_position'] < stat.st_size:
            return False, 'Invalid G-code byte position'
        return True, 'Checkpoint and source file validated (no motion performed)'

    def cmd_validate(self, gcmd):
        if self.collector is not None and self.collector.stats.state in ('printing', 'paused'):
            raise gcmd.error('Validate only when printer is not printing or paused')
        ok, message = self.validate_checkpoint()
        gcmd.respond_info('Klipper Revive: %s' % message)
        if not ok:
            logging.warning('Klipper Revive validation: %s', message)


def load_config(config):
    return KlipperRevive(config)
