"""Klipper Revive: installation scaffold.

The recovery engine is not implemented yet. No polling, background workers,
disk writes, or print-motion hooks are started by this module.
"""


class KlipperRevive:
    def __init__(self, config):
        gcode = config.get_printer().lookup_object("gcode")
        gcode.register_command(
            "KLIPPER_REVIVE_STATUS", self.cmd_status,
            desc="Show Klipper Revive installation status"
        )

    def cmd_status(self, gcmd):
        gcmd.respond_info(
            "Klipper Revive is installed. Recovery is not implemented yet."
        )


def load_config(config):
    return KlipperRevive(config)
