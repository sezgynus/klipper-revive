"""Experimental operator-guided print recovery.

All movements require explicit user G-code commands. Only Cartesian/CoreXY
machines are accepted. This module does not replay start macros or restore
state owned privately by third-party plugins.
"""
import math
import os
import re


class RecoveryError(Exception):
    pass


class RecoverySession:
    def __init__(self, owner, z_hop=10.0, xy_speed=60., z_speed=2.,
                 max_xy_adjust=5.0, max_z_adjust=2.0):
        self.owner = owner
        self.printer = owner.printer
        self.gcode = owner.gcode
        self.z_hop = z_hop
        self.xy_speed = xy_speed
        self.z_speed = z_speed
        self.max_xy_adjust = max_xy_adjust
        self.max_z_adjust = max_z_adjust
        self.phase = 'inactive'
        self.snapshot = None
        self.offsets = [0., 0., 0.]
        self.target = None

    def _check_idle(self):
        stats = self.printer.lookup_object('print_stats')
        sd = self.printer.lookup_object('virtual_sdcard')
        if stats.state in ('printing', 'paused') or sd.is_active():
            raise RecoveryError('A print is already active; cannot recover')

    def _head(self):
        return self.printer.lookup_object('toolhead')

    def _move(self, x=None, y=None, z=None, speed=None):
        th = self._head()
        coordinates = [x, y, z] + [None] * (len(th.get_position()) - 3)
        th.manual_move(coordinates, speed or self.xy_speed)
        th.wait_moves()

    def _check_unexpected_motion(self):
        if self.target is None:
            raise RecoveryError('No expected position saved')
        current = self._head().get_position()
        if any(abs(current[i] - self.target[i]) > 0.02 for i in range(3)):
            self.phase = 'error'
            raise RecoveryError('Unexpected toolhead motion; recovery stopped')

    def _script(self, script):
        self.gcode.run_script_from_command(script)

    def prepare(self):
        if self.phase != 'inactive':
            raise RecoveryError('Recovery already active; cancel or restart Klipper')
        self._check_idle()
        ok, message = self.owner.validate_checkpoint()
        if not ok:
            raise RecoveryError(message)
        cp = self.owner.writer.last_saved
        kin = self._head().get_kinematics()
        if type(kin).__name__ not in ('CartKinematics', 'CoreXYKinematics'):
            raise RecoveryError('Recovery currently supports Cartesian and CoreXY only')
        current_ext = cp['extruder']
        heaters = cp['heater_targets']
        target_temp = heaters.get(current_ext)
        if target_temp is None or not math.isfinite(target_temp) or target_temp < 150.:
            raise RecoveryError('Missing or unsafe saved nozzle target temperature')
        axis_status = kin.get_status(self.owner.reactor.monotonic())
        zmax = float(axis_status['axis_maximum'].z)
        saved_z = float(cp['machine_position'][2])
        safe_z = saved_z + self.z_hop
        if not 0 <= saved_z < safe_z <= zmax:
            raise RecoveryError('Insufficient Z travel available for safe lift')
        name = os.path.relpath(cp['file']['path'],
                               self.printer.lookup_object('virtual_sdcard').sdcard_dirname)
        if any(ch in name for ch in ('\n', '\r', ';')):
            raise RecoveryError('Unsupported G-code filename characters')
        mesh_name = cp.get('mesh_profile', '')
        if mesh_name:
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', mesh_name):
                raise RecoveryError('Unsafe mesh profile name')
            bed_mesh = self.printer.lookup_object('bed_mesh', None)
            if bed_mesh is None or mesh_name not in bed_mesh.pmgr.get_profiles():
                raise RecoveryError('Saved bed mesh is unavailable; recovery stopped')
        self.snapshot = cp
        self.offsets = [0., 0., 0.]
        # Long heater waits happen only after the user explicitly starts recovery.
        self.phase = 'heating'
        try:
            bed = heaters.get('heater_bed', 0.)
            if bed > 0:
                self._script('M190 S%.2f' % bed)
            if self._head().get_extruder().get_name() != current_ext:
                self._script('ACTIVATE_EXTRUDER EXTRUDER=%s' % current_ext)
            self._script('M109 S%.2f' % target_temp)
            # Physical Z does NOT move when we assign its saved machine value.
            pos = self._head().get_position()
            pos[2] = saved_z
            self._head().set_position(pos, homing_axes='z')
            self._move(z=safe_z, speed=self.z_speed)
            # Homing is performed only after Z is clear of the print.
            self._script('G28 X Y')
            if abs(self._head().get_position()[2] - safe_z) > .05:
                raise RecoveryError('XY homing unexpectedly moved Z; recovery stopped')
            # Go above the last checkpoint position.
            self._move(x=cp['machine_position'][0],
                       y=cp['machine_position'][1], speed=self.xy_speed)
            self.target = self._head().get_position()[:3]
            self.phase = 'align_xy'
        except Exception:
            self.phase = 'error'
            raise

    def adjust_xy(self, dx, dy):
        if self.phase != 'align_xy':
            raise RecoveryError('XY alignment is not active')
        self._check_unexpected_motion()
        if not all(math.isfinite(v) for v in (dx, dy)):
            raise RecoveryError('Nonfinite XY adjustment')
        xoffset = self.offsets[0] + dx
        yoffset = self.offsets[1] + dy
        if abs(xoffset) > self.max_xy_adjust or abs(yoffset) > self.max_xy_adjust:
            raise RecoveryError('XY adjustment exceeds configured safety limit')
        self._move(x=self.target[0] + dx,
                   y=self.target[1] + dy,
                   speed=min(self.xy_speed, 20.))
        self.offsets[:2] = [xoffset, yoffset]
        self.target = self._head().get_position()[:3]

    def confirm_xy(self):
        if self.phase != 'align_xy':
            raise RecoveryError('XY alignment is not active')
        self._check_unexpected_motion()
        self.phase = 'approaching_z'
        try:
            self._move(z=self.snapshot['machine_position'][2],
                       speed=self.z_speed)
            self.target = self._head().get_position()[:3]
            self.phase = 'align_z'
        except Exception:
            self.phase = 'error'
            raise

    def adjust_z(self, dz):
        if self.phase != 'align_z':
            raise RecoveryError('Z alignment is not active')
        self._check_unexpected_motion()
        if not math.isfinite(dz) or abs(self.offsets[2] + dz) > self.max_z_adjust:
            raise RecoveryError('Z adjustment exceeds configured safety limit')
        self._move(z=self.target[2] + dz, speed=min(self.z_speed, 1.))
        self.offsets[2] += dz
        self.target = self._head().get_position()[:3]

    def confirm_z(self):
        if self.phase != 'align_z':
            raise RecoveryError('Z alignment is not active')
        self._check_unexpected_motion()
        self.phase = 'ready'

    def _open_resume_file(self, sd, gcmd, name):
        """Select a file, including subdirectories, without starting SD.

        M23 scans only the SD root and fails for paths such as test/cube.gcode.
        SDCARD_PRINT_FILE supports subdirectories but starts printing before
        we can seek to the checkpoint. Use its exact selection path directly
        and preserve the interrupted job across the internal SD reset.
        """
        if gcmd is None:
            raise RecoveryError('Recovery requires a G-code command context')
        if not callable(getattr(sd, '_reset_file', None)) or not callable(
                getattr(sd, '_load_file', None)):
            raise RecoveryError('This Klipper virtual_sdcard API is unsupported')
        if self.owner._recovery_reset_in_progress:
            raise RecoveryError('Another recovery SD reset is already active')
        self.owner._recovery_reset_in_progress = True
        try:
            sd._reset_file()
        finally:
            self.owner._recovery_reset_in_progress = False
        sd._load_file(gcmd, name, check_subdirs=True)

    def resume(self, acknowledge_external=False, gcmd=None):
        if self.phase != 'ready':
            raise RecoveryError('Confirm XY and Z before resuming')
        if not acknowledge_external:
            raise RecoveryError('Use EXTERNAL_STATE_OK=1 after checking tool/filament state')
        self._check_unexpected_motion()
        self._check_idle()
        cp = self.snapshot
        ok, message = self.owner.validate_checkpoint()
        if not ok:
            raise RecoveryError(message)
        if cp != self.owner.writer.last_saved:
            raise RecoveryError('Checkpoint changed since alignment')
        move = self.printer.lookup_object('gcode_move')
        gc = cp['gcode']
        sd = self.printer.lookup_object('virtual_sdcard')
        name = os.path.relpath(cp['file']['path'], sd.sdcard_dirname)
        if any(ch in name for ch in ('\n', '\r', ';')):
            raise RecoveryError('Invalid resume filename')
        self.phase = 'restoring'
        try:
            # Restore persistent named mesh before calculating the logical
            # G-code coordinate in the transform chain.
            mesh = cp.get('mesh_profile')
            if mesh:
                self._script('BED_MESH_PROFILE LOAD=%s' % mesh)
            move.reset_last_position()
            current = list(move.last_position)
            multiplier = float(gc['extrude_factor'])
            if multiplier <= 0:
                raise RecoveryError('Invalid saved extrusion factor')
            logical = gc['gcode_position']
            move.absolute_coord = bool(gc['absolute_coordinates'])
            move.allow_absolute_extrude = bool(gc['absolute_extrude'])
            move.extrude_factor = multiplier
            move.speed_factor = float(gc['speed_factor']) / 60.
            if move.speed_factor <= 0:
                raise RecoveryError('Invalid saved speed factor')
            move.speed = float(gc['speed']) * move.speed_factor
            move.homing_position[:4] = gc['homing_origin'][:4]
            for idx in range(4):
                factor = multiplier if idx == 3 else 1.
                move.base_position[idx] = current[idx] - logical[idx] * factor
            # Restore the regular part-cooling fan and configured generic
            # fan outputs where Klipper provides standard commands.
            for fan_name, speed in cp.get('fan_speeds', {}).items():
                if not 0. <= speed <= 1.:
                    raise RecoveryError('Invalid saved fan speed')
                if fan_name == 'fan':
                    self._script('M106 S%d' % round(speed * 255))
                elif fan_name.startswith('fan_generic '):
                    generic = fan_name[len('fan_generic '):]
                    if not re.fullmatch(r'[A-Za-z0-9_.-]+', generic):
                        raise RecoveryError('Unsafe generic fan name')
                    self._script('SET_FAN_SPEED FAN=%s SPEED=%.4f'
                                 % (generic, speed))
            # Restoring coordinates is complete; Klipper controls actual
            # subsequent moves using the calculated base_position.
            self._open_resume_file(sd, gcmd, name)
            self._script('M26 S%d' % cp['file_position'])
            self.phase = 'resuming'
            self._script('M24')
            self.phase = 'complete'
        except Exception:
            self.phase = 'error'
            raise

    def cancel(self):
        if self.phase in ('inactive', 'complete'):
            return
        self.phase = 'error'
        # Do not rehome or move the nozzle. Z was artificially marked homed.
        # User must RESTART Klipper to restore its normal homing state.
