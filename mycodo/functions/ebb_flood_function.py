# coding=utf-8
#
#  bang_bang_on_off.py - A hysteretic control for On/Off Outputs
import time
import logging
import threading
import datetime

from flask_babel import lazy_gettext

from mycodo.databases.models import Input
from mycodo.databases.models import CustomController
from mycodo.functions.base_function import AbstractFunction
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.constraints_pass import constraints_pass_positive_value
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.actions import which_controller
from mycodo.databases.utils import session_scope
from mycodo.config import MYCODO_DB_PATH

FUNCTION_INFORMATION = {
    'function_name_unique': 'ebb_flood_irrigation',
    'function_name': 'Ebb-Flood irrigation',
    'function_name_short': 'Ebb-Flood irrigation',

    'message': 'A simple ebb-flood control for controlling one outputs from one input.'
        ' Note: This output will only work with On/Off Outputs.',

    'dependencies_module': [
        ('pip-pypi', 'transitions', 'transitions==0.6.4'),
    ],

    'options_disabled': [
        'measurements_select',
        'measurements_configure'
    ],

    'custom_options': [
        {
            'id': 'measurement',
            'type': 'select_measurement',
            'default_value': '',
            'required': True,
            'options_select': [
                'Input',
                'Function'
            ],
            'name': lazy_gettext('Measurement'),
            'phrase': lazy_gettext('Select a measurement the selected output will affect')
        },
        {
            'id': 'measurement_max_age',
            'type': 'integer',
            'default_value': 2,
            'required': True,
            'name': "{}: {} ({})".format(lazy_gettext("Measurement"), lazy_gettext("Max Age"),
                                         lazy_gettext("Seconds")),
            'phrase': lazy_gettext('The maximum age of the measurement to use')
        },
        {
            'id': 'output_raise',
            'type': 'select_measurement_channel',
            'default_value': '',
            'required': True,
            'options_select': [
                'Output_Channels_Measurements',
            ],
            'name': 'Output (Raise)',
            'phrase': 'Select an output to control that will raise the measurement'
        },
        {
            'id': 'output_max_time',
            'type': 'float',
            'default_value': 90,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': lazy_gettext('Output maximal on-time'),
            'phrase': lazy_gettext('The maximum time to turn the output on')
        },
        {
            'id': 'max_filling_time',
            'type': 'float',
            'default_value': 95,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': "{} ({})".format(lazy_gettext('max filling time'), lazy_gettext('Seconds')),
            'phrase': lazy_gettext('The max duration to fill')
        },
        {
            'id': 'update_period',
            'type': 'float',
            'default_value': 1,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': "{} ({})".format(lazy_gettext('Period'), lazy_gettext('Seconds')),
            'phrase': lazy_gettext('The duration between measurements or actions')
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
        self.measurement_device_id = None
        self.measurement_device = None
        self.measurement_measurement_id = None
        self.measurement_max_age = None
        self.output_raise_device_id = None
        self.output_raise_measurement_id = None
        self.output_raise_channel_id = None
        self.output_raise_channel = None
        self.update_period = None
        self.output_max_time = None
        self.starting_tries = None
        self.filling_start = None
        self.filling_time = None
        self.max_filling_time = None

        # Set custom options
        custom_function = db_retrieve_table_daemon(
            CustomController, unique_id=self.unique_id)
        self.setup_custom_options(
            FUNCTION_INFORMATION['custom_options'], custom_function)

        if not testing:
            self.try_initialize()

    def initialize(self):

        self.output_raise_channel = self.get_output_channel_from_channel_id(
            self.output_raise_channel_id)

        self.logger.info(
            "Ebb-Flood controller started with options: "
            "Measurement Device: {}, Measurement: {}, "
            "Output Raise: {}, Output_Raise_Channel: {}, Output_Max_Time: {}, "
            "Period: {}".format(
                self.measurement_device_id,
                self.measurement_measurement_id,
                self.output_raise_device_id,
                self.output_raise_channel,
                self.output_max_time,
                self.update_period))

        from transitions import Machine
        logging.getLogger('transitions').setLevel(logging.INFO) 
        self.states = ['idle', 'starting', 'filling', 'filled', 'error', 'shutdown']
        self.machine = Machine(model=self, states=self.states, initial='idle')
        self.machine.add_transition('start', 'idle', dest='starting' )
        self.machine.add_transition('starting', 'starting', dest='starting', after='on_starting' )
        self.machine.add_transition('filling', 'starting', dest='filling' )
        self.machine.add_transition('filling', 'filling', dest='filling', after='on_filling' )
        self.machine.add_transition('filled', 'filling', dest='filled', after='on_filled' )
        self.machine.add_transition('error', '*', dest='error', after='on_error')
        self.machine.add_transition('shutdown', '*', dest='shutdown', after='on_shutdown')

    def loop(self):

        # check timer loop
        if self.timer_loop > time.time():
            return
        while self.timer_loop < time.time():
            self.timer_loop += self.update_period
        
        # state handling in loop
        self.logger.debug(f"loop - machine is now in state {self.state}")
        match self.state:
            case 'idle':
                self.start()
            case 'error':
                pass
            case 'shutdown':
                pass
            case _:
                self.trigger(self.state)
    
    def on_starting(self):
        self.logger.debug(f"state: now in on_starting (try #{self.starting_tries})")

        # debug myself
        # for artef in dir(self): self.logger.debug(f"{artef}")

        # don't try more than three times to start
        self.starting_tries = 0 if self.starting_tries is None else self.starting_tries+1
        if (self.starting_tries > 3):
            self.logger.debug(f"Too many tries to startup - giving up")
            self.error()
            return

        if (self.max_filling_time is None):
            self.logger.error("max_filling_time not set: Check setup values.")
            self.error()
            return

        # check output_raise_channel
        if (self.output_raise_channel is None):
            self.logger.error("Cannot start ebb-flood controller: Check output channel(s).")
            self.error()
            return

        # resolve measurement_device
        self.measurement_device = db_retrieve_table_daemon(
            Input, unique_id=self.measurement_device_id, entry='first')
        if not self.measurement_device:
            msg = f"Measurement device not found with ID {self.measurement_device_id}"
            self.logger.error(msg)
            self.error()
            return

        # read current state
        self.control.input_force_measurements(self.measurement_device_id)
        time.sleep(0.1)
        measurement = self.get_last_measurement(
            self.measurement_device_id, self.measurement_measurement_id) # max_age=self.measurement_max_age)
        self.logger.debug(f"Measurement is {measurement}, max_age={self.measurement_max_age}")
        
        if not measurement:
            self.logger.error(f"Could not acquire a measurement: device_id: {self.measurement_device_id}, measurement_id: {self.measurement_measurement_id}")
            self.error()
            return

        if not measurement[1] == 0:
            self.logger.error(f"Measurement is non-zero: {measurement}")
            self.error()
            return

        # turn on the pump
        self.logger.debug("Raise: On")
        self.control.output_on(
            self.output_raise_device_id, output_channel=self.output_raise_channel)
        
        self.fill_start_timestamp = time.time()
        self.filling()

    def on_filling(self):
        self.filling_time = time.time() - self.fill_start_timestamp
        self.logger.debug(f"state: now in on_filling, filling_time {self.filling_time}")

        if (self.filling_time > self.max_filling_time):
            self.logger.debug(f"Filling took too long - giving up")
            self.error()
            return

        # read current state
        self.control.input_force_measurements(self.measurement_device_id)
        time.sleep(0.5)
        timestamp = datetime.datetime.utcnow()
        measurement = self.get_last_measurement(
            self.measurement_device_id, self.measurement_measurement_id) # max_age=self.measurement_max_age)
        self.logger.debug(f"Measurement is {measurement}, max_age={self.measurement_max_age}, age={time.time()-measurement[0]}")
        
        if not measurement:
            self.logger.error(f"Could not acquire a measurement: device_id: {self.measurement_device_id}, measurement_id: {self.measurement_measurement_id}")
            self.error()
            return
        if measurement[1] == 1:
            self.on_filled()
            return
        if measurement[1] != 0:
            self.logger.error(f"Invalid measurement returned: {measurement}. Giving up.")
            self.error()
            return
    
    def on_filled(self):
        self.logger.debug(f"state: now in on_filled")
        self.control.output_off(
            self.output_raise_device_id, output_channel=self.output_raise_channel)
        self.turn_off()

    def on_error(self):
        self.logger.debug(f"state: now in on_error")
        self.control.output_off(
            self.output_raise_device_id, output_channel=self.output_raise_channel)
        self.turn_off()

    def on_shutdown(self):
        self.logger.debug(f"state: now in on_shutdown")
        self.control.output_off(
            self.output_raise_device_id, output_channel=self.output_raise_channel)

    def turn_off(self):
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
    
    def something(self):
        self.control.input_force_measurements(self.measurement_device_id)

        last_measurement = self.get_last_measurement(
            self.measurement_device_id,
            self.measurement_measurement_id,
            max_age=self.measurement_max_age)

        if not last_measurement:
            self.logger.error("Could not acquire a measurement")
            return

        if last_measurement[1] >= 1:
            self.logger.debug("Raise: Off")
            self.control.output_off(
                self.output_raise_device_id, output_channel=self.output_raise_channel)
        elif last_measurement[1] <= 1:
            self.logger.debug("Raise: On")
            self.control.output_on(
                self.output_raise_device_id, output_channel=self.output_raise_channel)
        else:
            self.logger.error(
                "Unknown controller direction: '{}'".format(self.direction))

        output_raise_state = self.control.output_state(
            self.output_raise_device_id, self.output_raise_channel)

        self.logger.debug(
            f"Before execution: Input: {last_measurement[1]}, "
            f"output_raise: {output_raise_state}",
            f"output_max_time: {self.output_max_time}")

    def stop_function(self):
        self.shutdown()
        self.control.output_off(
            self.output_raise_device_id, output_channel=self.output_raise_channel)
