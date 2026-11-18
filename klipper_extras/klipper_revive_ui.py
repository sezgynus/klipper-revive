"""Optional Mainsail Macro Prompt UI for the native Klipper Revive commands.

The UI is a thin view/controller: checkpointing, safety checks, motion, and
resume remain in the existing native recovery handlers. No user CFG macros,
network calls, frontend plugin, or periodic UI polling are required.
"""
import os


class RecoveryWizard:
    XY_STEPS = ('0.05', '0.1', '0.5')
    Z_STEPS = ('0.02', '0.05', '0.1')
    RESUME_SPEEDS = ('10', '25', '50')

    def __init__(self, owner):
        self.owner = owner
        self.printer = owner.printer
        self.gcode = owner.gcode
        self.recovery = owner.recovery
        self.active = False
        self.page = 'checkpoint'
        self.xy_step = '0.1'
        self.z_step = '0.02'
        self.external_confirmed = False
        self.last_error = ''

    @staticmethod
    def _clean(value):
        """Keep untrusted filenames/status strings inside one prompt text."""
        raw = str(value)
        return ' '.join(raw.replace('|', '/').split())[:160]

    def _emit(self, kind, value=''):
        # Buttons use the pipe character as a protocol delimiter. Sanitize
        # only user-supplied display text, never the command/button framing.
        if kind in ('button', 'footer_button'):
            payload = str(value).replace('\r', ' ').replace('\n', ' ')
        else:
            payload = self._clean(value)
        self.gcode.respond_raw('// action:prompt_' + kind
                               + (' ' + payload if payload else ''))

    def _button(self, label, action, color=''):
        text = '%s|KLIPPER_REVIVE_UI ACTION=%s' % (label, action)
        self._emit('button', text + ('|' + color if color else ''))

    def _footer(self, label, action, color=''):
        text = '%s|KLIPPER_REVIVE_UI ACTION=%s' % (label, action)
        self._emit('footer_button', text + ('|' + color if color else ''))

    def _group(self, buttons):
        self._emit('button_group_start')
        for label, action, color in buttons:
            self._button(label, action, color)
        self._emit('button_group_end')

    def _begin(self, title, lines):
        # A new begin/show replaces the current Mainsail prompt atomically.
        self._emit('begin', 'Klipper Revive - ' + title)
        for line in lines:
            self._emit('text', line)

    def _show(self):
        self._emit('show')

    def reset(self, close_prompt=False):
        if close_prompt and self.active:
            self._emit('end')
        self.active = False
        self.page = 'checkpoint'
        self.external_confirmed = False
        self.last_error = ''

    def cmd_open(self, gcmd):
        self.active = True
        self.last_error = ''
        phase = self.recovery.phase
        if phase == 'inactive':
            self.page = 'checkpoint'
            self.external_confirmed = False
        elif phase == 'ready':
            # Reopening must not invent a new MMU/filament acknowledgment.
            self.page = 'speed' if self.external_confirmed else 'filament'
        elif phase in ('align_xy', 'align_z', 'heating', 'approaching_z',
                       'restoring', 'resuming', 'complete', 'error'):
            self.page = 'phase'
        self._render()

    def _require(self, gcmd, phase):
        if self.recovery.phase != phase:
            raise gcmd.error('Revive wizard: expected %s, got %s'
                             % (phase, self.recovery.phase))

    def _script(self, value):
        # Use exactly the same native handlers as console-based recovery.
        self.gcode.run_script_from_command(value)

    def cmd_action(self, gcmd):
        if not self.active:
            raise gcmd.error('Open KLIPPER_REVIVE_WIZARD first')
        action = gcmd.get('ACTION', '').upper()
        try:
            self._action(gcmd, action)
        except Exception as exc:
            # Keep the original Klipper error visible, without allowing the
            # prompt UI to hide the underlying safety failure.
            self.last_error = str(exc)
            self.page = 'issue'
            self._render()
            raise

    def _action(self, gcmd, action):
        phase = self.recovery.phase
        if action == 'CLOSE':
            self.reset(close_prompt=True)
            return
        if action == 'REFRESH':
            self.last_error = ''
            self.page = 'phase'
            if phase == 'inactive':
                self.page = 'checkpoint'
            self._render()
            return
        if action == 'CHECK_SAFETY':
            self._require(gcmd, 'inactive')
            ok, reason = self.owner.validate_checkpoint()
            if not ok:
                raise gcmd.error(reason)
            self.page = 'safety'
        elif action == 'BACK':
            self._require(gcmd, 'inactive')
            if self.page != 'safety':
                raise gcmd.error('No previous wizard page')
            self.page = 'checkpoint'
        elif action == 'BEGIN':
            self._require(gcmd, 'inactive')
            if self.page != 'safety':
                raise gcmd.error('Confirm the physical Z safety check first')
            self._begin('Preparing (2/6)', [
                'Heating to checkpoint temperatures, lifting Z and homing XY.',
                'The operation may take time. Keep the printer attended.',
                'Do not close the prompt to stop motion.',
            ])
            self._show()
            self._script('KLIPPER_REVIVE_RECOVER')
            self.page = 'phase'
        elif action == 'XY_STEP':
            self._require(gcmd, 'align_xy')
            step = gcmd.get('STEP', '')
            if step not in self.XY_STEPS:
                raise gcmd.error('Invalid XY increment')
            self.xy_step = step
        elif action == 'Z_STEP':
            self._require(gcmd, 'align_z')
            step = gcmd.get('STEP', '')
            if step not in self.Z_STEPS:
                raise gcmd.error('Invalid Z increment')
            self.z_step = step
        elif action in ('X_PLUS', 'X_MINUS', 'Y_PLUS', 'Y_MINUS'):
            self._require(gcmd, 'align_xy')
            axis, direction = action.split('_')
            amount = float(self.xy_step) * (1 if direction == 'PLUS' else -1)
            self._script('KLIPPER_REVIVE_XY_ADJUST %s=%.4f' % (axis, amount))
        elif action == 'CONFIRM_XY':
            self._require(gcmd, 'align_xy')
            self._script('KLIPPER_REVIVE_CONFIRM_XY')
        elif action in ('Z_PLUS', 'Z_MINUS'):
            self._require(gcmd, 'align_z')
            amount = float(self.z_step) * (1 if action == 'Z_PLUS' else -1)
            self._script('KLIPPER_REVIVE_Z_ADJUST Z=%.4f' % amount)
        elif action == 'CONFIRM_Z':
            self._require(gcmd, 'align_z')
            self.external_confirmed = False
            self._script('KLIPPER_REVIVE_CONFIRM_Z')
            self.page = 'filament'
        elif action == 'EXTERNAL_OK':
            self._require(gcmd, 'ready')
            if self.page != 'filament':
                raise gcmd.error('Inspect the filament/MMU state first')
            self.external_confirmed = True
            self.page = 'speed'
        elif action == 'RESUME':
            self._require(gcmd, 'ready')
            speed = gcmd.get('SPEED', '')
            if (self.page != 'speed' or not self.external_confirmed
                    or speed not in self.RESUME_SPEEDS):
                raise gcmd.error('Filament/MMU approval or speed is missing')
            self._begin('Resuming (6/6)', [
                'Restoring checkpoint state and resuming the saved G-code.',
                'Do not issue another recovery command.',
            ])
            self._show()
            self._script('KLIPPER_REVIVE_RESUME EXTERNAL_STATE_OK=1 SPEED=%s'
                         % speed)
            self.external_confirmed = False
            self.page = 'phase'
        elif action == 'CANCEL_ASK':
            if phase in ('inactive', 'complete', 'error'):
                raise gcmd.error('No active alignment to cancel')
            self.page = 'cancel_confirm'
        elif action == 'CANCEL_BACK':
            if self.page != 'cancel_confirm':
                raise gcmd.error('No cancellation is pending')
            self.page = 'phase'
        elif action == 'CANCEL_CONFIRM':
            if self.page != 'cancel_confirm':
                raise gcmd.error('Explicit cancellation confirmation required')
            self._script('KLIPPER_REVIVE_CANCEL')
            self.external_confirmed = False
            self.page = 'phase'
        else:
            raise gcmd.error('Unknown Revive wizard action')
        self._render()

    def _render(self):
        if not self.active:
            return
        if self.page == 'issue':
            self._begin('Command failed', [
                'Action rejected: ' + self._clean(self.last_error),
                'No further action is performed by this popup.',
            ])
            self._button('Refresh current stage', 'REFRESH', 'primary')
            self._footer('Close UI', 'CLOSE')
            self._show()
            return
        if self.page == 'cancel_confirm':
            self._begin('Cancel recovery?', [
                'Cancellation stops the guided recovery. It does not move Z.',
                'After cancellation, RESTART Klipper before normal homing.',
            ])
            self._button('No, go back', 'CANCEL_BACK', 'primary')
            self._button('Cancel recovery', 'CANCEL_CONFIRM', 'error')
            self._show()
            return
        phase = self.recovery.phase
        if phase == 'inactive':
            if self.page == 'safety':
                self._render_safety()
            else:
                self._render_preflight()
        elif phase == 'align_xy':
            self._render_xy()
        elif phase == 'align_z':
            self._render_z()
        elif phase == 'ready':
            self._render_ready()
        elif phase in ('heating', 'approaching_z', 'restoring', 'resuming'):
            self._begin('Recovery in progress', [
                'Current state: ' + phase,
                'Keep the printer attended. Wait for this operation to finish.',
            ])
            self._footer('Close UI', 'CLOSE')
            self._show()
        elif phase == 'complete':
            self._begin('Print resumed (6/6)', [
                'Revive resumed the saved G-code.',
                'Monitor the first layers carefully after recovery.',
            ])
            self._footer('Close', 'CLOSE', 'primary')
            self._show()
        else:
            self._begin('Recovery stopped', [
                'Recovery state: ' + phase,
                'Do not home/move normally until the temporary Z reference is',
                'cleared with Klipper RESTART.',
            ])
            self._footer('Close', 'CLOSE')
            self._show()

    def _render_preflight(self):
        cp = self.owner._recoverable_checkpoint()
        if cp is None:
            self._begin('No checkpoint (1/6)', [
                'No interrupted print checkpoint is available.',
                'Starting a new print invalidates previous recovery data.',
            ])
            self._footer('Close', 'CLOSE')
            self._show()
            return
        ok, reason = self.owner.validate_checkpoint()
        f = cp.get('file', {})
        filename = os.path.basename(f.get('path', 'unknown.gcode'))
        file_size = f.get('size', 0)
        byte = cp.get('file_position', 0)
        percent = 100. * byte / file_size if file_size else 0.
        xyz = cp.get('machine_position', (0., 0., 0.))
        heaters = cp.get('heater_targets', {})
        self._begin('Checkpoint (1/6)', [
            'File: ' + filename,
            'Progress: %.1f%% - G-code byte %d' % (percent, byte),
            'X %.3f / Y %.3f / Z %.3f mm' % tuple(xyz[:3]),
            'Nozzle %s C - Bed %s C' % (
                heaters.get(cp.get('extruder', 'extruder'), '?'),
                heaters.get('heater_bed', '?')),
            'Mesh: ' + str(cp.get('mesh_profile', '') or 'none'),
            'Validation: ' + ('OK' if ok else reason),
        ])
        if ok:
            self._button('Continue to safety check', 'CHECK_SAFETY', 'primary')
        self._footer('Close', 'CLOSE')
        self._show()

    def _render_safety(self):
        self._begin('Physical safety (2/6)', [
            'Confirm Z has NOT physically moved since the interruption.',
            'Confirm the part and build plate have not shifted.',
            'Revive trusts the stored Z without probing or homing Z.',
            'Continue heats the toolhead, lifts Z, homes XY and moves.',
        ])
        self._button('I checked Z and the print', 'BEGIN', 'warning')
        self._footer('Back', 'BACK')
        self._footer('Close UI', 'CLOSE')
        self._show()

    def _render_xy(self):
        dx, dy = self.recovery.offsets[:2]
        self._begin('XY alignment (3/6)', [
            'Nozzle is above the stored XY point at a lifted Z height.',
            'X offset: %+.3f mm; Y offset: %+.3f mm' % (dx, dy),
            'Step: %s mm (max +/- %.2f mm per axis)' % (
                self.xy_step, self.recovery.max_xy_adjust),
        ])
        self._group([('%s mm' % s, 'XY_STEP STEP=' + s,
                      'primary' if s == self.xy_step else 'secondary')
                     for s in self.XY_STEPS])
        self._group([
            ('X -', 'X_MINUS', 'secondary'), ('X +', 'X_PLUS', 'primary'),
            ('Y -', 'Y_MINUS', 'secondary'), ('Y +', 'Y_PLUS', 'primary'),
        ])
        self._button('XY aligned - lower to Z', 'CONFIRM_XY', 'warning')
        self._footer('Cancel recovery', 'CANCEL_ASK', 'error')
        self._footer('Close UI', 'CLOSE')
        self._show()

    def _render_z(self):
        dz = self.recovery.offsets[2]
        self._begin('Z alignment (4/6)', [
            'The nozzle is near the saved layer height.',
            'Z offset: %+.3f mm' % dz,
            'Step: %s mm (max +/- %.2f mm)' % (
                self.z_step, self.recovery.max_z_adjust),
            'Watch nozzle clearance and the print surface.',
        ])
        self._group([('%s mm' % s, 'Z_STEP STEP=' + s,
                      'primary' if s == self.z_step else 'secondary')
                     for s in self.Z_STEPS])
        self._group([
            ('Z down (-)', 'Z_MINUS', 'warning'),
            ('Z up (+)', 'Z_PLUS', 'primary'),
        ])
        self._button('Z aligned - continue', 'CONFIRM_Z', 'warning')
        self._footer('Cancel recovery', 'CANCEL_ASK', 'error')
        self._footer('Close UI', 'CLOSE')
        self._show()

    def _render_ready(self):
        if self.page != 'speed' or not self.external_confirmed:
            self.page = 'filament'
            self._begin('Filament / MMU (5/6)', [
                'Check that the required filament is physically loaded.',
                'If Happy Hare MMU is used, verify selected gate/tool and',
                'filament position manually. Revive does NOT restore',
                'private MMU state automatically.',
                'The confirmation applies only to this recovery session.',
            ])
            self._button('Filament / MMU state verified', 'EXTERNAL_OK', 'warning')
        else:
            cp = self.recovery.snapshot or {}
            self._begin('Resume speed (6/6)', [
                'XY and Z alignment confirmed.',
                'Filament / MMU state manually confirmed.',
                'G-code byte: %s' % cp.get('file_position', '?'),
                'Choose a cautious initial speed (M220 override).',
            ])
            for s in self.RESUME_SPEEDS:
                self._button('Resume at %s%%' % s, 'RESUME SPEED=' + s,
                             'primary' if s == '10' else 'warning')
        self._footer('Cancel recovery', 'CANCEL_ASK', 'error')
        self._footer('Close UI', 'CLOSE')
        self._show()
