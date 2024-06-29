# coding=utf-8
import timeit
import datetime
from collections import OrderedDict

import copy
from flask_babel import lazy_gettext

from mycodo.config_translations import TRANSLATIONS
from mycodo.inputs.base_input import AbstractInput
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.databases.models import InputChannel

# Measurements
measurements_dict = OrderedDict()
channels_dict = OrderedDict()
for each_channel in range(8):
    measurements_dict[each_channel] = {
        'measurement': 'gpio_state',
        'unit': 'bool',
    }
    channels_dict[each_channel] = {
        'name': f'Channel {each_channel + 1}',
        'types': ['bool'],
        'measurements': [each_channel],
        'last_measurement': None,
        'update_mode': 'periodic',
        'update_count': 0
    }


# Input information
INPUT_INFORMATION = {
    'input_name_unique': 'PCF8574_8CH_IN',
    'input_manufacturer': 'Texas Instruments',
    'input_name': "PCF8574 8-Channel {}".format(lazy_gettext('I/O Expander')),
    'input_library': 'smbus2',
    'measurements_name': 'GPIO State',
    'measurements_dict': measurements_dict,
    'channels_dict': channels_dict,

    'options_enabled': [
        'measurements_select',
        'i2c_location',
        'period',
        'pre_output'
    ],

    'options_disabled': ['interface'],

    'dependencies_module': [
        ('pip-pypi', 'smbus2', 'smbus2==0.4.1')
    ],

    'interfaces': ['I2C'],
    'i2c_location': ['0x20', '0x21', '0x22', '0x23', '0x24', '0x25', '0x26', '0x27'],
    'i2c_address_editable': False,
    'i2c_address_default': '0x20',

    'custom_options': [
    ],

    'custom_channel_options': [
        {
            'id': 'name',
            'type': 'text',
            'default_value': '',
            'required': False,
            'name': TRANSLATIONS['name']['title'],
            'phrase': TRANSLATIONS['name']['phrase']
        },
        {
            'id': 'update_mode',
            'type': 'select',
            'default_value': 'periodic',
            'options_select': [
                ('periodic', 'Periodic'),
                ('on_change', 'On change'),
                ('periodic_on_change', 'Periodic and on change')
            ],
            'name': 'Update mode',
            'phrase': 'Sets the channel update mode'
        }
    ]
}


class InputModule(AbstractInput):
    """A sensor support class that monitors the K30's CO2 concentration."""

    def __init__(self, input_dev, testing=False):
        super().__init__(input_dev, testing=testing, name=__name__)

        self.device = None
        input_channels = db_retrieve_table_daemon(
            InputChannel).filter(InputChannel.input_id == input_dev.unique_id).all()

        if not testing:
            self.setup_custom_options(
                INPUT_INFORMATION['custom_options'], input_dev)
            self.options_channels = self.setup_custom_channel_options_json(
                INPUT_INFORMATION['custom_channel_options'], input_channels)
            self.try_initialize()

    def initialize(self):
        import smbus2

        try:
            self.logger.debug(f"I2C: Address: {self.input_dev.i2c_location}, Bus: {self.input_dev.i2c_bus}")
            if self.input_dev.i2c_location:
                self.device = PCF8574(smbus2, self.input_dev.i2c_bus, int(str(self.input_dev.i2c_location), 16))
                self.input_setup = True
        except:
            self.logger.exception("Could not set up input")
            return
        
        for channel in channels_dict:
            if self.options_channels['update_mode'][channel]:
                channels_dict[channel]['update_mode'] = self.options_channels['update_mode'][channel]

    def get_measurement(self):
        """Gets the GPIO state from the device."""
        self.return_dict = copy.deepcopy(measurements_dict)

        self.device.read_state()
        timestamp = datetime.datetime.utcnow()
        for channel in self.channels_measurement:
            if self.is_enabled(channel):
                last_measurement = None
                update_mode = 'periodic'
                update_count = 0
                if channel in channels_dict:
                    last_measurement = channels_dict[channel]['last_measurement']
                    update_mode = channels_dict[channel]['update_mode']
                    update_count = channels_dict[channel]['update_count']
                port_value = self.device.read_pin_from_state(channel)
                self.logger.debug(f"Read Channel: {channel}, last_measurement: {last_measurement}, port_value: {port_value}, update_mode: {update_mode}, update_count: {update_count}")
                do_set_value = False
                match update_mode:
                    case 'periodic':
                        do_set_value = True
                    case 'on_change':
                        do_set_value = bool(port_value != last_measurement)
                    case 'periodic_on_change':
                        do_set_value = bool((port_value != last_measurement) or (update_count >= 60))
                    case _:
                        do_set_value = True
                if (do_set_value):
                    self.value_set(channel, port_value, timestamp=timestamp)
                    update_count = 0
                    channels_dict[channel]['last_measurement'] = port_value
                    self.logger.debug(f"value_set: {channel}, Value: {port_value}")
                update_count = update_count + 1
                channels_dict[channel]['update_count'] = update_count
        
        return self.return_dict

    def stop_input(self):
        pass

class PCF8574:
    """
    A software representation of a single PCF8574 IO expander chip.
    """

    def __init__(self, smbus, i2c_bus, i2c_address):
        self.bus_no = i2c_bus
        self.bus = smbus.SMBus(i2c_bus)
        self.address = i2c_address

    def __repr__(self):
        # type: () -> str
        return "PCF8574(i2c_bus_no=%r, address=0x%02x)" % (self.bus_no, self.address)

    @property
    def port(self):
        # type: () -> IOPort
        """
        Represent IO port as a list of boolean values.
        """
        return IOPort(self)

    @port.setter
    def port(self, value):
        # type: (List[bool]) -> None
        """
        Set the whole port using a list.
        """
        if not isinstance(value, list):
            raise AssertionError
        if len(value) != 8:
            raise AssertionError
        new_state = 0
        for i, val in enumerate(value):
            if val:
                new_state |= 1 << i
        self.bus.write_byte(self.address, new_state)

    def set_output(self, output_number, value):
        # type: (int, bool) -> None
        """
        Set a specific output high (True) or low (False).
        """
        assert output_number in range(
            8
        ), "Output number must be an integer between 0 and 7"
        current_state = self.bus.read_byte(self.address)
        bit = 1 << output_number
        new_state = current_state | bit if value else current_state & (~bit & 0xFF)
        self.bus.write_byte(self.address, new_state)

    def get_pin_state(self, pin_number):
        # type: (int) -> bool
        """
        Get the boolean state of an individual pin.
        """
        assert pin_number in range(8), "Pin number must be an integer between 0 and 7"
        state = self.bus.read_byte(self.address)
        return bool(state & 1 << pin_number)
    
    def read_state(self):
        # type: (int) -> None
        """
        Get the state of byte.
        """
        self.state = self.bus.read_byte(self.address)

    def read_pin_from_state(self, pin_number):
        # type: (int) -> bool
        """
        Get the boolean state of an individual pin.
        """
        assert not self.state is None
        assert pin_number in range(8), "Pin number must be an integer between 0 and 7"
        return bool(self.state & 1 << pin_number)
