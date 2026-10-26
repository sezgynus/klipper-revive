"""Klipper Revive: low-overhead checkpoint monitor (experimental).

Only reads existing Klipper objects. No motion commands and no automatic
recovery actions exist in this milestone.
"""
import logging
import os

from .klipper_revive_collect import StateCollector
from .klipper_revive_store import CheckpointStore, CheckpointWriter


class KlipperRevive:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.sample_interval = config.getfloat('sample_interval', 1.0, above=0.)
        self.checkpoint_interval = config.getfloat('checkpoint_interval', 15.0,
                                                    above=0.)
        path = config.get('checkpoint_dir',
                          os.path.expanduser('~/printer_data/klipper_revive'))
        self.store = CheckpointStore(os.path.expanduser(path))
        self.writer = CheckpointWriter(self.store)
        self.collector = None
        self.timer = None
        self.next_checkpoint = 0.
        self.enabled = True
        self.closed_job = False
        self.gcode.register_command('KLIPPER_REVIVE_STATUS', self.cmd_status,
                                    desc='Show Klipper Revive checkpoint status')
        self.gcode.register_command('KLIPPER_REVIVE_INSPECT', self.cmd_inspect,
                                    desc='Inspect last saved recovery checkpoint')
        self.gcode.register_command('KLIPPER_REVIVE_VALIDATE', self.cmd_validate,
                                    desc='Validate last saved checkpoint and G-code file')
        self.printer.register_event_handler('klippy:ready', self._on_ready)
        self.printer.register_event_handler('klippy:shutdown', self._on_stop)
        self.printer.register_event_handler('klippy:disconnect', self._on_stop)

    def _on_ready(self):
        try:
            self.collector = StateCollector(self.printer)
            self.timer = self.reactor.register_timer(self._sample, self.reactor.NOW)
        except Exception:
            logging.exception('Klipper Revive disabled: required Klipper objects unavailable')
            self.enabled = False

    def _on_stop(self):
        self.enabled = False
        if self.timer is not None:
            self.reactor.unregister_timer(self.timer)
            self.timer = None
        self.writer.close()

    def _sample(self, eventtime):
        if not self.enabled or self.writer.last_error is not None:
            self.enabled = False
            return self.reactor.NEVER
        try:
            state = self.collector.stats.state
            if state == 'printing':
                self.closed_job = False
                if eventtime >= self.next_checkpoint:
                    snapshot = self.collector.capture(eventtime)
                    if snapshot is not None:
                        previous = self.writer.last_saved
                        if (previous is None
                                or snapshot['file_position'] != previous.get('file_position')
                                or snapshot['file'] != previous.get('file')
                                or previous.get('status') != 'printing'):
                            self.writer.submit(snapshot)
                        self.next_checkpoint = eventtime + self.checkpoint_interval
            elif state in ('complete', 'cancelled'):
                if not self.closed_job:
                    self.writer.submit({'schema': 1, 'status': state})
                    self.closed_job = True
            # On a print error/shutdown the last good checkpoint remains valid.
        except Exception:
            logging.exception('Klipper Revive sampler disabled; printing is unaffected')
            self.enabled = False
            return self.reactor.NEVER
        return eventtime + self.sample_interval

    def get_status(self, eventtime):
        checkpoint = self.writer.last_saved
        return {
            'enabled': self.enabled,
            'checkpoint_available': (checkpoint is not None
                                     and checkpoint.get('status') == 'printing'),
            'file_position': checkpoint.get('file_position', 0) if checkpoint else 0,
            'file': checkpoint.get('file', {}).get('path', '') if checkpoint else '',
            'sequence': self.writer.sequence,
            'storage_error': self.writer.last_error or '',
        }

    def cmd_status(self, gcmd):
        s = self.get_status(self.reactor.monotonic())
        gcmd.respond_info(
            'Klipper Revive: enabled=%s checkpoint=%s seq=%d pos=%d storage_error=%s'
            % (s['enabled'], s['checkpoint_available'], s['sequence'],
               s['file_position'], s['storage_error'] or 'none'))

    def cmd_inspect(self, gcmd):
        snapshot = self.writer.last_saved
        if not snapshot or snapshot.get('status') != 'printing':
            gcmd.respond_info('Klipper Revive: no recoverable checkpoint')
            return
        gcmd.respond_info(
            'Klipper Revive checkpoint: file=%s byte=%s machine_xyz=%s '
            'extruder=%s heater_targets=%s mesh=%s (no motion performed)'
            % (snapshot['file']['path'], snapshot['file_position'],
               snapshot['machine_position'][:3], snapshot['extruder'],
               snapshot['heater_targets'], snapshot.get('mesh_profile', '')))

    def validate_checkpoint(self):
        """Intended for explicit, idle-time inspection only."""
        checkpoint = self.writer.last_saved
        if not checkpoint or checkpoint.get('status') != 'printing':
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
        if not 0 < checkpoint['file_position'] < stat.st_size:
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
