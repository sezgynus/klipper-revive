"""Minimal, bounded Klipper state capture at SD motion-command entry.

No filesystem, network, or G-code dispatch. The collector reads a strict
whitelist of Klipper state; persistence runs on a separate worker thread.
"""
import math

SCHEMA_VERSION = 1


def number(value):
    n = float(value)
    if not math.isfinite(n):
        raise ValueError('Nonfinite printer state')
    return n


def coords(value):
    return [number(getattr(value, key)) for key in ('x', 'y', 'z', 'e')]


class StateCollector:
    def __init__(self, printer):
        self.printer = printer
        self.sd = printer.lookup_object('virtual_sdcard')
        self.move = printer.lookup_object('gcode_move')
        self.stats = printer.lookup_object('print_stats')
        self.toolhead = printer.lookup_object('toolhead')
        self.heaters = []
        self.fans = []
        self.mesh = None
        self.firmware_retraction = None
        self.refresh_objects()

    def refresh_objects(self):
        """Called once after Klipper ready, not at every checkpoint."""
        objects = list(self.printer.lookup_objects())
        self.heaters = [(name, obj) for name, obj in objects
                        if name == 'heater_bed' or name.startswith('extruder')
                        or name.startswith('heater_generic ')]
        self.fans = [(name, obj) for name, obj in objects
                     if name == 'fan' or name.startswith('fan_generic ')]
        self.mesh = self.printer.lookup_object('bed_mesh', None)
        self.firmware_retraction = self.printer.lookup_object(
            'firmware_retraction', None)

    def capture(self, eventtime, at_motion_start=False):
        """Snapshot the state *before* the current SD motion command.

        VirtualSD sets cmd_from_sd=True and next_file_position to the
        END of the current line just before invoking gcode.run_script().
        file_position still points to that line's START. Use that address
        so recovery re-enters at the current command, not the next one.
        """
        if self.stats.state != 'printing':
            return None
        if not at_motion_start or not self.sd.cmd_from_sd:
            return None
        position_field = 'file_position'
        path = self.sd.file_path()
        before = int(getattr(self.sd, position_field))
        size = int(self.sd.file_size)
        if not path or before < 0 or size <= 0 or before >= size:
            return None
        gc = self.move.get_status(eventtime)
        machine = [number(v) for v in self.toolhead.get_position()[:4]]
        extruder = self.toolhead.get_extruder().get_name()
        # Live tuning can change via G-code, UI, macros or filament profiles.
        # Read it on motion events; non-motion SD lines do not trigger capture.
        toolhead_status = self.toolhead.get_status(eventtime)
        limit_keys = ('max_velocity', 'max_accel',
                      'square_corner_velocity', 'minimum_cruise_ratio')
        motion_limits = None
        if all(key in toolhead_status for key in limit_keys):
            motion_limits = {key: number(toolhead_status[key])
                             for key in limit_keys}
        extruder_status = self.printer.lookup_object(extruder).get_status(eventtime)
        pressure_advance = None
        if 'pressure_advance' in extruder_status and 'smooth_time' in extruder_status:
            pressure_advance = {
                'advance': number(extruder_status['pressure_advance']),
                'smooth_time': number(extruder_status['smooth_time']),
            }
        firmware_retraction = None
        if self.firmware_retraction is not None:
            retract_status = self.firmware_retraction.get_status(eventtime)
            retract_keys = ('retract_length', 'retract_speed',
                            'unretract_extra_length', 'unretract_speed')
            if all(key in retract_status for key in retract_keys):
                firmware_retraction = {key: number(retract_status[key])
                                       for key in retract_keys}
        heat = {}
        for name, obj in self.heaters:
            status = obj.get_status(eventtime)
            heat[name] = number(status.get('target', 0))
        fan = {}
        for name, obj in self.fans:
            status = obj.get_status(eventtime)
            fan[name] = number(status.get('speed', 0))
        mesh_name = ''
        if self.mesh is not None and self.mesh.z_mesh is not None:
            mesh_name = str(self.mesh.z_mesh.get_profile_name() or '')
        # Reject any interleaved source-file advancement.
        if (not self.sd.cmd_from_sd
                or self.stats.state != 'printing'
                or int(getattr(self.sd, position_field)) != before
                or self.sd.file_path() != path):
            return None
        snapshot = {
            'schema': SCHEMA_VERSION,
            'status': 'printing',
            'file': {'path': str(path), 'size': size},
            'file_position': before,
            'machine_position': machine,
            'gcode': {
                'position': coords(gc['position']),
                'gcode_position': coords(gc['gcode_position']),
                'homing_origin': coords(gc['homing_origin']),
                'absolute_coordinates': bool(gc['absolute_coordinates']),
                'absolute_extrude': bool(gc['absolute_extrude']),
                'speed_factor': number(gc['speed_factor']),
                'extrude_factor': number(gc['extrude_factor']),
                'speed': number(gc['speed']),
            },
            'extruder': str(extruder),
            'heater_targets': heat,
            'fan_speeds': fan,
            'mesh_profile': mesh_name,
        }
        if motion_limits is not None:
            snapshot['motion_limits'] = motion_limits
        if pressure_advance is not None:
            snapshot['pressure_advance'] = pressure_advance
        if firmware_retraction is not None:
            snapshot['firmware_retraction'] = firmware_retraction
        return snapshot
