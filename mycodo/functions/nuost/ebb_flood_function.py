# coding=utf-8
#
#  bang_bang_on_off.py - A hysteretic control for On/Off Outputs
import time

from flask_babel import lazy_gettext

from mycodo.databases.models import CustomController
from mycodo.functions.base_function import AbstractFunction
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.constraints_pass import constraints_pass_positive_value
from mycodo.utils.database import db_retrieve_table_daemon


FUNCTION_INFORMATION = {
    'function_name_unique': 'ebb_flood_irrigation',
    'function_name': 'Ebb-Flood irrigation',
    'function_name_short': 'Ebb-Flood irrigation',

    'message': 'A simple ebb-flood control for controlling one outputs from one input.'
        ' Note: This output will only work with On/Off Outputs.',

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
            'id': 'update_period',
            'type': 'float',
            'default_value': 3,
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
        self.measurement_measurement_id = None
        self.measurement_max_age = None
        self.output_raise_device_id = None
        self.output_raise_measurement_id = None
        self.output_raise_channel_id = None
        self.output_raise_channel = None
        self.update_period = None
        self.output_max_time = None

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

    def loop(self):
        if self.timer_loop > time.time():
            return

        while self.timer_loop < time.time():
            self.timer_loop += self.update_period

        if (self.output_raise_channel is None):
            self.logger.error("Cannot start ebb-flood controller: Check output channel(s).")
            return

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
        self.control.output_off(
            self.output_raise_device_id, output_channel=self.output_raise_channel)
