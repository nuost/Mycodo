# coding=utf-8
#
#  function_pid_autotune.py - PID Controller Autotune
#
#  Copyright (C) 2020  Kyle T. Gabriel
#
#  This file is part of Mycodo
#
#  Mycodo is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Mycodo is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Mycodo. If not, see <http://www.gnu.org/licenses/>.
#
#  Contact at kylegabriel.com
#
import threading
import time
import timeit

from flask_babel import lazy_gettext

from mycodo.controllers.base_controller import AbstractController
from mycodo.databases.models import CustomController
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.PID_hirschmann.pid_autotune import PIDAutotune
from mycodo.utils.database import db_retrieve_table_daemon


def constraints_pass_positive_value(mod_controller, value):
    """
    Check if the user controller is acceptable
    :param mod_controller: SQL object with user-saved Input options
    :param value: float or int
    :return: tuple: (bool, list of strings)
    """
    errors = []
    all_passed = True
    # Ensure value is positive
    if value <= 0:
        all_passed = False
        errors.append("Must be a positive value")
    return all_passed, errors, mod_controller


FUNCTION_INFORMATION = {
    'function_name_unique': 'function_pid_autotune',
    'function_name': 'PID Controller Autotune',

    'message': 'This function will attempt to perform a PID controller autotune. That is, an output will be powered and the response measured from a sensor several times to calculate the P, I, and D gains. Updates about the operation will be sent to the Daemon log. If the autotune successfulyl completes, a summary will be sent to the Daemon log as well. Autotune is an experimental feature. It is not well-developed, and has a high likelihood of failing to successfully generate PID gains. Do not rely on it for accurately tuning your PID controller.',

    'options_enabled': [],
    'dependencies_module': [],

    'custom_options': [
        {
            'id': 'measurement',
            'type': 'select_measurement',
            'default_value': '',
            'required': True,
            'options_select': [
                'Input',
                'Math',
            ],
            'name': lazy_gettext('Measurement'),
            'phrase': lazy_gettext('Select a measurement the selected output will affect')
        },
        {
            'id': 'output',
            'type': 'select_device',
            'default_value': '',
            'required': True,
            'options_select': [
                'Output',
            ],
            'name': lazy_gettext('Output'),
            'phrase': lazy_gettext('Select an output to modulate that will affect the measurement')
        },
        {
            'id': 'period',
            'type': 'integer',
            'default_value': 30,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': lazy_gettext('Period'),
            'phrase': lazy_gettext('The period between powering the output')
        },
        {
            'id': 'setpoint',
            'type': 'float',
            'default_value': 50,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': lazy_gettext('Setpoint'),
            'phrase': lazy_gettext('A value sufficiently far from the current measured value that the output is capable of pushing the measurement toward')
        },
        {
            'id': 'noiseband',
            'type': 'float',
            'default_value': 0.5,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': lazy_gettext('Noise Band'),
            'phrase': lazy_gettext('The amount above the setpoint the measurement must reach')
        },
        {
            'id': 'outstep',
            'type': 'float',
            'default_value': 10,
            'required': True,
            'constraints_pass': constraints_pass_positive_value,
            'name': lazy_gettext('Outstep'),
            'phrase': lazy_gettext('How many seconds the output will turn on every Period')
        },
        {
            'type': 'message',
            'default_value': 'Currently, only autotuning to raise a condition (measurement) is supported.',
        },
        {
            'id': 'direction',
            'type': 'select',
            'default_value': 'raise',
            'options_select': [
                ('raise', 'Raise')
            ],
            'name': lazy_gettext('Direction'),
            'phrase': lazy_gettext('The direction the Output will push the Measurement')
        }
    ]
}


class CustomModule(AbstractController, threading.Thread):
    """
    Class to operate custom controller
    """
    def __init__(self, ready, unique_id, testing=False):
        threading.Thread.__init__(self)
        super(CustomModule, self).__init__(ready, unique_id=unique_id, name=__name__)

        self.unique_id = unique_id
        self.log_level_debug = None
        self.autotune = None
        self.autotune_active = None
        self.control_variable = None
        self.timestamp = None
        self.timer = None
        self.control = DaemonControl()

        # Initialize custom options
        self.measurement_device_id = None
        self.measurement_measurement_id = None
        self.output_id = None
        self.setpoint = None
        self.period = None
        self.noiseband = None
        self.outstep = None
        self.direction = None

        # Set custom options
        custom_function = db_retrieve_table_daemon(
            CustomController, unique_id=unique_id)
        self.setup_custom_options(
            FUNCTION_INFORMATION['custom_options'], custom_function)

        self.initialize_variables()

    def initialize_variables(self):
        controller = db_retrieve_table_daemon(
            CustomController, unique_id=self.unique_id)
        self.log_level_debug = controller.log_level_debug
        self.set_log_level_debug(self.log_level_debug)

        self.timestamp = time.time()
        self.autotune = PIDAutotune(
            self.setpoint,
            out_step=self.outstep,
            sampletime=self.period,
            out_min=0,
            out_max=self.period,
            noiseband=self.noiseband)

    def run(self):
        try:
            self.logger.info("Activated in {:.1f} ms".format(
                (timeit.default_timer() - self.thread_startup_timer) * 1000))

            self.ready.set()
            self.running = True
            self.autotune_active = True
            self.timer = time.time()

            self.logger.info(
                "PID Autotune started with options: "
                "Measurement Device: {}, Measurement: {}, Output: {}, Setpoint: {}, "
                "Period: {}, Noise Band: {}, Outstep: {}, DIrection: {}".format(
                    self.measurement_device_id,
                    self.measurement_measurement_id,
                    self.output_id,
                    self.setpoint,
                    self.period,
                    self.noiseband,
                    self.outstep,
                    self.direction))

            # Start a loop
            while self.running:
                self.loop()
                time.sleep(0.1)
        except:
            self.logger.exception("Run Error")
        finally:
            self.run_finally()
            self.running = False
            if self.thread_shutdown_timer:
                self.logger.info("Deactivated in {:.1f} ms".format(
                    (timeit.default_timer() - self.thread_shutdown_timer) * 1000))
            else:
                self.logger.error("Deactivated unexpectedly")

    def loop(self):
        if time.time() > self.timer and self.autotune_active:
            while time.time() > self.timer:
                self.timer = self.timer + self.period

            last_measurement = self.get_last_measurement(
                self.measurement_device_id,
                self.measurement_measurement_id)

            if not self.autotune.run(last_measurement[1]):
                self.control_variable = self.autotune.output

                self.logger.info('')
                self.logger.info("state: {}".format(self.autotune.state))
                self.logger.info("output: {}".format(self.autotune.output))
            else:
                # Autotune has finished
                timestamp = time.time() - self.timestamp
                self.autotune_active = False
                self.logger.info('Autotune has finished')
                self.logger.info('time:  {0} min'.format(round(timestamp / 60)))
                self.logger.info('state: {0}'.format(self.autotune.state))

                if self.autotune.state == PIDAutotune.STATE_SUCCEEDED:
                    self.logger.info('Autotube was successful')
                    for rule in self.autotune.tuning_rules:
                        params = self.autotune.get_pid_parameters(rule)
                        self.logger.info('')
                        self.logger.info('rule: {0}'.format(rule))
                        self.logger.info('Kp: {0}'.format(params.Kp))
                        self.logger.info('Ki: {0}'.format(params.Ki))
                        self.logger.info('Kd: {0}'.format(params.Kd))
                else:
                    self.logger.info('Autotune was not successful')

                # Finally, deactivate controller
                self.deactivate_self()
                return

            self.control.output_on(
                self.output_id, output_type='sec', amount=self.control_variable)

    def deactivate_self(self):
        self.logger.info("Deactivating Autotune Function")

        from mycodo.databases.utils import session_scope
        from mycodo.config import SQL_DATABASE_MYCODO
        MYCODO_DB_PATH = 'sqlite:///' + SQL_DATABASE_MYCODO
        with session_scope(MYCODO_DB_PATH) as new_session:
            mod_cont = new_session.query(CustomController).filter(
                CustomController.unique_id == self.unique_id).first()
            mod_cont.is_activated = False
            new_session.commit()

        deactivate_controller = threading.Thread(
            target=self.control.controller_deactivate,
            args=(self.unique_id,))
        deactivate_controller.start()