"""Simulated guided recovery tests: no Klipper service or hardware needed."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'klipper_extras'))
from klipper_revive_recovery import RecoverySession, RecoveryError


class CartKinematics:
    def get_status(self, eventtime):
        maximum = SimpleNamespace(x=230., y=230., z=250.)
        return {'axis_maximum': maximum}


class FakeHead:
    def __init__(self):
        self.pos = [0., 0., 0., 0.]
        self.moves = []
        self.kin = CartKinematics()

    def get_kinematics(self):
        return self.kin

    def get_position(self):
        return list(self.pos)

    def set_position(self, newpos, homing_axes=''):
        assert homing_axes == 'z'
        self.pos[:3] = newpos[:3]

    def manual_move(self, coords, speed):
        for i, coordinate in enumerate(coords):
            if coordinate is not None:
                self.pos[i] = coordinate
        self.moves.append((list(self.pos), speed))

    def wait_moves(self):
        pass

    def get_extruder(self):
        return SimpleNamespace(get_name=lambda: 'extruder')


class FakeMove:
    def __init__(self, head):
        self.head = head
        self.last_position = [0., 0., 0., 0.]
        self.base_position = [0., 0., 0., 0.]
        self.homing_position = [0., 0., 0., 0.]
        self.absolute_coord = True
        self.allow_absolute_extrude = True
        self.extrude_factor = 1.
        self.speed_factor = 1. / 60.
        self.speed = 25.

    def reset_last_position(self):
        self.last_position = self.head.get_position()


class FakeSD:
    def __init__(self):
        self.sdcard_dirname = '/tmp'
        self.active = False

    def is_active(self):
        return self.active


class FakeGcode:
    def __init__(self, head, sd, stats):
        self.calls = []
        self.head = head
        self.sd = sd
        self.stats = stats

    def run_script_from_command(self, script):
        self.calls.append(script)
        if script == 'G28 X Y':
            self.head.pos[:2] = [0., 0.]
        if script == 'M24':
            self.sd.active = True
            self.stats.state = 'printing'


class FakePrinter:
    def __init__(self):
        head = FakeHead()
        sd = FakeSD()
        stats = SimpleNamespace(state='standby')
        self.objects = {
            'toolhead': head, 'virtual_sdcard': sd,
            'print_stats': stats, 'gcode_move': FakeMove(head),
            'bed_mesh': SimpleNamespace(
                pmgr=SimpleNamespace(get_profiles=lambda: {'default': {}}))
        }
        self.gcode = FakeGcode(head, sd, stats)

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class FakeOwner:
    def __init__(self):
        self.printer = FakePrinter()
        self.gcode = self.printer.gcode
        self.reactor = SimpleNamespace(monotonic=lambda: 10.)
        self.snapshot = {
            'file': {'path': '/tmp/cube.gcode', 'size': 12345},
            'file_position': 1234,
            'machine_position': [105., 76., 12.5, 200.],
            'extruder': 'extruder',
            'heater_targets': {'extruder': 220., 'heater_bed': 65.},
            'mesh_profile': 'default',
            'gcode': {
                'gcode_position': [105., 76., 12.5, 200.],
                'homing_origin': [0., 0., 0., 0.],
                'absolute_coordinates': True, 'absolute_extrude': True,
                'speed_factor': 1., 'extrude_factor': 1., 'speed': 1800.
            }
        }
        self.writer = SimpleNamespace(last_saved=self.snapshot)

    def validate_checkpoint(self):
        return True, 'Validated'


class GuidedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.owner = FakeOwner()
        self.session = RecoverySession(self.owner)

    def test_align_and_resume_requires_explicit_ack(self):
        session = self.session
        with self.assertRaisesRegex(RecoveryError, 'Confirm XY'):
            session.resume()
        session.prepare()
        self.assertEqual(session.phase, 'align_xy')
        calls = self.owner.gcode.calls
        self.assertEqual(calls[:3], ['M190 S65.00', 'M109 S220.00', 'G28 X Y'])
        self.assertEqual(self.owner.printer.objects['toolhead'].pos[:3],
                         [105., 76., 22.5])
        session.adjust_xy(.2, -.3)
        session.confirm_xy()
        self.assertEqual(session.phase, 'align_z')
        self.assertAlmostEqual(self.owner.printer.objects['toolhead'].pos[2], 12.5)
        session.adjust_z(.05)
        session.confirm_z()
        with self.assertRaisesRegex(RecoveryError, 'EXTERNAL_STATE_OK'):
            session.resume()
        session.resume(True)
        self.assertEqual(session.phase, 'complete')
        self.assertEqual(self.owner.gcode.calls[-3:],
                         ['M23 cube.gcode', 'M26 S1234', 'M24'])
        move = self.owner.printer.objects['gcode_move']
        self.assertAlmostEqual(move.base_position[0], .2)
        self.assertAlmostEqual(move.base_position[1], -.3)
        self.assertAlmostEqual(move.base_position[2], .05)
        self.assertAlmostEqual(move.base_position[3], -200.)

    def test_wrong_phase_adjustment_rejected(self):
        with self.assertRaises(RecoveryError):
            self.session.adjust_z(.1)
        self.session.prepare()
        with self.assertRaises(RecoveryError):
            self.session.confirm_z()
        with self.assertRaises(RecoveryError):
            self.session.adjust_xy(12., 0.)

    def test_unexpected_motion_stops_session(self):
        self.session.prepare()
        self.owner.printer.objects['toolhead'].pos[0] += 1.
        with self.assertRaisesRegex(RecoveryError, 'Unexpected'):
            self.session.confirm_xy()
        self.assertEqual(self.session.phase, 'error')

    def test_unsupported_kinematics_rejected_before_heat(self):
        class DeltaKinematics:
            pass
        self.owner.printer.objects['toolhead'].kin = DeltaKinematics()
        with self.assertRaisesRegex(RecoveryError, 'CoreXY'):
            self.session.prepare()
        self.assertEqual(self.owner.gcode.calls, [])

    def test_nozzle_temperature_required(self):
        self.owner.snapshot['heater_targets']['extruder'] = 0.
        with self.assertRaisesRegex(RecoveryError, 'nozzle'):
            self.session.prepare()
        self.assertEqual(self.owner.gcode.calls, [])


if __name__ == '__main__':
    unittest.main()
