# coding=utf-8
#
#  bang_bang_on_off.py - A hysteretic control for On/Off Outputs
import time
import threading
import copy

from flask_babel import lazy_gettext

from mycodo.databases.models import Input
from mycodo.databases.models import CustomController
from mycodo.databases.models import DeviceMeasurements
from mycodo.functions.base_function import AbstractFunction
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.constraints_pass import constraints_pass_positive_value
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.actions import which_controller
from mycodo.databases.utils import session_scope
from mycodo.config import MYCODO_DB_PATH
from mycodo.utils.influx import add_measurements_influxdb
from mycodo.utils.influx import read_influxdb_single
from mycodo.utils.system_pi import get_measurement

measurements_dict = {
    0: {
        'measurement': '',
        'unit': 'none',
        'name': 'Flooding count',
    },
    1: {
        'measurement': '',
        'unit': 'none',
        'name': 'Error count',
    },
    2: {
        'measurement': '',
        'unit': 's',
        'name': 'Flooding time',
    },
    3: {
        'measurement': '',
        'unit': 'l',
        'name': 'Flooding volume',
    },
    4: {
        'measurement': '',
        'unit': 's',
        'name': 'Draining time',
    },
    5: {
        'measurement': '',
        'unit': 's',
        'name': 'Low water time',
    },
}

FUNCTION_INFORMATION = {
    'function_name_unique': 'ebb_flood_irrigation',
    'function_name': 'Ebb-Flood irrigation',
    'function_name_short': 'Ebb-Flood irrigation',

    'message': 'A simple ebb-flood control for controlling one outputs from one input.'
        ' Note: This output will only work with On/Off Outputs.',

    'dependencies_module': [
        ('pip-pypi', 'transitions', 'transitions==0.6.4'),
    ],

    'measurements_dict': measurements_dict,
    'options_enabled': [
        'measurements_configure'
    ],
    'custom_commands': [
        {
            'id': 'button_reset_errors',  # This button will execute the "button_one(self, args_dict) function, below
            'type': 'button',
            'wait_for_return': True,  # The UI will wait until the function has returned the UI with a value to display
            'name': 'Reset error counter',
            'phrase': "Reset error counter"
        },
    ],
    'custom_options': [
        {
            'id': 'period',
            'type': 'float',
            'default_value': 1,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': "{} ({})".format(lazy_gettext('Period'), lazy_gettext('Seconds')),
            'phrase': lazy_gettext('The duration between measurements or actions')
        },
        {
            'type': 'message',
            'default_value': "Select inputs to react on ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
        },
        {
            'id': 'measurement_min_waterlevel',
            'type': 'select_measurement',
            'default_value': '',
            'required': False,
            'options_select': [
                'Input',
                'Function'
            ],
            'name': lazy_gettext('Min water level Measurement'),
            'phrase': lazy_gettext('Select a measurement for minimum water level')
        },
        {
            'id': 'measurement_flood_waterlevel',
            'type': 'select_measurement',
            'default_value': '',
            'required': True,
            'options_select': [
                'Input',
                'Function'
            ],
            'name': lazy_gettext('Flood level Measurement'),
            'phrase': lazy_gettext('Select a measurement the selected output will affect')
        },
        {
            'id': 'measurement_max_age',
            'type': 'integer',
            'default_value': 3,
            'required': True,
            'name': "{}: {} ({})".format(lazy_gettext("Measurement"), lazy_gettext("Max Age"),
                                         lazy_gettext("Seconds")),
            'phrase': lazy_gettext('The maximum age of the measurement to use')
        },
        {
            'type': 'message',
            'default_value': "Select outputs to operate ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
        },
        {
            'id': 'output_waterpump',
            'type': 'select_measurement_channel',
            'default_value': '',
            'required': True,
            'options_select': [
                'Output_Channels_Measurements',
            ],
            'name': 'Output (Waterpump)',
            'phrase': 'Select an output to control that will raise the measurement'
        },
        {
            'type': 'new_line'
        },
        {
            'id': 'output_valve1',
            'type': 'select_measurement_channel',
            'default_value': '',
            'required': False,
            'options_select': [
                'Output_Channels_Measurements',
            ],
            'name': 'Output (Valve 1)',
            'phrase': 'Select an output to control that will activate valve 1'
        },
        {
            'id': 'output_valve2',
            'type': 'select_measurement_channel',
            'default_value': '',
            'required': False,
            'options_select': [
                'Output_Channels_Measurements',
            ],
            'name': 'Output (Valve 2)',
            'phrase': 'Select an output to control that will activate valve 2'
        },
        {
            'type': 'new_line'
        },
        {
            'id': 'output_max_time',
            'type': 'float',
            'default_value': 120,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': lazy_gettext('Output maximal on-time'),
            'phrase': lazy_gettext('The maximum time to turn the output on')
        },
        {
            'type': 'message',
            'default_value': "Set timings ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"
        },
        {
            'id': 'max_flooding_time',
            'type': 'float',
            'default_value': 110,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': "{} ({})".format(lazy_gettext('Maximum flooding time'), lazy_gettext('Seconds')),
            'phrase': lazy_gettext('The max duration to fill')
        },
        {
            'id': 'flooding_overshoot',
            'type': 'float',
            'default_value': 5,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': "{} ({})".format(lazy_gettext('Flooding overshoot time'), lazy_gettext('Seconds')),
            'phrase': lazy_gettext('The time extension to flooding')
        },
        {
            'id': 'max_draining_time',
            'type': 'float',
            'default_value': 60,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': "{} ({})".format(lazy_gettext('Max. draining time'), lazy_gettext('Seconds')),
            'phrase': lazy_gettext('The maximum time to wait until water is drained')
        },
        {
            'id': 'valve_cleaning_time',
            'type': 'float',
            'default_value': 30,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': "{} ({})".format(lazy_gettext('Valve cleaning time'), lazy_gettext('Seconds')),
            'phrase': lazy_gettext('Valve cleaning time')
        },
    ]
}


class CustomModule(AbstractFunction):
    """
    Class to operate custom controller
    """
    def __init__(self, function, testing=False):
        super().__init__(function, testing=testing, name=__name__)

        self.control_variable = None
        self.control = DaemonControl()
        self.timer_loop = time.time()

        # Initialize custom options
        self.period = None
        self.measurement_flood_waterlevel_device_id = None
        self.measurement_flood_waterlevel_device = None
        self.measurement_flood_waterlevel_measurement_id = None
        self.measurement_min_waterlevel_device_id = None
        self.measurement_min_waterlevel_device = None
        self.measurement_min_waterlevel_measurement_id = None
        self.measurement_max_age = None
        self.output_waterpump_device_id = None
        self.output_waterpump_measurement_id = None
        self.output_waterpump_channel_id = None
        self.output_waterpump_channel = None
        self.output_valve1_device_id = None
        self.output_valve1_measurement_id = None
        self.output_valve1_channel_id = None
        self.output_valve1_channel = None
        self.output_valve2_device_id = None
        self.output_valve2_measurement_id = None
        self.output_valve2_channel_id = None
        self.output_valve2_channel = None
        self.output_max_time = None
        self.starting_tries = None
        self.filling_start = None
        self.max_flooding_time = None
        self.flooding_overshoot = None
        self.max_draining_time = None
        self.valve_cleaning_time = 0
        self.flooding_count = 0
        self.flooding_time = 0
        self.overshoot_time = 0
        self.flooded_high_time = 0
        self.flooding_volume = 0
        self.draining_time = 0
        self.low_water_time = 0
        self.cleaning_valves = 0
        self.error_count = 0
        self.error_message = ""
        self.last_force_measurement_time = { }
        # Set custom options
        custom_function = db_retrieve_table_daemon(
            CustomController, unique_id=self.unique_id)
        self.setup_custom_options(
            FUNCTION_INFORMATION['custom_options'], custom_function)

        if not testing:
            self.try_initialize()

    def initialize(self):

        self.output_waterpump_channel = self.get_output_channel_from_channel_id(self.output_waterpump_channel_id)
        if None not in [self.output_valve1_channel_id , self.output_valve1_device_id]:
            self.output_valve1_channel = self.get_output_channel_from_channel_id(self.output_valve1_channel_id)
        if None not in [self.output_valve2_channel_id , self.output_valve2_device_id]:
            self.output_valve2_channel = self.get_output_channel_from_channel_id(self.output_valve2_channel_id)

        self.logger.info(f"Ebb-Flood controller started with options: "
            f"Period: {self.period}, "
            f"Level Measurement Device: {self.measurement_flood_waterlevel_device_id}, "
            f"Level Measurement: {self.measurement_flood_waterlevel_measurement_id}, "
            f"Min-Level Measurement Device: {self.measurement_min_waterlevel_device_id}, "
            f"Min-Level Measurement: {self.measurement_min_waterlevel_measurement_id}, "
            f"Output Waterpump: {self.output_waterpump_device_id}, "
            f"Output_Waterpump_Channel: {self.output_waterpump_channel}, "
            f"Output Valve1: {self.output_valve1_device_id}, "
            f"Output_Valve1_Channel: {self.output_valve1_channel}, "
            f"Output Valve2: {self.output_valve2_device_id}, "
            f"Output_Valve2_Channel: {self.output_valve2_channel}, "
            f"Output_Max_Time: {self.output_max_time}, "
            f"Max flooding time: {self.max_flooding_time}, "
            f"Flooding overshoot: {self.flooding_overshoot} ")

        from transitions import Machine
        self.states = ['idle', 'starting', 'filling', 'full', 'draining', 'drained', 'error', 'shutdown']
        self.machine = Machine(model=self, states=self.states, initial='idle')
        self.machine.add_transition('start', 'idle', dest='starting' )
        self.machine.add_transition('starting', 'starting', dest='starting', after='on_starting' )
        self.machine.add_transition('filling', 'starting', dest='filling' )
        self.machine.add_transition('filling', 'filling', dest='filling', after='on_filling' )
        self.machine.add_transition('full', 'filling', dest='full', after='on_full' )
        self.machine.add_transition('drain', 'full', dest='draining' )
        self.machine.add_transition('draining', 'draining', dest='draining', after='on_draining' )
        self.machine.add_transition('drained', 'draining', dest='drained', after='on_drained' )
        self.machine.add_transition('error', '*', dest='error', after='on_error')
        self.machine.add_transition('shutdown', '*', dest='shutdown', after='on_shutdown')

        for channel_no, channel_obj in measurements_dict.items():
            last_measurement = read_influxdb_single(
                self.unique_id,
                channel_obj['unit'],
                channel_no,
                measure=channel_obj['measurement'],
                value='LAST')
            if (not last_measurement is None):
                self.logger.debug(f"read last_measurement for {channel_no} ({channel_obj}): {last_measurement}")
                match channel_no:
                    case 0:
                        self.flooding_count = last_measurement[1]
                    case 1:
                        self.error_count = last_measurement[1]
                        break

    def check_force_measurement(self, device_id):
        time_now = time.time()
        if (device_id in self.last_force_measurement_time):
            if (time_now - self.last_force_measurement_time[device_id]<1):
                self.logger.error(f"Ignoring check_force_measurement for {device_id}.")
                return
        self.control.input_force_measurements(device_id)
        self.last_force_measurement_time[device_id] = time_now 

    # read current flood_waterlevel state
    def read_flood_waterlevel(self):
        # force read of measurements
        self.check_force_measurement(self.measurement_flood_waterlevel_device_id)

        # get last measurement
        measurement = self.get_last_measurement(
            self.measurement_flood_waterlevel_device_id, self.measurement_flood_waterlevel_measurement_id, max_age=self.measurement_max_age)
        if not measurement:
            self.logger.warning(f"Could not acquire water level measurement: device_id: {self.measurement_flood_waterlevel_device_id}, measurement_id: {self.measurement_flood_waterlevel_measurement_id}")
            return None
        self.logger.debug(f"flood water level measurement is {measurement}, age={time.time()-measurement[0]}, max_age={self.measurement_max_age}")        
        if measurement[1] in [0, 0.0, False, 'off']:
            return 0
        if measurement[1] in [1, 1.0, True, 'on']: 
            return 1
        
        self.logger.error(f"Invalid water level measurement returned: {measurement}.")
        self.error()
        return None

    # read current basin_waterlevel state
    def read_basin_waterlevel(self):
        # force read of measurements
        self.check_force_measurement(self.measurement_flood_waterlevel_device_id)

        # get last measurement
        min_measurement = self.get_last_measurement(
            self.measurement_min_waterlevel_device_id, self.measurement_min_waterlevel_measurement_id, max_age=self.measurement_max_age)            
        if not min_measurement:
            self.logger.warning(f"Could not acquire a water min level measurement: device_id: {self.measurement_min_waterlevel_device_id}, measurement_id: {self.measurement_min_waterlevel_measurement_id}")
            return None
        self.logger.debug(f"Min Level measurement is {min_measurement}, max_age={self.measurement_max_age}, age={time.time()-min_measurement[0]}")
        if min_measurement[1] in [0, 0.0, False, 'off']:
            return 0
        if min_measurement[1] in [1, 1.0, True, 'on']: 
            return 1
        
        self.logger.error(f"Invalid min level measurement returned: {min_measurement}.")
        self.error()
        return None

    def button_reset_errors(self, args_dict):
        self.logger.debug("executing button_reset_errors")
        # self.logger.debug("Button Reset Errors pressed!: {}".format(int(args_dict['button_one_value'])))
        # 'flooding_count', 'error_count', 'flooding_time', 'flooding_volume', 'draining_time' 
        measure_dict = copy.deepcopy(measurements_dict)
        measure_set0 = { 0: measure_dict[0], 1: measure_dict[1] }
        measure_set0[0]['value'] = 0
        measure_set0[1]['value'] = 0
        self.logger.debug(f"writing statistics {measure_set0}")
        add_measurements_influxdb(self.unique_id, measure_set0)
        return "Reset of error counter."

    def loop(self):

        # check timer loop
        if self.timer_loop > time.time():
            return
        while self.timer_loop < time.time():
            self.timer_loop += self.period
        
        # state handling in loop
        self.logger.debug(f"loop - machine is now in state {self.state}")
        match self.state:
            case 'idle':
                self.start()
            case 'starting':
                self.trigger(self.state)
            case 'filling':
                self.trigger(self.state)
            case 'draining':
                self.trigger(self.state)
            case 'error':
                self.set_controller_off()
                raise Exception("Invalid error state in ebb_flood_function.")

    
    def on_starting(self):
        self.logger.debug(f"state: now in on_starting (try #{self.starting_tries})")

        # reset flooding time
        self.flooding_time = 0
        self.low_water_time = 0

        # debug myself
        # for artef in dir(self): self.logger.debug(f"{artef}")

        # don't try more than three times to start
        self.starting_tries = 0 if self.starting_tries is None else self.starting_tries+1
        if (self.starting_tries > 3):
            self.logger.error(f"Too many tries to startup - giving up")
            self.error()
            return

        if None in [self.max_flooding_time, self.flooding_overshoot]:
            self.logger.error("max_flooding_time and/or flooding_overshoot not set: Check setup values.")
            self.error()
            return

        # check measurement_flood_waterlevel_device_id
        if None in [self.measurement_flood_waterlevel_device_id, self.measurement_flood_waterlevel_measurement_id]:
            self.logger.error("Cannot start ebb-flood controller: Check if output channel(s) set: measurement_flood_waterlevel.")
            self.error()
            return

        # check output_waterpump_channel
        if (self.output_waterpump_channel is None):
            self.logger.error("Cannot start ebb-flood controller: Check if output channel(s) set: output_waterpump_channel.")
            self.error()
            return

        # --- measurement_flood_waterlevel 
        # resolve measurement_device
        self.measurement_flood_waterlevel_device = db_retrieve_table_daemon(
            Input, unique_id=self.measurement_flood_waterlevel_device_id, entry='first')
        if not self.measurement_flood_waterlevel_device:
            msg = f"Measurement device not found with ID {self.measurement_flood_waterlevel_device_id}"
            self.logger.error(msg)
            self.error()
            return

        # read current flood_waterlevel state
        waterlevel = self.read_flood_waterlevel()
        if waterlevel in [None, 1]:
            self.logger.error(f"Flood water level measurement is invalid or non-zero: {waterlevel} - trying again")
            return 

        # --- measurement_min_waterlevel 
        if not (self.measurement_min_waterlevel_measurement_id is None):
            # resolve measurement_device
            self.measurement_min_waterlevel_device = db_retrieve_table_daemon(
                Input, unique_id=self.measurement_min_waterlevel_device_id, entry='first')
            if not self.measurement_min_waterlevel_device:
                msg = f"Min waterlevel measurement device with ID {self.measurement_min_waterlevel_device_id} not found."
                self.logger.error(msg)
                self.error()
                return

            basin_waterlevel = self.read_basin_waterlevel()
            if basin_waterlevel is None:
                return
            if basin_waterlevel == 0:
                self.logger.error(f"Min water level measurement is null: trying again")
                return
            
        # we are good to go
        
        # turn on the pump
        self.logger.debug("Waterpump: On")
        self.control.output_on(
            self.output_waterpump_device_id, output_channel=self.output_waterpump_channel, amount=self.output_max_time)
        if not self.output_valve1_channel is None:
            self.control.output_on(
                self.output_valve1_device_id, output_channel=self.output_valve1_channel, amount=self.output_max_time)
        if not self.output_valve2_channel is None:
            self.control.output_on(
                self.output_valve2_device_id, output_channel=self.output_valve2_channel, amount=self.output_max_time)
        
        self.fill_start_timestamp = time.time()
        self.flooding_time = 0
        self.overshoot_time = 0
        self.flooded_high_time = 0
        self.cleaning_valves = 1
        self.filling()

    def on_filling(self):
        self.flooding_time = time.time() - self.fill_start_timestamp
        self.logger.debug(f"state: now in on_filling, flooding_time: {self.flooding_time}, overshoot_time: {self.overshoot_time}")

        if (self.flooding_time > self.max_flooding_time):
            self.logger.error(f"Filling took too long - giving up")
            self.error()
            return

        # check status of waterpump (did somebody turned it off?)
        output_waterpump_state = self.control.output_state(
            self.output_waterpump_device_id, self.output_waterpump_channel)
        if not output_waterpump_state:
            self.logger.error(f"Could not acquire an output measurement: device_id: {self.output_waterpump_device_id}, channel: {self.output_waterpump_channel}")
            self.error()
            return
        if output_waterpump_state not in [1, 1.0, True, 'on']:
            self.logger.error(f"Invalid measurement from waterpump: {output_waterpump_state}. Giving up.")
            self.error()
            return
        
        # cleanup valve1 in the first 20 sec
        if (self.cleaning_valves >= 1):
            if (self.flooding_time < self.valve_cleaning_time):
                self.cleaning_valves = self.cleaning_valves + 1
                match (self.cleaning_valves % 4):
                    case 0:
                        self.logger.debug(f"cleaning valve: 1-")
                        if not self.output_valve1_channel is None:
                            self.control.output_on(
                                self.output_valve1_device_id, output_channel=self.output_valve1_channel, amount=self.output_max_time)
                    case 1:
                        self.logger.debug(f"cleaning valve: X-")
                        if not self.output_valve1_channel is None:
                            self.control.output_off(
                                self.output_valve1_device_id, output_channel=self.output_valve1_channel)
                    case 2:
                        self.logger.debug(f"cleaning valve: -2")
                        if not self.output_valve2_channel is None:
                            self.control.output_on(
                                self.output_valve2_device_id, output_channel=self.output_valve2_channel, amount=self.output_max_time)
                    case 3:
                        self.logger.debug(f"cleaning valve: -X")
                        if not self.output_valve2_channel is None:
                            self.control.output_off(
                                self.output_valve2_device_id, output_channel=self.output_valve2_channel)
            else:
                self.cleaning_valves = 0
                if not self.output_valve1_channel is None:
                    self.control.output_on(
                        self.output_valve1_device_id, output_channel=self.output_valve1_channel, amount=self.output_max_time)
                if not self.output_valve2_channel is None:
                    self.control.output_on(
                        self.output_valve2_device_id, output_channel=self.output_valve2_channel, amount=self.output_max_time)

        if (not (self.measurement_min_waterlevel_measurement_id is None)) and (self.low_water_time == 0):
            # resolve measurement_device
            basin_waterlevel = self.read_basin_waterlevel()
            if basin_waterlevel == 0:
                self.low_water_time = time.time() - self.fill_start_timestamp;
                self.logger.debug(f"Min water level detected after {self.low_water_time} seconds.")

        # read current flood_waterlevel state
        waterlevel = self.read_flood_waterlevel()
        if waterlevel in [None]:
            self.logger.error(f"Invalid measurement for water level: {waterlevel}. Giving up.")
            self.error()
        if waterlevel in [None, 0]:
            return
        
        # sensor is on now
        if ((self.flooded_high_time is None) or (self.flooded_high_time == 0)):
            self.flooded_high_time = time.time()
            self.logger.debug(f"Set flooded_high_time to {self.flooded_high_time}.")

        # check overshoot period
        self.overshoot_time = time.time() - self.flooded_high_time
        if (self.overshoot_time < self.flooding_overshoot):
            self.logger.debug(f"Sill in overshoot period: {self.overshoot_time} of {self.flooding_overshoot}.")
            return
        
        # we are full
        self.full()

    def on_full(self):
        self.logger.debug(f"state: now in on_full")
        if (self.low_water_time == 0):
            self.low_water_time = time.time() - self.fill_start_timestamp;
            self.logger.debug(f"Min water level detected after {self.low_water_time} seconds (in state full).")
        self.flooding_count = self.flooding_count + 1
        self.all_outputs_off()
        self.draining_start_timestamp = time.time()
        self.drain()

    def on_draining(self):
        self.draining_time = time.time() - self.draining_start_timestamp
        self.logger.debug(f"state: now in on_draining")
        
        # check if we reached maximum drain time
        if (self.draining_time > self.max_draining_time):
            self.logger.error(f"Reached maximum drain time: {self.draining_time}. Giving up.")
            self.error()
            return 
        
        # read current flood_waterlevel state
        waterlevel = self.read_flood_waterlevel()
        if waterlevel in [None]:
            self.logger.error(f"Invalid measurement for water level: {waterlevel}. Giving up.")
            self.error()
            return
        if waterlevel in [0]:
            self.drained()
            return
        
    def on_drained(self):
        self.logger.debug(f"state: now in on_drained")
        self.write_statistics()
        self.set_controller_off()

    def on_error(self):
        self.logger.debug(f"state: now in on_error")
        self.error_count = self.error_count + 1
        self.write_statistics()
        self.set_controller_off()

    def on_shutdown(self):
        self.logger.debug(f"state: now in on_shutdown")
        self.all_outputs_off()

    def set_controller_off(self):
        controller_id = self.unique_id

        self.logger.debug(f"Finding controller with ID {controller_id}")
        (controller_type, controller_object, controller_entry) = which_controller(controller_id)

        if not controller_entry:
            self.logger.error(f"Error: Controller with ID '{controller_id}' not found.")
            return

        self.logger.debug(f"Deactivate Controller {controller_id} ({controller_entry.name}).")

        if not controller_entry.is_activated:
            self.logger.debug(f"Notice: Controller {controller_id} ({controller_entry.name}) is already not active!")
        else:
            with session_scope(MYCODO_DB_PATH) as new_session:
                mod_cont = new_session.query(controller_object).filter(
                    controller_object.unique_id == controller_id).first()
                mod_cont.is_activated = False
                new_session.commit()
            activate_controller = threading.Thread(
                target=self.control.controller_deactivate,
                args=(controller_id,))
            activate_controller.start()

        self.logger.debug(f"Deactivating controller {controller_id} ({controller_entry.name}) via separated thread.")
        return
    
    def stop_function(self):
        self.all_outputs_off()
        self.shutdown()

    def all_outputs_off(self):
        self.logger.debug(f"Turining off water pump.")
        self.control.output_off(
            self.output_waterpump_device_id, output_channel=self.output_waterpump_channel)
        if not self.output_valve1_channel is None:
            self.logger.debug(f"Turining off valve 1.")
            self.control.output_off(
                self.output_valve1_device_id, output_channel=self.output_valve1_channel)
        if not self.output_valve2_channel is None:
            self.logger.debug(f"Turining off valve 2.")
            self.control.output_off(
                self.output_valve2_device_id, output_channel=self.output_valve2_channel)

    def write_statistics(self):
        # 'flooding_count', 'error_count', 'flooding_time', 'flooding_volume', 'draining_time' 
        measure_dict = copy.deepcopy(measurements_dict)
        measure_dict[0]['value'] = self.flooding_count
        measure_dict[1]['value'] = self.error_count
        measure_dict[2]['value'] = self.flooding_time
        measure_dict[3]['value'] = self.flooding_volume
        measure_dict[4]['value'] = self.draining_time
        measure_dict[5]['value'] = self.low_water_time
        self.logger.debug(f"writing statistics {measure_dict}")
        add_measurements_influxdb(self.unique_id, measure_dict)

