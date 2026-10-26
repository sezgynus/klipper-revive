"""Minimal, bounded Klipper state sampling. Runs only from reactor timer.

No filesystem, network, or gcode dispatch. The collector deliberately uses
whitelisted Klipper state and does not attempt to serialize arbitrary plugins.
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

    def capture(self, eventtime):
        """Return one bounded plain-data snapshot, or None when not printing.

        Never poll data while Klipper is executing a file command: yieldable
        macros can otherwise change state while file_position is still old.
        """
        if self.stats.state != 'printing' or self.sd.cmd_from_sd:
            return None
        path = self.sd.file_path()
        before = int(self.sd.file_position)
        size = int(self.sd.file_size)
        if not path or before <= 0 or size <= 0 or before >= size:
            return None
        gc = self.move.get_status(eventtime)
        machine = [number(v) for v in self.toolhead.get_position()[:4]]
        extruder = self.toolhead.get_extruder().get_name()
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
        if self.sd.cmd_from_sd or self.stats.state != 'printing' \
                or self.sd.file_position != before or self.sd.file_path() != path:
            return None
        return {
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
