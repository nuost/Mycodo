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
from mycodo.utils.system_pi import return_measurement_info

measurements_dict = {
    0: {
        'measurement': '',
        'unit': 'none',
        'name': 'Flooding count',
    },
    1: {
        'measurement': '',
        'unit': 's',
        'name': 'Flooding time',
    },
    2: {
        'measurement': '',
        'unit': 'l',
        'name': 'Flooding volume',
    },
    3: {
        'measurement': '',
        'unit': 'none',
        'name': 'Error count',
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
            'type': 'new_line'
        },
        {
            'id': 'measurement_waterlevel',
            'type': 'select_measurement',
            'default_value': '',
            'required': True,
            'options_select': [
                'Input',
                'Function'
            ],
            'name': lazy_gettext('Level Measurement'),
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
            'type': 'new_line'
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
            'id': 'max_flooding_time',
            'type': 'float',
            'default_value': 120,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': "{} ({})".format(lazy_gettext('max filling time'), lazy_gettext('Seconds')),
            'phrase': lazy_gettext('The max duration to fill')
        }
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
        self.measurement_waterlevel_device_id = None
        self.measurement_waterlevel_device = None
        self.measurement_waterlevel_measurement_id = None
        self.measurement_waterlevel_max_age = None
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
        self.flooding_count = 0
        self.flooding_time = 0
        self.flooding_volume = 0
        self.error_count = 0

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

        self.logger.info(
            "Ebb-Flood controller started with options: "
            "Level Measurement Device: {}, Level Measurement: {}, "
            "Output Waterpump: {}, Output_Waterpump_Channel: {}, "
            "Output Valve1: {}, Output_Valve1_Channel: {}, "
            "Output Valve2: {}, Output_Valve2_Channel: {}, "
            "Output_Max_Time: {}, Period: {}".format(
                self.measurement_waterlevel_device_id,
                self.measurement_waterlevel_measurement_id,
                self.output_waterpump_device_id,
                self.output_waterpump_channel,
                self.output_valve1_device_id,
                self.output_valve1_channel,
                self.output_valve2_device_id,
                self.output_valve2_channel,
                self.output_max_time,
                self.period))

        from transitions import Machine
        self.states = ['idle', 'starting', 'filling', 'filled', 'error', 'shutdown']
        self.machine = Machine(model=self, states=self.states, initial='idle')
        self.machine.add_transition('start', 'idle', dest='starting' )
        self.machine.add_transition('starting', 'starting', dest='starting', after='on_starting' )
        self.machine.add_transition('filling', 'starting', dest='filling' )
        self.machine.add_transition('filling', 'filling', dest='filling', after='on_filling' )
        self.machine.add_transition('filled', 'filling', dest='filled', after='on_filled' )
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
                    case 3:
                        self.error_count = last_measurement[1]

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
    
    def on_starting(self):
        self.logger.debug(f"state: now in on_starting (try #{self.starting_tries})")

        # reset flooding time
        self.flooding_time = 0

        # debug myself
        # for artef in dir(self): self.logger.debug(f"{artef}")

        # don't try more than three times to start
        self.starting_tries = 0 if self.starting_tries is None else self.starting_tries+1
        if (self.starting_tries > 3):
            self.logger.error(f"Too many tries to startup - giving up")
            self.error()
            return

        if (self.max_flooding_time is None):
            self.logger.error("max_flooding_time not set: Check setup values.")
            self.error()
            return

        # check output_waterpump_channel
        if (self.output_waterpump_channel is None):
            self.logger.error("Cannot start ebb-flood controller: Check output channel(s).")
            self.error()
            return

        # resolve measurement_device
        self.measurement_waterlevel_device = db_retrieve_table_daemon(
            Input, unique_id=self.measurement_waterlevel_device_id, entry='first')
        if not self.measurement_waterlevel_device:
            msg = f"Measurement device not found with ID {self.measurement_waterlevel_device_id}"
            self.logger.error(msg)
            self.error()
            return

        # read current state
        self.control.input_force_measurements(self.measurement_waterlevel_device_id)
        time.sleep(0.1)
        measurement = self.get_last_measurement(
            self.measurement_waterlevel_device_id, self.measurement_waterlevel_measurement_id, max_age=self.measurement_waterlevel_max_age)
        self.logger.debug(f"Measurement is {measurement}, max_age={self.measurement_waterlevel_max_age}, age={time.time()-measurement[0]}")
        
        if not measurement:
            self.logger.error(f"Could not acquire a measurement: device_id: {self.measurement_waterlevel_device_id}, measurement_id: {self.measurement_waterlevel_measurement_id}")
            self.error()
            return

        if measurement[1] not in [0, 0.0, False, 'off']:
            self.logger.error(f"Measurement is non-zero: {measurement} - trying again")
            return

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
        self.filling()

    def on_filling(self):
        self.flooding_time = time.time() - self.fill_start_timestamp
        self.logger.debug(f"state: now in on_filling, flooding_time {self.flooding_time}")

        if (self.flooding_time > self.max_flooding_time):
            self.logger.error(f"Filling took too long - giving up")
            self.error()
            return

        # read current state
        self.control.input_force_measurements(self.measurement_waterlevel_device_id)
        time.sleep(0.1)
        measurement = self.get_last_measurement(
            self.measurement_waterlevel_device_id, self.measurement_waterlevel_measurement_id, max_age=self.measurement_waterlevel_max_age)
        self.logger.debug(f"Measurement is {measurement}, max_age={self.measurement_waterlevel_max_age}, age={time.time()-measurement[0]}")
        
        if not measurement:
            self.logger.error(f"Could not acquire a measurement: device_id: {self.measurement_waterlevel_device_id}, measurement_id: {self.measurement_waterlevel_measurement_id}")
            self.error()
            return
        if measurement[1] in [1, 1.0, True, 'on']:
            self.on_filled()
            return
        if measurement[1] not in [0, 0.0, False, 'off']:
            self.logger.error(f"Invalid measurement returned: {measurement}. Giving up.")
            self.error()
            return
        
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

    def on_filled(self):
        self.logger.debug(f"state: now in on_filled")
        self.flooding_count = self.flooding_count + 1
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
        self.control.output_off(
            self.output_waterpump_device_id, output_channel=self.output_waterpump_channel)
        if not self.output_valve1_channel is None:
            self.control.output_off(
                self.output_valve1_device_id, output_channel=self.output_valve1_channel)
        if not self.output_valve2_channel is None:
            self.control.output_off(
                self.output_valve2_device_id, output_channel=self.output_valve2_channel)

    def write_statistics(self):
        # 'flooding_count', 'flooding_time', 'flooding_volume', 'error_count'
        measure_dict = copy.deepcopy(measurements_dict)
        measure_dict[0]['value'] = self.flooding_count
        measure_dict[1]['value'] = self.flooding_time
        measure_dict[2]['value'] = self.flooding_volume
        measure_dict[3]['value'] = self.error_count
        self.logger.debug(f"writing statistics {measure_dict}")
        add_measurements_influxdb(self.unique_id, measure_dict)

