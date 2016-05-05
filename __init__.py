"""
Copyright 2015 Ryan Fobel

This file is part of opendrop_plugin.

opendrop_plugin is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

opendrop_plugin is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with opendrop_plugin.  If not, see <http://www.gnu.org/licenses/>.
"""
import os
import math
import re
from copy import deepcopy
import warnings

import tables
import json
from datetime import datetime
from pygtkhelpers.ui.dialogs import info as info_dialog
import yaml
import gtk
import gobject
import numpy as np
from path_helpers import path
from flatland import Integer, Boolean, Float, Form, Enum, String
from flatland.validation import ValueAtLeast, ValueAtMost, Validator
import microdrop_utility as utility
from microdrop_utility.gui import yesno, FormViewDialog
from microdrop.logger import logger
from microdrop.gui.protocol_grid_controller import ProtocolGridController
from microdrop.plugin_helpers import (StepOptionsController, AppDataController,
                                      get_plugin_info)
from microdrop.plugin_manager import (IPlugin, IWaveformGenerator, Plugin,
                                      implements, PluginGlobals,
                                      ScheduleRequest, emit_signal,
                                      get_service_instance,
                                      get_service_instance_by_name)
from microdrop.app_context import get_app, get_hub_uri
from microdrop.dmf_device import DeviceScaleNotSet
from serial_device import SerialDevice, get_serial_ports
from dropbot_dx import SerialProxy
from zmq_plugin.plugin import Plugin as ZmqPlugin
from zmq_plugin.schema import decode_content_data
import zmq
import gobject
import pandas as pd

# Ignore natural name warnings from PyTables [1].
#
# [1]: https://www.mail-archive.com/pytables-users@lists.sourceforge.net/msg01130.html
warnings.simplefilter('ignore', tables.NaturalNameWarning)

PluginGlobals.push_env('microdrop.managed')


class DmfZmqPlugin(ZmqPlugin):
    '''
    API for adding/clearing droplet routes.
    '''
    def __init__(self, parent, *args, **kwargs):
        self.parent = parent
        super(DmfZmqPlugin, self).__init__(*args, **kwargs)

    def check_sockets(self):
        '''
        Check for messages on command and subscription sockets and process
        any messages accordingly.
        '''
        try:
            msg_frames = self.command_socket.recv_multipart(zmq.NOBLOCK)
        except zmq.Again:
            pass
        else:
            self.on_command_recv(msg_frames)

        try:
            msg_frames = self.subscribe_socket.recv_multipart(zmq.NOBLOCK)
            source, target, msg_type, msg_json = msg_frames
            if ((source == 'wheelerlab.electrode_controller_plugin') and
                (msg_type == 'execute_reply')):
                # The 'wheelerlab.electrode_controller_plugin' plugin maintains
                # the requested state of each electrode.
                msg = json.loads(msg_json)
                if msg['content']['command'] in ('set_electrode_state',
                                                 'set_electrode_states'):
                    data = decode_content_data(msg)
                    self.parent.actuated_area = data['actuated_area']
                    self.parent.update_channel_states(data['channel_states'])
                elif msg['content']['command'] == 'get_channel_states':
                    data = decode_content_data(msg)
                    self.parent.actuated_area = data['actuated_area']
                    self.parent.channel_states = self.parent.channel_states.iloc[0:0]
                    self.parent.update_channel_states(data['channel_states'])
            else:
                self.most_recent = msg_json
        except zmq.Again:
            pass
        except:
            logger.error('Error processing message from subscription '
                            'socket.', exc_info=True)
        return True


def max_voltage(element, state):
    """Verify that the voltage is below a set maximum"""
    service = get_service_instance_by_name(
        get_plugin_info(path(__file__).parent).plugin_name)

    if service.control_board and \
        element.value > service.control_board.max_waveform_voltage:
        return element.errors.append('Voltage exceeds the maximum value '
                                     '(%d V).' %
                                     service.control_board.max_waveform_voltage)
    else:
        return True


def check_frequency(element, state):
    """Verify that the frequency is within the valid range"""
    service = get_service_instance_by_name(
        get_plugin_info(path(__file__).parent).plugin_name)

    if service.control_board and \
        (element.value < service.control_board.min_waveform_frequency or \
        element.value > service.control_board.max_waveform_frequency):
        return element.errors.append('Frequency is outside of the valid range '
            '(%.1f - %.1f Hz).' %
            (service.control_board.min_waveform_frequency,
             service.control_board.max_waveform_frequency)
        )
    else:
        return True


class DropBotDxPlugin(Plugin, StepOptionsController, AppDataController):
    """
    This class is automatically registered with the PluginManager.
    """
    implements(IPlugin)
    implements(IWaveformGenerator)

    serial_ports_ = [port for port in get_serial_ports()]
    if len(serial_ports_):
        default_port_ = serial_ports_[0]
    else:
        default_port_ = None

    AppFields = Form.of(
        Enum.named('serial_port').using(default=default_port_,
                                        optional=True).valued(*serial_ports_),
    )

    StepFields = Form.of(
        Integer.named('duration').using(default=100, optional=True,
                                        validators=
                                        [ValueAtLeast(minimum=0), ]),
        Float.named('voltage').using(default=100, optional=True,
                                     validators=[ValueAtLeast(minimum=0),
                                                 max_voltage]),
        Float.named('frequency').using(default=10e3, optional=True,
                                       validators=[ValueAtLeast(minimum=0),
                                                   check_frequency]),
    )

    version = get_plugin_info(path(__file__).parent).version

    def __init__(self):
        self.control_board = None
        self.name = get_plugin_info(path(__file__).parent).plugin_name
        self.connection_status = "Not connected"
        self.current_frequency = None
        self.timeout_id = None
        self.channel_states = pd.Series()
        self.plugin = None
        self.plugin_timeout_id = None

    def update_channel_states(self, channel_states):
        # Update locally cached channel states with new modified states.
        try:
            self.channel_states = channel_states.combine_first(self
                                                               .channel_states)
        except ValueError:
            logging.info('channel_states: %s', channel_states)
            logging.info('self.channel_states: %s', self.channel_states)
            logging.info('', exc_info=True)
        else:
            app = get_app()
            connected = self.control_board != None
            if connected and (app.realtime_mode or
                              app.running):
                self.on_step_run()

    def cleanup_plugin(self):
        if self.plugin_timeout_id is not None:
            gobject.source_remove(self.plugin_timeout_id)
        if self.plugin is not None:
            self.plugin = None

    def on_plugin_enable(self):
        super(DropBotDxPlugin, self).on_plugin_enable()

        self.cleanup_plugin()
        # Initialize 0MQ hub plugin and subscribe to hub messages.
        self.plugin = DmfZmqPlugin(self, self.name, get_hub_uri(),
                                   subscribe_options={zmq.SUBSCRIBE: ''})
        # Initialize sockets.
        self.plugin.reset()

        # Periodically process outstanding message received on plugin sockets.
        self.plugin_timeout_id = gtk.timeout_add(10, self.plugin.check_sockets)

        self.check_device_name_and_version()
        if get_app().protocol:
            self.on_step_run()
            self._update_protocol_grid()

    def on_plugin_disable(self):
        self.cleanup_plugin()
        if get_app().protocol:
            self.on_step_run()
            self._update_protocol_grid()

    def on_app_exit(self):
        """
        Handler called just before the Microdrop application exits.
        """
        self.cleanup_plugin()
        try:
            self.control_board.hv_output_enabled = False
        except: # ignore any exceptions (e.g., if the board is not connected)
            pass

    def on_protocol_swapped(self, old_protocol, protocol):
        self._update_protocol_grid()

    def _update_protocol_grid(self):
        app = get_app()
        app_values = self.get_app_values()
        pgc = get_service_instance(ProtocolGridController, env='microdrop')
        if pgc.enabled_fields:
            pgc.update_grid()

    def on_app_options_changed(self, plugin_name):
        app = get_app()
        if plugin_name == self.name:
            app_values = self.get_app_values()
            reconnect = False

            if self.control_board:
                for k, v in app_values.items():
                    if k == 'serial_port' and self.control_board.port != v:
                        reconnect = True

            if reconnect:
                self.connect()
                
            self._update_protocol_grid()
        elif plugin_name == app.name:
            # Turn off all electrodes if we're not in realtime mode and not
            # running a protocol.
            if (self.control_board and not app.realtime_mode and
                not app.running):
                logger.info('Turning off all electrodes.')
                self.control_board.hv_output_enabled = False

    def connect(self):
        '''
        Try to connect to the control board at the default serial port selected
        in the Microdrop application options.

        If unsuccessful, try to connect to the control board on any available
        serial port, one-by-one.
        '''
        self.current_frequency = None
        if len(DropBotDxPlugin.serial_ports_):
            app_values = self.get_app_values()
            # try to connect to the last successful port
            try:
                self.control_board = SerialProxy(port=str(app_values['serial_port']))
                self.control_board.initialize_switching_boards()
            except RuntimeError, why:
                logger.warning('Could not connect to control board on port %s.'
                               ' Checking other ports... [%s]' %
                               (app_values['serial_port'], why))
                
                self.control_board = SerialProxy()
            app_values['serial_port'] = self.control_board.port
            self.set_app_values(app_values)
        else:
            raise Exception("No serial ports available.")

    def check_device_name_and_version(self):
        '''
        Check to see if:

         a) The connected device is a OpenDrop
         b) The device firmware matches the host driver API version

        In the case where the device firmware version does not match, display a
        dialog offering to flash the device with the firmware version that
        matches the host driver API version.
        '''
        try:
            self.connect()
            name = self.control_board.properties['package_name']
            if name != self.control_board.host_package_name:
                raise Exception("Device is not a DropBot V3")

            host_software_version = self.control_board.host_software_version
            remote_software_version = self.control_board.remote_software_version

            # Reflash the firmware if it is not the right version.
            if host_software_version != remote_software_version:
                response = yesno("The DropBot DX firmware version (%s) "
                                 "does not match the driver version (%s). "
                                 "Update firmware?" % (remote_software_version,
                                                       host_software_version))
                if response == gtk.RESPONSE_YES:
                    self.on_flash_firmware()
        except Exception, why:
            logger.warning("%s" % why)

        self.update_connection_status()

    def on_flash_firmware(self, widget=None, data=None):
        app = get_app()
        try:
            connected = self.control_board != None
            if not connected:
                self.connect()
            '''
            response = yesno("Save current control board configuration before "
                             "flashing?")
            if response == gtk.RESPONSE_YES:
                self.save_config()
            '''
            hardware_version = utility.Version.fromstring(
                self.control_board.hardware_version
            )
            if connected:
                # disconnect
                del self.control_board
                self.control_board = None
            self.control_board.flash_firmware(hardware_version)
            app.main_window_controller.info("Firmware updated successfully.",
                                            "Firmware update")
        except Exception, why:
            logger.error("Problem flashing firmware. ""%s" % why)
        self.check_device_name_and_version()

    def update_connection_status(self):
        self.connection_status = "Not connected"
        app = get_app()
        connected = self.control_board != None
        if connected:
            properties = self.control_board.properties
            version = self.control_board.hardware_version
            n_channels = self.control_board.number_of_channels
            serial_number = self.control_board.serial_number
            self.connection_status = ('%s v%s (Firmware: %s, S/N %03d)\n'
                '%d channels' % (properties['display_name'], version,
                                 properties['software_version'], serial_number,
                                 n_channels))

        app.main_window_controller.label_control_board_status\
           .set_text(self.connection_status)

    def on_step_run(self):
        """
        Handler called whenever a step is executed.

        Plugins that handle this signal must emit the on_step_complete
        signal once they have completed the step. The protocol controller
        will wait until all plugins have completed the current step before
        proceeding.
        """
        logger.debug('[DropBotDxPlugin] on_step_run()')
        self._kill_running_step()
        app = get_app()
        options = self.get_step_options()
        app_values = self.get_app_values()

        if (self.control_board and (app.realtime_mode or app.running)):
            max_channels = self.control_board.number_of_channels
            # All channels should default to off.
            channel_states = np.zeros(max_channels, dtype=int)
            # Set the state of any channels that have been set explicitly.
            channel_states[self.channel_states.index
                           .values.tolist()] = self.channel_states

            emit_signal("set_frequency",
                        options['frequency'],
                        interface=IWaveformGenerator)
            emit_signal("set_voltage", options['voltage'],
                        interface=IWaveformGenerator)
            if not self.control_board.hv_output_enabled:
                self.control_board.hv_output_enabled = True

            self.control_board.set_state_of_channels(channel_states)

        # if a protocol is running, wait for the specified minimum duration
        if app.running:
            logger.debug('[DropBotDxPlugin] on_step_run: '
                         'timeout_add(%d, _callback_step_completed)' %
                         options['duration'])
            self.timeout_id = gobject.timeout_add(
                options['duration'], self._callback_step_completed)
            return
        else:
            self.step_complete()

    def step_complete(self, return_value=None):
        app = get_app()
        if app.running or app.realtime_mode:
            emit_signal('on_step_complete', [self.name, return_value])

    def on_step_complete(self, plugin_name, return_value=None):
        if plugin_name == self.name:
            self.timeout_id = None

    def _kill_running_step(self):
        if self.timeout_id:
            logger.debug('[DropBotDxPlugin] _kill_running_step: removing'
                         'timeout_id=%d' % self.timeout_id)
            gobject.source_remove(self.timeout_id)

    def _callback_step_completed(self):
        logger.debug('[DropBotDxPlugin] _callback_step_completed')
        self.step_complete()
        return False  # stop the timeout from refiring

    def on_protocol_run(self):
        """
        Handler called when a protocol starts running.
        """
        app = get_app()
        if not self.control_board:
            logger.warning("Warning: no control board connected.")
        elif (self.control_board.number_of_channels <=
              app.dmf_device.max_channel()):
            logger.warning("Warning: currently connected board does not have "
                           "enough channels for this protocol.")

    def on_protocol_pause(self):
        """
        Handler called when a protocol is paused.
        """
        app = get_app()
        self._kill_running_step()
        if self.control_board and not app.realtime_mode:
            # Turn off all electrodes
            logger.debug('Turning off all electrodes.')
            self.control_board.hv_output_enabled = False

    def on_experiment_log_selection_changed(self, data):
        """
        Handler called whenever the experiment log selection changes.

        Parameters:
            data : dictionary of experiment log data for the selected steps
        """
        pass

    def set_voltage(self, voltage):
        """
        Set the waveform voltage.

        Parameters:
            voltage : RMS voltage
        """
        logger.info("[DropBotDxPlugin].set_voltage(%.1f)" % voltage)
        self.control_board.voltage = voltage

    def set_frequency(self, frequency):
        """
        Set the waveform frequency.

        Parameters:
            frequency : frequency in Hz
        """
        logger.info("[DropBotDxPlugin].set_frequency(%.1f)" % frequency)
        self.control_board.frequency = frequency
        self.current_frequency = frequency

    def on_step_options_changed(self, plugin, step_number):
        logger.debug('[DropBotDxPlugin] on_step_options_changed(): %s '
                     'step #%d' % (plugin, step_number))
        app = get_app()
        app_values = self.get_app_values()
        options = self.get_step_options(step_number)
        if (app.protocol and not app.running and not app.realtime_mode and
            (plugin == 'microdrop.gui.dmf_device_controller' or plugin ==
             self.name) and app.protocol.current_step_number == step_number):
            self.on_step_run()

    def on_step_swapped(self, original_step_number, new_step_number):
        logger.debug('[DropBotDxPlugin] on_step_swapped():'
                     'original_step_number=%d, new_step_number=%d' %
                     (original_step_number, new_step_number))
        self.on_step_options_changed(self.name,
                                     get_app().protocol.current_step_number)

    def on_experiment_log_changed(self, log):
        # Check if the experiment log already has control board meta data, and
        # if so, return.
        data = log.get("control board name")
        for val in data:
            if val:
                return

        # otherwise, add the name, hardware version, serial number,
        # and firmware version
        data = {}
        if self.control_board:
            data["control board name"] = self.control_board.properties['display_name']
            data["control board serial number"] = \
                self.control_board.serial_number
            data["control board hardware version"] = (self.control_board
                                                      .hardware_version)
            data["control board software version"] = (self.control_board
                                                      .properties['software_version'])
            # add info about the devices on the i2c bus
            """
            try:
                #data["i2c devices"] = (self.control_board._i2c_devices)
            except:
                pass
            """
        log.add_data(data)

    def get_schedule_requests(self, function_name):
        """
        Returns a list of scheduling requests (i.e., ScheduleRequest
        instances) for the function specified by function_name.
        """
        if function_name in ['on_step_options_changed']:
            return [ScheduleRequest(self.name,
                                    'microdrop.gui.protocol_grid_controller'),
                    ScheduleRequest(self.name,
                                    'microdrop.gui.protocol_controller'),
                    ]
        elif function_name == 'on_app_options_changed':
            return [ScheduleRequest('microdrop.app', self.name)]
        elif function_name == 'on_protocol_swapped':
            return [ScheduleRequest('microdrop.gui.protocol_grid_controller',
                                    self.name)]
        elif function_name == 'on_app_exit':
            return [ScheduleRequest('microdrop.gui.experiment_log_controller',
                                    self.name)]
        return []

PluginGlobals.pop_env()
