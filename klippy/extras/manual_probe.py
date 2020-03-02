# Helper script for manual z height probing
#
# Copyright (C) 2019  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import bisect

import homing


class ManualProbe:
    def __init__(self, config):
        self.printer = config.get_printer()
        # Register commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('MANUAL_PROBE', self.cmd_MANUAL_PROBE,
                                    desc=self.cmd_MANUAL_PROBE_help)
        self.z_position_endstop = None
        if config.has_section('stepper_z'):
            zconfig = config.getsection('stepper_z')
            if zconfig.get_prefix_options('position_endstop'):
                self.z_position_endstop = zconfig.getfloat('position_endstop')
                self.gcode.register_command(
                    'Z_ENDSTOP_CALIBRATE', self.cmd_Z_ENDSTOP_CALIBRATE,
                    desc=self.cmd_Z_ENDSTOP_CALIBRATE_help)

    def manual_probe_finalize(self, kin_pos):
        if kin_pos is not None:
            self.gcode.respond_info("Z position is %.3f" % (kin_pos[2],))

    cmd_MANUAL_PROBE_help = "Start manual probe helper script"

    def cmd_MANUAL_PROBE(self, params):
        ManualProbeHelper(self.printer, params, self.manual_probe_finalize)

    def z_endstop_finalize(self, kin_pos):
        if kin_pos is None:
            return
        z_pos = self.z_position_endstop - kin_pos[2]
        self.gcode.respond_info(
            "stepper_z: position_endstop: %.3f\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "with the above and restart the printer." % (z_pos,))
        configfile = self.printer.lookup_object('configfile')
        configfile.set('stepper_z', 'position_endstop', "%.3f" % (z_pos,))

    cmd_Z_ENDSTOP_CALIBRATE_help = "Calibrate a Z endstop"

    def cmd_Z_ENDSTOP_CALIBRATE(self, params):
        ManualProbeHelper(self.printer, params, self.z_endstop_finalize)


# Verify that a manual probe isn't already in progress
def verify_no_manual_probe(printer):
    gcode = printer.lookup_object('gcode')
    try:
        gcode.register_command('ACCEPT', 'dummy')
    except printer.config_error as e:
        raise gcode.error(
            "Already in a manual Z probe. Use ABORT to abort it.")
    gcode.register_command('ACCEPT', None)


Z_BOB_MINIMUM = 0.500
BISECT_MAX = 0.200


# Helper script to determine a Z height
class ManualProbeHelper:
    def __init__(self, printer, params, finalize_callback):
        self.printer = printer
        self.finalize_callback = finalize_callback
        self.gcode = self.printer.lookup_object('gcode')
        self.toolhead = self.printer.lookup_object('toolhead')
        self.speed = self.gcode.get_float("SPEED", params, 5.)
        self.past_positions = []
        self.last_toolhead_pos = self.last_kinematics_pos = None
        # Register commands
        verify_no_manual_probe(printer)
        self.gcode.register_command('ACCEPT', self.cmd_ACCEPT,
                                    desc=self.cmd_ACCEPT_help)
        self.gcode.register_command('NEXT', self.cmd_ACCEPT)
        self.gcode.register_command('ABORT', self.cmd_ABORT,
                                    desc=self.cmd_ABORT_help)
        self.gcode.register_command('TESTZ', self.cmd_TESTZ,
                                    desc=self.cmd_TESTZ_help)
        self.gcode.respond_info(
            "Starting manual Z probe. Use TESTZ to adjust position.\n"
            "Finish with ACCEPT or ABORT command.")
        self.start_position = self.toolhead.get_position()
        self.report_z_status()

    def get_kinematics_pos(self):
        toolhead_pos = self.toolhead.get_position()
        if toolhead_pos == self.last_toolhead_pos:
            return self.last_kinematics_pos
        self.toolhead.flush_step_generation()
        kin = self.toolhead.get_kinematics()
        for s in kin.get_steppers():
            s.set_tag_position(s.get_commanded_position())
        kin_pos = kin.calc_tag_position()
        self.last_toolhead_pos = toolhead_pos
        self.last_kinematics_pos = kin_pos
        return kin_pos

    def move_z(self, z_pos):
        curpos = self.toolhead.get_position()
        try:
            if curpos[2] - z_pos < Z_BOB_MINIMUM:
                curpos[2] = z_pos + Z_BOB_MINIMUM
                self.toolhead.move(curpos, self.speed)
            curpos[2] = z_pos
            self.toolhead.move(curpos, self.speed)
        except homing.CommandError as e:
            self.finalize(False)
            raise

    def report_z_status(self, warn_no_change=False, prev_pos=None):
        # Get position
        kin_pos = self.get_kinematics_pos()
        z_pos = kin_pos[2]
        if warn_no_change and z_pos == prev_pos:
            self.gcode.respond_info(
                "WARNING: No change in position (reached stepper resolution)")
        # Find recent positions that were tested
        pp = self.past_positions
        next_pos = bisect.bisect_left(pp, z_pos)
        prev_pos = next_pos - 1
        if next_pos < len(pp) and pp[next_pos] == z_pos:
            next_pos += 1
        prev_str = next_str = "??????"
        if prev_pos >= 0:
            prev_str = "%.3f" % (pp[prev_pos],)
        if next_pos < len(pp):
            next_str = "%.3f" % (pp[next_pos],)
        # Find recent positions
        self.gcode.respond_info("Z position: %s --> %.3f <-- %s" % (
            prev_str, z_pos, next_str))

    cmd_ACCEPT_help = "Accept the current Z position"

    def cmd_ACCEPT(self, params):
        pos = self.toolhead.get_position()
        start_pos = self.start_position
        if pos[:2] != start_pos[:2] or pos[2] >= start_pos[2]:
            self.gcode.respond_info(
                "Manual probe failed! Use TESTZ commands to position the\n"
                "nozzle prior to running ACCEPT.")
            self.finalize(False)
            return
        self.finalize(True)

    cmd_ABORT_help = "Abort manual Z probing tool"

    def cmd_ABORT(self, params):
        self.finalize(False)

    cmd_TESTZ_help = "Move to new Z height"

    def cmd_TESTZ(self, params):
        # Store current position for later reference
        kin_pos = self.get_kinematics_pos()
        z_pos = kin_pos[2]
        pp = self.past_positions
        insert_pos = bisect.bisect_left(pp, z_pos)
        if insert_pos >= len(pp) or pp[insert_pos] != z_pos:
            pp.insert(insert_pos, z_pos)
        # Determine next position to move to
        req = self.gcode.get_str("Z", params)
        if req in ('+', '++'):
            check_z = 9999999999999.9
            if insert_pos < len(self.past_positions) - 1:
                check_z = self.past_positions[insert_pos + 1]
            if req == '+':
                check_z = (check_z + z_pos) / 2.
            next_z_pos = min(check_z, z_pos + BISECT_MAX)
        elif req in ('-', '--'):
            check_z = -9999999999999.9
            if insert_pos > 0:
                check_z = self.past_positions[insert_pos - 1]
            if req == '-':
                check_z = (check_z + z_pos) / 2.
            next_z_pos = max(check_z, z_pos - BISECT_MAX)
        else:
            next_z_pos = z_pos + self.gcode.get_float("Z", params)
        # Move to given position and report it
        self.move_z(next_z_pos)
        self.report_z_status(next_z_pos != z_pos, z_pos)

    def finalize(self, success):
        self.gcode.register_command('ACCEPT', None)
        self.gcode.register_command('NEXT', None)
        self.gcode.register_command('ABORT', None)
        self.gcode.register_command('TESTZ', None)
        kin_pos = None
        if success:
            kin_pos = self.get_kinematics_pos()
        self.finalize_callback(kin_pos)


def load_config(config):
    return ManualProbe(config)
