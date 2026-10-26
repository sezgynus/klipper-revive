"""Pure Python Klipper state collector tests."""
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'klipper_extras'))
from klipper_revive_collect import StateCollector


class Obj:
    def __init__(self, **values):
        self.values = values

    def get_status(self, eventtime):
        return self.values


class SD:
    cmd_from_sd = False
    file_position = 100
    file_size = 1200

    def file_path(self):
        return '/tmp/cube.gcode'


class Move:
    def get_status(self, eventtime):
        pos = SimpleNamespace(x=1.0, y=2.0, z=3.0, e=4.0)
        return {'position': pos, 'gcode_position': pos,
                'homing_origin': pos, 'speed_factor': 1.0,
                'extrude_factor': 1.0, 'speed': 2000,
                'absolute_coordinates': True, 'absolute_extrude': False}


class Head:
    def get_position(self):
        return [1.0, 2.0, 3.0, 4.0]
    def get_extruder(self):
        return SimpleNamespace(get_name=lambda: 'extruder')


class Printer:
    def __init__(self):
        self.sd = SD()
        self.stats = SimpleNamespace(state='printing')
        self.values = {'virtual_sdcard': self.sd,
                       'gcode_move': Move(), 'print_stats': self.stats,
                       'toolhead': Head(),
                       'extruder': Obj(target=215),
                       'heater_bed': Obj(target=60),
                       'fan': Obj(speed=0.7)}
    def lookup_object(self, name, default=None):
        return self.values.get(name, default)
    def lookup_objects(self):
        return list(self.values.items())


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.printer = Printer()
        self.collector = StateCollector(self.printer)

    def test_capture_whitelisted_fields(self):
        state = self.collector.capture(1.0)
        self.assertEqual(state['file_position'], 100)
        self.assertEqual(state['heater_targets'], {'extruder': 215, 'heater_bed': 60})
        self.assertEqual(state['fan_speeds'], {'fan': .7})
        self.assertEqual(state['machine_position'], [1, 2, 3, 4])
        self.assertFalse(state['gcode']['absolute_extrude'])

    def test_paused_or_executing_skipped(self):
        self.printer.stats.state = 'paused'
        self.assertIsNone(self.collector.capture(0))
        self.printer.stats.state = 'printing'
        self.printer.sd.cmd_from_sd = True
        self.assertIsNone(self.collector.capture(0))
        self.printer.sd.cmd_from_sd = False

    def test_skip_changed_file_position(self):
        old = self.collector.move.get_status
        def advance(eventtime):
            self.printer.sd.file_position += 10
            return old(eventtime)
        self.collector.move.get_status = advance
        self.assertIsNone(self.collector.capture(0))


if __name__ == '__main__':
    unittest.main()
