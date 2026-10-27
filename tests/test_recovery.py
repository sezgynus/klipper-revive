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

    def get_status(self, eventtime):
        return {'minimum_cruise_ratio': .5}

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
        self.file_position = 0
        self.on_reset = None
        self.reset_count = 0
        self.open_calls = []
        self.fail_open = False

    def is_active(self):
        return self.active

    def _reset_file(self):
        self.reset_count += 1
        self.active = False
        self.file_position = 0
        if self.on_reset is not None:
            self.on_reset()

    def _load_file(self, gcmd, filename, check_subdirs=False):
        self.open_calls.append((filename, check_subdirs, self.active))
        if self.fail_open:
            raise RuntimeError('Unable to open file')
        if '/' in filename and not check_subdirs:
            raise RuntimeError('M23 does not scan subdirectories')
        gcmd.respond_raw('File selected')


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
        if script.startswith('M26 S'):
            self.sd.file_position = int(script[5:])
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
            'extruder': SimpleNamespace(get_status=lambda _: {
                'pressure_advance': .0, 'smooth_time': .04}),
            'firmware_retraction': SimpleNamespace(get_status=lambda _: {
                'retract_length': 1., 'retract_speed': 50.,
                'unretract_extra_length': 0., 'unretract_speed': 50.}),
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
        self._recovery_reset_in_progress = False
        self.unexpected_resets = 0
        self.printer.objects['virtual_sdcard'].on_reset = self._on_reset

    def _on_reset(self):
        if not self._recovery_reset_in_progress:
            self.unexpected_resets += 1

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
        messages = []
        session.resume(True, gcmd=SimpleNamespace(respond_raw=messages.append))
        self.assertEqual(session.phase, 'complete')
        self.assertEqual(self.owner.gcode.calls[-2:],
                         ['M26 S1234', 'M24'])
        sd = self.owner.printer.objects['virtual_sdcard']
        self.assertEqual(sd.open_calls, [('cube.gcode', True, False)])
        self.assertEqual(sd.file_position, 1234)
        self.assertEqual(sd.reset_count, 1)
        self.assertEqual(self.owner.unexpected_resets, 0)
        self.assertFalse(self.owner._recovery_reset_in_progress)
        move = self.owner.printer.objects['gcode_move']
        self.assertAlmostEqual(move.base_position[0], .2)
        self.assertAlmostEqual(move.base_position[1], -.3)
        self.assertAlmostEqual(move.base_position[2], .05)
        self.assertAlmostEqual(move.base_position[3], -200.)

    def test_subdirectory_resume_opens_before_seek_and_start(self):
        cp = self.owner.snapshot
        cp['file']['path'] = '/tmp/test/Kup_ABS_12m16s.gcode'
        session = self.session
        session.prepare()
        session.confirm_xy()
        session.confirm_z()
        sd = self.owner.printer.objects['virtual_sdcard']
        messages = []
        session.resume(True, gcmd=SimpleNamespace(respond_raw=messages.append))
        self.assertEqual(sd.open_calls, [
            ('test/Kup_ABS_12m16s.gcode', True, False)])
        self.assertEqual(sd.file_position, cp['file_position'])
        self.assertTrue(sd.active)
        self.assertEqual(self.owner.gcode.calls[-2:],
                         ['M26 S1234', 'M24'])
        self.assertEqual(self.owner.unexpected_resets, 0)
        self.assertIn('File selected', messages)

    def test_file_open_error_keeps_checkpoint_and_releases_reset_guard(self):
        session = self.session
        session.prepare()
        session.confirm_xy()
        session.confirm_z()
        sd = self.owner.printer.objects['virtual_sdcard']
        sd.fail_open = True
        saved = self.owner.writer.last_saved
        with self.assertRaisesRegex(RuntimeError, 'Unable to open file'):
            session.resume(True, gcmd=SimpleNamespace(respond_raw=lambda _: None))
        self.assertEqual(session.phase, 'error')
        self.assertIs(self.owner.writer.last_saved, saved)
        self.assertEqual(self.owner.unexpected_resets, 0)
        self.assertFalse(self.owner._recovery_reset_in_progress)
        self.assertNotIn('M24', self.owner.gcode.calls)

    def test_missing_gcode_context_fails_before_sd_reset(self):
        session = self.session
        session.prepare()
        session.confirm_xy()
        session.confirm_z()
        with self.assertRaisesRegex(RecoveryError, 'G-code command context'):
            session.resume(True)
        sd = self.owner.printer.objects['virtual_sdcard']
        self.assertEqual(sd.reset_count, 0)
        self.assertNotIn('M24', self.owner.gcode.calls)

    def test_restores_dynamic_tuning_before_start(self):
        cp = self.owner.snapshot
        cp['motion_limits'] = {
            'max_velocity': 460., 'max_accel': 17500.,
            'square_corner_velocity': 6.5, 'minimum_cruise_ratio': .25,
        }
        cp['pressure_advance'] = {'advance': .0925, 'smooth_time': .055}
        cp['firmware_retraction'] = {
            'retract_length': 1.6, 'retract_speed': 32.,
            'unretract_extra_length': .15, 'unretract_speed': 37.,
        }
        session = self.session
        session.prepare()
        session.confirm_xy()
        session.confirm_z()
        session.resume(True, gcmd=SimpleNamespace(respond_raw=lambda _: None))
        calls = self.owner.gcode.calls
        self.assertEqual(calls[-5:], [
            'M26 S1234',
            'SET_VELOCITY_LIMIT VELOCITY=460.000000000 ACCEL=17500.000000000 '
            'SQUARE_CORNER_VELOCITY=6.500000000 MINIMUM_CRUISE_RATIO=0.250000000',
            'SET_PRESSURE_ADVANCE EXTRUDER=extruder ADVANCE=0.092500000 '
            'SMOOTH_TIME=0.055000000',
            'SET_RETRACTION RETRACT_LENGTH=1.600000000 '
            'RETRACT_SPEED=32.000000000 UNRETRACT_EXTRA_LENGTH=0.150000000 '
            'UNRETRACT_SPEED=37.000000000',
            'M24',
        ])

    def test_invalid_tuning_fails_before_heat_or_motion(self):
        self.owner.snapshot['motion_limits'] = {
            'max_velocity': 500., 'max_accel': -1.,
            'square_corner_velocity': 5., 'minimum_cruise_ratio': .5,
        }
        with self.assertRaisesRegex(RecoveryError, 'max_accel'):
            self.session.prepare()
        self.assertEqual(self.owner.gcode.calls, [])
        self.assertEqual(self.owner.printer.objects['toolhead'].moves, [])

    def test_missing_firmware_retraction_blocks_incompatible_recovery(self):
        self.owner.snapshot['firmware_retraction'] = {
            'retract_length': 1., 'retract_speed': 50.,
            'unretract_extra_length': 0., 'unretract_speed': 50.,
        }
        self.owner.printer.objects.pop('firmware_retraction')
        with self.assertRaisesRegex(RecoveryError, 'unavailable'):
            self.session.prepare()
        self.assertEqual(self.owner.gcode.calls, [])

    def test_legacy_checkpoint_without_tuning_still_resumes(self):
        self.session.prepare()
        self.session.confirm_xy()
        self.session.confirm_z()
        self.session.resume(True, gcmd=SimpleNamespace(respond_raw=lambda _: None))
        self.assertEqual(self.owner.gcode.calls[-2:], ['M26 S1234', 'M24'])
        self.assertEqual(self.session.phase, 'complete')

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
