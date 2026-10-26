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


class Obj:
    def __init__(self, **values):
        self.values = values

    def get_status(self, eventtime):
        return self.values


class SD:
    cmd_from_sd = False
    file_position = 100
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
