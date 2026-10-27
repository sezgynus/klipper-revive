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
    next_file_position = 120
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
    def __init__(self):
        self.limits = {
            'max_velocity': 500., 'max_accel': 20000.,
            'square_corner_velocity': 5., 'minimum_cruise_ratio': .5,
        }

    def get_position(self):
        return [1.0, 2.0, 3.0, 4.0]

    def get_status(self, eventtime):
        return self.limits

    def get_extruder(self):
        return SimpleNamespace(get_name=lambda: 'extruder')


class Printer:
    def __init__(self):
        self.sd = SD()
        self.stats = SimpleNamespace(state='printing')
        self.values = {'virtual_sdcard': self.sd,
                       'gcode_move': Move(), 'print_stats': self.stats,
                       'toolhead': Head(),
                       'extruder': Obj(target=215,
                                       pressure_advance=.07, smooth_time=.04),
                       'firmware_retraction': Obj(
                           retract_length=1., retract_speed=50.,
                           unretract_extra_length=0., unretract_speed=50.),
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
        self.printer.sd.cmd_from_sd = True
        state = self.collector.capture(1.0, at_motion_start=True)
        self.assertEqual(state['file_position'], 100)
        self.assertEqual(state['heater_targets'], {'extruder': 215, 'heater_bed': 60})
        self.assertEqual(state['fan_speeds'], {'fan': .7})
        self.assertEqual(state['machine_position'], [1, 2, 3, 4])
        self.assertFalse(state['gcode']['absolute_extrude'])
        self.assertEqual(state['motion_limits'], {
            'max_velocity': 500., 'max_accel': 20000.,
            'square_corner_velocity': 5., 'minimum_cruise_ratio': .5,
        })
        self.assertEqual(state['pressure_advance'],
                         {'advance': .07, 'smooth_time': .04})
        self.assertEqual(state['firmware_retraction'], {
            'retract_length': 1., 'retract_speed': 50.,
            'unretract_extra_length': 0., 'unretract_speed': 50.,
        })

    def test_live_tuning_changes_are_reflected_in_new_checkpoint(self):
        self.printer.values['toolhead'].limits.update(
            max_velocity=360., max_accel=8700.,
            square_corner_velocity=7., minimum_cruise_ratio=.2)
        self.printer.values['extruder'].values.update(
            pressure_advance=.095, smooth_time=.06)
        self.printer.values['firmware_retraction'].values.update(
            retract_length=1.4, retract_speed=31.,
            unretract_extra_length=.2, unretract_speed=38.)
        self.printer.sd.cmd_from_sd = True
        snapshot = self.collector.capture(2., at_motion_start=True)
        self.assertEqual(snapshot['motion_limits']['max_accel'], 8700.)
        self.assertEqual(snapshot['motion_limits']['minimum_cruise_ratio'], .2)
        self.assertEqual(snapshot['pressure_advance']['advance'], .095)
        self.assertEqual(snapshot['pressure_advance']['smooth_time'], .06)
        self.assertEqual(snapshot['firmware_retraction']['retract_length'], 1.4)
        self.assertEqual(snapshot['firmware_retraction']['unretract_extra_length'], .2)

    def test_optional_tuning_is_omitted_when_module_missing(self):
        self.printer.values.pop('firmware_retraction')
        self.printer.values['extruder'].values.pop('pressure_advance')
        self.collector.refresh_objects()
        self.printer.sd.cmd_from_sd = True
        snapshot = self.collector.capture(1., at_motion_start=True)
        self.assertNotIn('firmware_retraction', snapshot)
        self.assertNotIn('pressure_advance', snapshot)
        self.assertIn('motion_limits', snapshot)

    def test_capture_only_during_sd_motion_entry(self):
        sd = self.printer.sd
        self.assertIsNone(self.collector.capture(0., at_motion_start=True))
        sd.cmd_from_sd = True
        self.assertIsNotNone(self.collector.capture(0., at_motion_start=True))
        self.assertIsNone(self.collector.capture(0.))
        self.printer.stats.state = 'paused'
        self.assertIsNone(self.collector.capture(0., at_motion_start=True))

    def test_motion_entry_uses_line_start_not_next_byte(self):
        sd = self.printer.sd
        sd.cmd_from_sd = True
        sd.file_position = 100
        sd.next_file_position = 280
        snapshot = self.collector.capture(1., at_motion_start=True)
        self.assertEqual(snapshot['file_position'], 100)
        self.assertNotEqual(snapshot['file_position'], 280)

    def test_motion_entry_at_file_byte_zero_is_valid(self):
        sd = self.printer.sd
        sd.cmd_from_sd = True
        sd.file_position = 0
        sd.next_file_position = 15
        snapshot = self.collector.capture(1., at_motion_start=True)
        self.assertEqual(snapshot['file_position'], 0)

    def test_capture_rejects_sd_byte_advancement(self):
        sd = self.printer.sd
        sd.cmd_from_sd = True
        original = self.collector.move.get_status

        def progress_during_capture(eventtime):
            sd.file_position += 10
            return original(eventtime)

        self.collector.move.get_status = progress_during_capture
        self.assertIsNone(self.collector.capture(1., at_motion_start=True))

    def test_capture_rejects_stopped_sd_dispatch(self):
        sd = self.printer.sd
        sd.cmd_from_sd = True
        original = self.collector.move.get_status

        def stopped_during_capture(eventtime):
            sd.cmd_from_sd = False
            return original(eventtime)

        self.collector.move.get_status = stopped_during_capture
        self.assertIsNone(self.collector.capture(1., at_motion_start=True))


if __name__ == '__main__':
    unittest.main()
