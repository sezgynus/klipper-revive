"""Integration smoke test using Klipper-compatible fake printer objects."""
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import unittest

from klipper_extras.klipper_revive import KlipperRevive
from klipper_extras.klipper_revive_store import CheckpointStore


class Reactor:
    NOW = 0.
    NEVER = float('inf')

    def monotonic(self):
        return 0.

    def register_timer(self, callback, waketime):
        self.callback = callback
        return callback

    def unregister_timer(self, timer):
        pass


class Gcode:
    def __init__(self):
        self.commands = {}

    def register_command(self, name, callback, desc=None):
        self.commands[name] = callback

    def run_script(self, script):
        if getattr(self, 'run_callback', None) is not None:
            return self.run_callback(script)


class Obj:
    def __init__(self, **values):
        self.values = values

    def get_status(self, eventtime):
        return self.values


class SD:
    cmd_from_sd = False
    file_position = 100
    next_file_position = 100
    file_size = 1000

    def __init__(self, file):
        self.file = str(file)
        self.file_size = file.stat().st_size
        self.sdcard_dirname = str(file.parent)

    def file_path(self):
        return self.file


class Head:
    def get_position(self):
        return [11., 22., 4., 7.]

    def get_extruder(self):
        return SimpleNamespace(get_name=lambda: 'extruder')


class Move:
    def get_status(self, eventtime):
        position = SimpleNamespace(x=11., y=22., z=4., e=7.)
        return {
            'position': position, 'gcode_position': position,
            'homing_origin': position,
            'absolute_coordinates': True, 'absolute_extrude': True,
            'speed_factor': 1., 'extrude_factor': 1., 'speed': 1200.
        }


class Printer:
    def __init__(self, file):
        self.reactor = Reactor()
        self.gcode = Gcode()
        self.callbacks = {}
        self.stats = SimpleNamespace(state='printing')
        self.objects = {
            'gcode': self.gcode,
            'virtual_sdcard': SD(file),
            'gcode_move': Move(),
            'toolhead': Head(),
            'print_stats': self.stats,
            'extruder': Obj(target=212.0),
            'heater_bed': Obj(target=63.0),
        }

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def lookup_objects(self):
        return list(self.objects.items())

    def register_event_handler(self, name, callback):
        self.callbacks[name] = callback


class Config:
    def __init__(self, printer, directory):
        self.printer = printer
        self.directory = str(directory)

    def get_printer(self):
        return self.printer

    def get(self, key, default):
        return self.directory if key == 'checkpoint_dir' else default

    def getfloat(self, key, default, **kwargs):
        return default


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        file = root / 'cube.gcode'
        file.write_bytes(b'G1 X1 Y2 E3\n' * 100)
        self.printer = Printer(file)
        self.directory = root / 'state'
        self.module = KlipperRevive(Config(self.printer, self.directory))
        self.printer.callbacks['klippy:ready']()

    def wait_until(self, predicate):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.01)
        self.fail('checkpoint state did not reach expected value')

    def test_same_file_new_print_hides_previous_checkpoint_immediately(self):
        # M27 for a new print can be at byte 20 while an old same-file
        # checkpoint is at byte 100. That old state must never be offered.
        timer = self.printer.reactor.callback
        timer(1.)
        self.wait_for(1)
        previous = self.module._recoverable_checkpoint()
        self.assertEqual(previous['file_position'], 100)

        sd = self.printer.objects['virtual_sdcard']
        self.printer.stats.state = 'standby'
        sd.file_position = 0
        self.printer.callbacks['virtual_sdcard:reset_file']()
        self.assertIsNone(self.module._recoverable_checkpoint())
        status = self.module.get_status(2.)
        self.assertFalse(status['checkpoint_available'])
        self.assertEqual(status['file_position'], 0)
        self.assertEqual(self.module.validate_checkpoint()[0], False)
        messages = []
        self.module.cmd_inspect(SimpleNamespace(respond_info=messages.append))
        self.assertEqual(messages, ['Klipper Revive: no recoverable checkpoint'])

        # Invalidation is durable before the next checkpoint is available.
        self.wait_until(lambda: CheckpointStore(self.directory).load()[1]
                        .get('status') == 'invalidated')
        sd.file_position = 20
        self.printer.stats.state = 'printing'
        timer(3.)
        self.wait_until(lambda: self.module._recoverable_checkpoint() is not None)
        current = self.module._recoverable_checkpoint()
        self.assertEqual(current['file_position'], 20)
        self.assertNotEqual(previous['job_id'], current['job_id'])
        self.assertEqual(self.module.get_status(3.)['file_position'], 20)
        self.printer.callbacks['klippy:shutdown']()

    def test_interrupted_checkpoint_survives_restart_until_next_job(self):
        # A Klippy restart by itself is not a fresh print. A previously
        # interrupted checkpoint remains available until file selection.
        timer = self.printer.reactor.callback
        timer(1.)
        self.wait_for(1)
        old = self.module._recoverable_checkpoint()
        self.printer.callbacks['klippy:shutdown']()
        self.module.writer._thread.join(3)

        self.printer.stats.state = 'standby'
        replacement = KlipperRevive(Config(self.printer, self.directory))
        self.module = replacement
        self.printer.callbacks['klippy:ready']()
        self.assertEqual(replacement._recoverable_checkpoint(), old)
        self.assertTrue(replacement.get_status(0)['checkpoint_available'])

        # Same G-code selected for printing again, without completing
        # a recovery session: it is an entirely new print.
        self.printer.callbacks['virtual_sdcard:reset_file']()
        self.assertFalse(replacement.get_status(0)['checkpoint_available'])
        self.assertFalse(replacement.validate_checkpoint()[0])
        self.wait_until(lambda: CheckpointStore(self.directory).load()[1]
                        .get('status') == 'invalidated')
        self.printer.callbacks['klippy:shutdown']()

    def test_recovery_internal_reset_does_not_invalidate_interrupted_job(self):
        timer = self.printer.reactor.callback
        timer(1.)
        self.wait_for(1)
        previous = self.module._recoverable_checkpoint()
        self.assertIsNotNone(previous)

        # Internal file re-selection during Revive recovery must not be
        # mistaken for the start of a new user-requested print.
        self.module._recovery_reset_in_progress = True
        try:
            self.printer.callbacks['virtual_sdcard:reset_file']()
        finally:
            self.module._recovery_reset_in_progress = False
        self.assertIs(self.module._recoverable_checkpoint(), previous)
        self.assertEqual(self.module.writer.sequence, 1)

        # Outside of that narrow guard, a user reset still invalidates it.
        self.printer.callbacks['virtual_sdcard:reset_file']()
        self.assertIsNone(self.module._recoverable_checkpoint())
        self.printer.callbacks['klippy:shutdown']()

    def test_repeated_file_reset_creates_distinct_sessions(self):
        self.printer.stats.state = 'standby'
        ids = []
        for _ in range(2):
            self.printer.callbacks['virtual_sdcard:reset_file']()
            ids.append(self.module._session_id)
            self.assertFalse(self.module.get_status(0)['checkpoint_available'])
        self.assertNotEqual(ids[0], ids[1])
        self.wait_until(lambda: (
            (CheckpointStore(self.directory).load()[1] or {})
            .get('job_id') == ids[-1]))
        self.assertEqual(CheckpointStore(self.directory).load()[1]['status'],
                         'invalidated')
        self.printer.callbacks['klippy:shutdown']()

    def test_legacy_checkpoint_hides_on_new_job_and_allows_new_lower_offset(self):
        # Pre-session checkpoint from an older Klipper Revive build.
        self.printer.callbacks['klippy:shutdown']()
        legacy = {'schema': 1, 'status': 'printing',
                  'file': {'path': self.printer.objects['virtual_sdcard'].file,
                           'size': self.printer.objects['virtual_sdcard'].file_size},
                  'file_position': 500}
        CheckpointStore(self.directory).write(1, legacy)
        self.printer.stats.state = 'standby'
        self.module = KlipperRevive(Config(self.printer, self.directory))
        self.printer.callbacks['klippy:ready']()
        self.assertEqual(self.module._recoverable_checkpoint(), legacy)
        self.printer.callbacks['virtual_sdcard:reset_file']()
        self.assertIsNone(self.module._recoverable_checkpoint())
        self.wait_until(lambda: CheckpointStore(self.directory).load()[1]
                        .get('status') == 'invalidated')
        sd = self.printer.objects['virtual_sdcard']
        sd.file_position = 45
        self.printer.stats.state = 'printing'
        self.printer.reactor.callback(1.)
        self.wait_until(lambda: self.module._recoverable_checkpoint() is not None)
        self.assertEqual(self.module.get_status(1.)['file_position'], 45)
        self.printer.callbacks['klippy:shutdown']()

    def test_completed_sd_lines_checkpoint_during_timer_starvation(self):
        # Reproduces real Klipper: SD file_position advances only *after*
        # gcode.run_script returns, while reactor timers mostly run inside
        # long/yielding G-code commands (cmd_from_sd stays True).
        sd = self.printer.objects['virtual_sdcard']
        clock = [0.0]
        self.printer.reactor.monotonic = lambda: clock[0]
        sd.cmd_from_sd = True
        sd.file_position = 100
        sd.next_file_position = 200

        # Timer runs in the middle of command -> must be rejected.
        timer = self.printer.reactor.callback
        timer(0.0)
        self.assertEqual(self.module.writer.sequence, 0)

        # Command succeeds. Post-command hook commits *next* position,
        # not the stale 100-byte file_position.
        self.printer.gcode.run_script('G1 X11')
        self.wait_for(1)
        self.assertEqual(self.module.writer.last_saved['file_position'], 200)
        sd.file_position = 200

        # The next 16 seconds are spent inside repeated commands; the
        # timer never observes cmd_from_sd == False.
        clock[0] = 16.0
        sd.next_file_position = 500
        timer(16.0)
        self.assertEqual(self.module.writer.sequence, 1)
        self.printer.gcode.run_script('G1 X22')
        self.wait_for(2)
        self.assertEqual(self.module.writer.last_saved['file_position'], 500)
        sd.file_position = 500
        sd.cmd_from_sd = False
        self.printer.callbacks['klippy:shutdown']()

    def test_sd_hook_skips_failed_commands_and_nested_scripts(self):
        sd = self.printer.objects['virtual_sdcard']
        clock = [0.0]
        self.printer.reactor.monotonic = lambda: clock[0]
        sd.cmd_from_sd = True
        sd.next_file_position = 300

        def nested(script):
            if script == 'OUTER':
                # Nested macros don't represent a finished SD file line.
                self.printer.gcode.run_script('INNER')
                self.assertEqual(self.module.writer.sequence, 0)
        self.printer.gcode.run_callback = nested
        self.printer.gcode.run_script('OUTER')
        self.wait_for(1)
        self.assertEqual(self.module.writer.last_saved['file_position'], 300)

        clock[0] = 20.0
        sd.next_file_position = 400
        self.printer.gcode.run_callback = lambda script: (_ for _ in ()).throw(
            RuntimeError('failed G-code'))
        with self.assertRaisesRegex(RuntimeError, 'failed G-code'):
            self.printer.gcode.run_script('FAIL')
        self.assertEqual(self.module.writer.sequence, 1)
        self.printer.callbacks['klippy:shutdown']()

    def test_shutdown_unhooks_gcode_dispatch(self):
        wrapper = self.printer.gcode.run_script
        self.assertIsNotNone(self.module._sd_dispatch_original)
        self.printer.callbacks['klippy:shutdown']()
        self.assertIsNot(self.printer.gcode.run_script, wrapper)
        self.assertIsNone(self.module._sd_dispatch_wrapper)
        self.assertFalse(self.module.enabled)

    def test_recovery_commands_registered_without_motion(self):
        commands = self.printer.gcode.commands
        expected = (
            'KLIPPER_REVIVE_STATUS', 'KLIPPER_REVIVE_INSPECT',
            'KLIPPER_REVIVE_VALIDATE', 'KLIPPER_REVIVE_RECOVER',
            'KLIPPER_REVIVE_XY_ADJUST', 'KLIPPER_REVIVE_CONFIRM_XY',
            'KLIPPER_REVIVE_Z_ADJUST', 'KLIPPER_REVIVE_CONFIRM_Z',
            'KLIPPER_REVIVE_RESUME', 'KLIPPER_REVIVE_CANCEL')
        for name in expected:
            self.assertIn(name, commands)
        self.assertEqual(self.module.recovery.phase, 'inactive')
        self.printer.callbacks['klippy:shutdown']()

    def wait_for(self, sequence):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if self.module.writer.sequence >= sequence:
                return
            time.sleep(.01)
        self.fail('checkpoint worker did not flush')

    def test_print_checkpoint_validate_and_complete(self):
        timer = self.printer.reactor.callback
        timer(1.)
        self.wait_for(1)
        s = self.module.get_status(0)
        self.assertTrue(s['checkpoint_available'])
        self.assertEqual(s['file_position'], 100)
        self.assertEqual(self.module.validate_checkpoint()[0], True)
        self.assertEqual(self.module.writer.last_saved['heater_targets']['extruder'], 212.)
        self.printer.stats.state = 'complete'
        timer(2.)
        self.wait_for(2)
        self.assertFalse(self.module.get_status(0)['checkpoint_available'])
        self.assertEqual(CheckpointStore(self.directory).load()[1]['status'], 'complete')
        self.printer.callbacks['klippy:shutdown']()

    def test_file_modification_blocks_validation(self):
        self.printer.reactor.callback(1.)
        self.wait_for(1)
        file = Path(self.printer.objects['virtual_sdcard'].file)
        file.write_bytes(b'G1 X0\n')
        self.assertFalse(self.module.validate_checkpoint()[0])
        self.printer.callbacks['klippy:shutdown']()

    def test_no_checkpoint_when_paused(self):
        self.printer.stats.state = 'paused'
        self.printer.reactor.callback(1.)
        self.assertEqual(self.module.writer.sequence, 0)
        self.printer.callbacks['klippy:shutdown']()


if __name__ == '__main__':
    unittest.main()
