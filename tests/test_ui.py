"""Tests for the optional Mainsail Macro Prompt wizard.

No Klipper installation or real motion is needed. All buttons are exercised
through the exact native-command dispatch messages issued by the wizard.
"""
import unittest
from types import SimpleNamespace

from klipper_extras.klipper_revive_ui import RecoveryWizard


class Command:
    def __init__(self, **params):
        self.params = params

    def get(self, key, default=None):
        return self.params.get(key, default)

    def error(self, message):
        return RuntimeError(message)


class Gcode:
    def __init__(self, recovery):
        self.recovery = recovery
        self.output = []
        self.executed = []
        self.fail_with = None

    def respond_raw(self, message):
        self.output.append(message)

    def run_script_from_command(self, command):
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        self.executed.append(command)
        if command == 'KLIPPER_REVIVE_RECOVER':
            self.recovery.phase = 'align_xy'
        elif command.startswith('KLIPPER_REVIVE_XY_ADJUST '):
            parts = command.split()
            axis, change = parts[1].split('=')
            self.recovery.offsets[0 if axis == 'X' else 1] += float(change)
        elif command == 'KLIPPER_REVIVE_CONFIRM_XY':
            self.recovery.phase = 'align_z'
        elif command.startswith('KLIPPER_REVIVE_Z_ADJUST '):
            self.recovery.offsets[2] += float(command.split('=')[1])
        elif command == 'KLIPPER_REVIVE_CONFIRM_Z':
            self.recovery.phase = 'ready'
        elif command.startswith('KLIPPER_REVIVE_RESUME '):
            self.recovery.phase = 'complete'
        elif command == 'KLIPPER_REVIVE_CANCEL':
            self.recovery.phase = 'error'


class Owner:
    def __init__(self):
        self.recovery = SimpleNamespace(
            phase='inactive', offsets=[0., 0., 0.],
            max_xy_adjust=5., max_z_adjust=2., snapshot=None)
        self.gcode = Gcode(self.recovery)
        self.printer = SimpleNamespace()
        self.valid = True
        self.checkpoint = {
            'file': {'path': '/gcodes/test/cube.gcode', 'size': 1000},
            'file_position': 600,
            'machine_position': [11., 22., 4., 0.],
            'extruder': 'extruder',
            'heater_targets': {'extruder': 210., 'heater_bed': 60.},
            'mesh_profile': 'PEI',
        }

    def _recoverable_checkpoint(self):
        return self.checkpoint

    def validate_checkpoint(self):
        return ((True, 'OK') if self.valid else
                (False, 'Source G-code no longer matches checkpoint'))


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.owner = Owner()
        self.wizard = RecoveryWizard(self.owner)

    def action(self, name, **params):
        self.wizard.cmd_action(Command(ACTION=name, **params))

    def prepare_xy(self):
        self.wizard.cmd_open(Command())
        self.action('CHECK_SAFETY')
        self.action('BEGIN')

    def prepare_ready(self):
        self.prepare_xy()
        self.action('CONFIRM_XY')
        self.action('CONFIRM_Z')

    def test_wizard_does_not_move_until_separate_physical_safety_ack(self):
        self.wizard.cmd_open(Command())
        self.assertEqual(self.owner.gcode.executed, [])
        output = '\n'.join(self.owner.gcode.output)
        self.assertIn('action:prompt_begin Klipper Revive - Checkpoint (1/6)', output)
        self.assertIn('cube.gcode', output)
        self.assertIn('60.0%', output)
        self.assertIn(
            'action:prompt_button Continue to safety check|KLIPPER_REVIVE_UI ACTION=CHECK_SAFETY|primary',
            output)
        self.assertIn('action:prompt_footer_button Close|KLIPPER_REVIVE_UI ACTION=CLOSE', output)
        with self.assertRaisesRegex(RuntimeError, 'Confirm the physical'):
            self.action('BEGIN')
        self.action('REFRESH')
        self.action('CHECK_SAFETY')
        self.assertEqual(self.owner.gcode.executed, [])
        self.assertIn('Confirm Z has NOT physically moved', '\n'.join(self.owner.gcode.output))
        self.action('BEGIN')
        self.assertEqual(self.owner.gcode.executed, ['KLIPPER_REVIVE_RECOVER'])
        self.assertEqual(self.owner.recovery.phase, 'align_xy')

    def test_xy_z_steps_confirmation_and_external_ack_required(self):
        self.prepare_xy()
        self.action('XY_STEP', STEP='0.05')
        self.action('X_PLUS')
        self.action('Y_MINUS')
        self.assertEqual(self.owner.recovery.offsets[:2], [.05, -.05])
        self.assertIn('KLIPPER_REVIVE_XY_ADJUST X=0.0500',
                      self.owner.gcode.executed)
        self.action('CONFIRM_XY')
        self.assertEqual(self.owner.recovery.phase, 'align_z')
        self.action('Z_STEP', STEP='0.05')
        self.action('Z_MINUS')
        self.assertAlmostEqual(self.owner.recovery.offsets[2], -.05)
        self.action('CONFIRM_Z')
        self.assertEqual(self.owner.recovery.phase, 'ready')
        self.assertFalse(self.wizard.external_confirmed)
        with self.assertRaisesRegex(RuntimeError, 'Filament/MMU approval'):
            self.action('RESUME', SPEED='10')
        self.assertNotIn('KLIPPER_REVIVE_RESUME', ' '.join(self.owner.gcode.executed))
        self.action('REFRESH')
        self.action('EXTERNAL_OK')
        self.assertTrue(self.wizard.external_confirmed)
        self.assertEqual(self.wizard.page, 'speed')
        self.action('RESUME', SPEED='25')
        self.assertEqual(self.owner.recovery.phase, 'complete')
        self.assertIn('KLIPPER_REVIVE_RESUME EXTERNAL_STATE_OK=1 SPEED=25',
                      self.owner.gcode.executed)
        self.assertFalse(self.wizard.external_confirmed)

    def test_invalid_steps_and_speed_fail_closed(self):
        self.prepare_xy()
        with self.assertRaisesRegex(RuntimeError, 'Invalid XY increment'):
            self.action('XY_STEP', STEP='1000')
        self.action('REFRESH')
        self.action('CONFIRM_XY')
        with self.assertRaisesRegex(RuntimeError, 'Invalid Z increment'):
            self.action('Z_STEP', STEP='42')
        self.action('REFRESH')
        self.action('CONFIRM_Z')
        self.action('EXTERNAL_OK')
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            self.action('RESUME', SPEED='200')
        self.assertFalse(any('KLIPPER_REVIVE_RESUME' in cmd
                             for cmd in self.owner.gcode.executed))

    def test_invalid_checkpoint_never_reaches_physical_safety_step(self):
        self.owner.valid = False
        self.wizard.cmd_open(Command())
        self.assertIn('Source G-code no longer matches',
                      '\n'.join(self.owner.gcode.output))
        with self.assertRaisesRegex(RuntimeError, 'no longer matches'):
            self.action('CHECK_SAFETY')
        self.assertEqual(self.owner.gcode.executed, [])

    def test_cancel_requires_confirmation_and_close_only_hides_ui(self):
        self.prepare_xy()
        self.action('CANCEL_ASK')
        self.assertEqual(self.owner.recovery.phase, 'align_xy')
        self.action('CANCEL_BACK')
        self.wizard.cmd_open(Command())
        self.assertIn('XY alignment', '\n'.join(self.owner.gcode.output))
        self.action('CLOSE')
        self.assertFalse(self.wizard.active)
        self.assertEqual(self.owner.recovery.phase, 'align_xy')
        self.wizard.cmd_open(Command())
        self.action('CANCEL_ASK')
        self.action('CANCEL_CONFIRM')
        self.assertEqual(self.owner.recovery.phase, 'error')
        self.assertIn('RESTART Klipper', '\n'.join(self.owner.gcode.output))

    def test_reopen_ready_requires_approval_again_after_new_session(self):
        self.prepare_ready()
        self.action('EXTERNAL_OK')
        self.wizard.reset(close_prompt=True)
        self.wizard.cmd_open(Command())
        self.assertEqual(self.wizard.page, 'filament')
        self.assertFalse(self.wizard.external_confirmed)
        with self.assertRaises(RuntimeError):
            self.action('RESUME', SPEED='10')
        self.assertFalse(any('KLIPPER_REVIVE_RESUME' in cmd
                             for cmd in self.owner.gcode.executed))

    def test_command_error_displays_failure_without_fake_success(self):
        self.prepare_xy()
        self.owner.gcode.fail_with = 'Motor did not move'
        with self.assertRaisesRegex(RuntimeError, 'Motor did not move'):
            self.action('X_PLUS')
        self.assertIn('Command failed',
                      '\n'.join(self.owner.gcode.output))
        self.assertEqual(self.owner.recovery.phase, 'align_xy')
        self.assertEqual(self.owner.recovery.offsets[0], 0.)

    def test_filename_cannot_inject_prompt_commands(self):
        self.owner.checkpoint['file']['path'] = (
            '/gcodes/Cube\n// action:prompt_button INJECTED|M112.gcode')
        self.wizard.cmd_open(Command())
        self.assertFalse(any(line.startswith('// action:prompt_button INJECTED')
                             for line in self.owner.gcode.output))


if __name__ == '__main__':
    unittest.main()
