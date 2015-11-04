#!/usr/bin/env python

"""
@package ion.agents.platform.rsn.rsn_platform_driver
@file    ion/agents/platform/rsn/rsn_platform_driver.py
@author  Carlos Rueda
@brief   The main RSN OMS platform driver class.
"""
import functools
from xmlrpclib import Fault
from xmlrpclib import ProtocolError
from socket import error as SocketError
import ntplib
import time
from copy import deepcopy
from functools import partial
import mi.core.log
from mi.core.common import BaseEnum
from mi.core.scheduler import PolledScheduler
from mi.platform.platform_driver import PlatformDriver
from mi.core.instrument.data_particle import DataParticle, DataParticleKey
from mi.core.instrument.instrument_driver import DriverAsyncEvent
from mi.platform.platform_driver import PlatformDriverState
from mi.platform.platform_driver import PlatformDriverEvent
from mi.platform.exceptions import PlatformException
from mi.platform.exceptions import PlatformDriverException
from mi.platform.exceptions import PlatformConnectionException
from mi.platform.rsn.oms_client_factory import CIOMSClientFactory
from mi.platform.responses import InvalidResponse
from mi.platform.util.node_configuration import NodeConfiguration


log = mi.core.log.get_logger()

__author__ = 'Carlos Rueda'
__license__ = 'Apache 2.0'


class PlatformParticle(DataParticle):
    """
    The contents of the parameter dictionary, published at the start of a scan
    """

    def _build_parsed_values(self):
        return [{DataParticleKey.VALUE_ID: a, DataParticleKey.VALUE: b} for a, b in self.raw_data]


class ScheduledJob(BaseEnum):
    """
    Instrument scheduled jobs
    """
    ACQUIRE_SAMPLE = 'pad_sample_timer_event'


class RSNPlatformDriverState(PlatformDriverState):
    """
    We simply inherit the states from the superclass
    """
    pass


class RSNPlatformDriverEvent(PlatformDriverEvent):
    """
    The ones for superclass plus a few others for the CONNECTED state.
    """
    GET_ENG_DATA = 'RSN_PLATFORM_DRIVER_GET_ENG_DATA'
    TURN_ON_PORT = 'RSN_PLATFORM_DRIVER_TURN_ON_PORT'
    TURN_OFF_PORT = 'RSN_PLATFORM_DRIVER_TURN_OFF_PORT'
    SET_PORT_OVER_CURRENT_LIMITS = 'RSN_PLATFORM_DRIVER_SET_PORT_OVER_CURRENT_LIMITS'
    START_PROFILER_MISSION = 'RSN_PLATFORM_DRIVER_START_PROFILER_MISSION'
    STOP_PROFILER_MISSION = 'RSN_PLATFORM_DRIVER_STOP_PROFILER_MISSION'
    GET_MISSION_STATUS = 'RSN_PLATFORM_DRIVER_GET_MISSION_STATUS'
    GET_AVAILABLE_MISSIONS = 'RSN_PLATFORM_DRIVER_GET_AVAILABLE_MISSIONS'


class RSNPlatformDriverCapability(BaseEnum):
    GET_ENG_DATA = RSNPlatformDriverEvent.GET_ENG_DATA
    TURN_ON_PORT = RSNPlatformDriverEvent.TURN_ON_PORT
    TURN_OFF_PORT = RSNPlatformDriverEvent.TURN_OFF_PORT
    SET_PORT_OVER_CURRENT_LIMITS = RSNPlatformDriverEvent.SET_PORT_OVER_CURRENT_LIMITS
    START_PROFILER_MISSION = RSNPlatformDriverEvent.START_PROFILER_MISSION
    STOP_PROFILER_MISSION = RSNPlatformDriverEvent.STOP_PROFILER_MISSION
    GET_MISSION_STATUS = RSNPlatformDriverEvent.GET_MISSION_STATUS
    GET_AVAILABLE_MISSIONS = RSNPlatformDriverEvent.GET_AVAILABLE_MISSIONS


def verify_rsn_oms(func):
        @functools.wraps(func)
        def _verify_rsn_oms(*args, **kwargs):
            if args:
                driver = args[0]
                if hasattr(driver, '_rsn_oms'):
                    if driver._rsn_oms is None:
                        raise PlatformConnectionException(
                            "Cannot %s: _rsn_oms object required (created via connect() call)" % func.__name__)
            return func(*args, **kwargs)
        return _verify_rsn_oms


# noinspection PyUnusedLocal
class RSNPlatformDriver(PlatformDriver):
    """
    The main RSN OMS platform driver class.
    """
    def __init__(self, event_callback, refdes=None):
        """
        Creates an RSNPlatformDriver instance.
        @param event_callback  Listener of events generated by this driver
        """
        PlatformDriver.__init__(self, event_callback)

        # CIOMSClient instance created by connect() and destroyed by disconnect():
        self._rsn_oms = None

        # URL for the event listener registration/unregistration (based on
        # web server launched by ServiceGatewayService, since that's the
        # service in charge of receiving/relaying the OMS events).
        # NOTE: (as proposed long ago), this kind of functionality should
        # actually be provided by some component more in charge of the RSN
        # platform netwokr as a whole -- as opposed to platform-specific).
        self.listener_url = None

        # scheduler config is a bit redundant now, but if we ever want to
        # re-initialize a scheduler we will need it.
        self._scheduler = None
        self._last_sample_time = {}

    ################
    # Static methods
    ################

    @staticmethod
    def _verify_response(response, key=None, msg=None):
        if key is not None:
            if key not in response:
                raise PlatformException("Error in %s response: %r" % (msg, response))
            response = response[key]

        if not response.startswith('OK'):
            raise PlatformException("Error in %s for key %s: %r" % (msg, key, response))

    @staticmethod
    def group_by_timestamp(attr_dict):
        return_dict = {}
        # go through all of the returned values and get the unique timestamps. Each
        # particle will have data for a unique timestamp
        for attr_id, attr_vals in attr_dict.iteritems():
            for value, timestamp in attr_vals:
                return_dict.setdefault(timestamp, []).append((attr_id, value))

        return return_dict

    @staticmethod
    def convert_attrs_to_ion(stream, attrs):
        attrs_return = []

        # convert back to ION parameter name and scale from OMS to ION
        for key, v in attrs:
            scale_factor = stream[key]['scale_factor']
            v = v * scale_factor if v else v
            attrs_return.append((stream[key]['ion_parameter_name'], v))

        return attrs_return

    def _filter_capabilities(self, events):
        """
        """
        events_out = [x for x in events if RSNPlatformDriverCapability.has(x)]
        return events_out

    def validate_driver_configuration(self, driver_config):
        """
        Driver config must include 'oms_uri' entry.
        """
        if 'oms_uri' not in driver_config:
            log.error("'oms_uri' not present in driver_config = %r", driver_config)
            raise PlatformDriverException(msg="driver_config does not indicate 'oms_uri'")

    def _configure(self, driver_config):
        """
        Nothing special done here, only calls super.configure(driver_config)

        @param driver_config with required 'oms_uri' entry.
        """
        PlatformDriver._configure(self, driver_config)

        self.nodeCfg = NodeConfiguration()

        self._platform_id = driver_config['node_id']
        self.nodeCfg.openNode(self._platform_id, driver_config['driver_config_file']['node_cfg_file'])

        self.nms_source = self.nodeCfg.node_meta_data['nms_source']

        self.oms_sample_rate = self.nodeCfg.node_meta_data['oms_sample_rate']

        self.nodeCfg.Print()

        self._construct_resource_schema()

    def _build_scheduler(self):
        """
        Build a scheduler for periodic status updates
        """
        self._scheduler = PolledScheduler()
        self._scheduler.start()

        def event_callback(event):
            log.debug("driver job triggered, raise event: %s" % event)
            self._fsm.on_event(event)

        # Dynamically create the method and add it
        method = partial(event_callback, RSNPlatformDriverEvent.GET_ENG_DATA)

        self._job = self._scheduler.add_interval_job(method, seconds=self.oms_sample_rate)

    def _delete_scheduler(self):
        """
        Remove the autosample schedule.
        """
        try:
            self._scheduler.unschedule_job(self._job)
        except KeyError:
            log.debug('Failed to remove scheduled job for ACQUIRE_SAMPLE')

        self._scheduler.shutdown()

    def _construct_resource_schema(self):
        """
        """
        parameters = deepcopy(self._param_dict)

        for k, v in parameters.iteritems():
            read_write = v.get('read_write', None)
            if read_write == 'write':
                v['visibility'] = 'READ_WRITE'
            else:
                v['visibility'] = 'READ_ONLY'

        commands = {RSNPlatformDriverEvent.TURN_ON_PORT: {
            "display_name": "Port Power On",
            "description": "Activate port power.",
            "args": [],
            "kwargs": {
                'port_id': {
                    "required": True,
                    "type": "string",
                }
            }

        }, RSNPlatformDriverEvent.TURN_OFF_PORT: {
            "display_name": "Port Power Off",
            "description": "Deactivate port power.",
            "args": [],
            "kwargs": {
                'port_id': {
                    "required": True,
                    "type": "string",
                }
            }
        }}

        self._resource_schema['parameters'] = parameters
        self._resource_schema['commands'] = commands

    def _connect(self, recursion=None):
        """
        Creates an CIOMSClient instance, does a ping to verify connection,
        and starts event dispatch.
        """
        # create CIOMSClient:
        oms_uri = self._driver_config['oms_uri']
        log.debug("%r: creating CIOMSClient instance with oms_uri=%r",
                  self._platform_id, oms_uri)
        self._rsn_oms = CIOMSClientFactory.create_instance(oms_uri)
        log.debug("%r: CIOMSClient instance created: %s",
                  self._platform_id, self._rsn_oms)

        # ping to verify connection:
        self._ping()
        self._build_scheduler()  # then start calling it every X seconds

    def _disconnect(self, recursion=None):
        CIOMSClientFactory.destroy_instance(self._rsn_oms)
        self._rsn_oms = None
        log.debug("%r: CIOMSClient instance destroyed", self._platform_id)

        self._delete_scheduler()
        self._scheduler = None

    def get_metadata(self):
        """
        """
        return self.nodeCfg.node_meta_data

    def get_eng_data(self):
        ntp_time = ntplib.system_to_ntp_time(time.time())
        max_time = ntp_time - self.oms_sample_rate * 10

        for key, stream in self.nodeCfg.node_streams.iteritems():
            log.debug("%r Stream(%s)", self._platform_id, key)
            # prevent the max lookback time getting to big if we stop getting data for some reason
            self._last_sample_time[key] = max(self._last_sample_time.get(key, max_time), max_time)

            for instance in stream:
                self.get_instance_particles(key, instance, stream[instance])

    def get_instance_particles(self, stream_name, instance, stream_def):
        # add a little bit of time to the last received so we don't get one we already have again
        attrs = [(k, self._last_sample_time[stream_name] + 0.1) for k in stream_def]

        if not attrs:
            return

        attr_dict = self.get_attribute_values_from_oms(attrs)  # go get the data from the OMS
        ts_attr_dict = self.group_by_timestamp(attr_dict)

        if not ts_attr_dict:
            return

        self._last_sample_time[stream_name] = max(ts_attr_dict.keys())

        for timestamp in ts_attr_dict:
            attrs = ts_attr_dict[timestamp]
            attrs = self.convert_attrs_to_ion(stream_def, attrs)
            particle = PlatformParticle(attrs, preferred_timestamp=DataParticleKey.INTERNAL_TIMESTAMP)
            particle.set_internal_timestamp(timestamp)
            particle._data_particle_type = stream_name

            event = {
                'type': DriverAsyncEvent.SAMPLE,
                'value': particle.generate(),
                'time': time.time(),
                'instance': '%s-%s' % (self.nodeCfg.node_meta_data['reference_designator'], instance),
            }

            self._send_event(event)

    def get_attribute_values(self, attrs):
        """
        Simple wrapper method for compatibility.
        :param attrs:
        """
        return self.get_attribute_values_from_oms(attrs)

    @verify_rsn_oms
    def get_attribute_values_from_oms(self, attrs):
        """
        Fetch values from the OMS
        :param attrs:
        """
        if not isinstance(attrs, (list, tuple)):
            msg = 'get_attribute_values: attrs argument must be a list [(attrName, from_time), ...]. Given: %s' % attrs
            raise PlatformException(msg)

        response = None

        try:
            response = self._rsn_oms.attr.get_platform_attribute_values(self._platform_id, attrs)
            response = self._verify_platform_id_in_response(response)
            return_dict = {}
            for key in response:
                value_list = response[key]
                if value_list == 'INVALID_ATTRIBUTE_ID':
                    continue

                if not isinstance(value_list, list):
                    raise PlatformException(msg="Error in getting values for attribute %s.  %s" % (key, value_list))
                if value_list and value_list[0][0] == "ERROR_DATA_REQUEST_TOO_FAR_IN_PAST":
                        raise PlatformException(msg="Time requested for %s too far in the past" % key)
                return_dict[key] = value_list
            return return_dict

        except (Fault, ProtocolError, SocketError) as e:
            msg = "get_attribute_values_from_oms Cannot get_platform_attribute_values: %s" % e
            raise PlatformConnectionException(msg)
        except AttributeError:
            msg = "Error returned in requesting attributes: %s" % response
            raise PlatformException(msg)

    def _verify_platform_id_in_response(self, response):
        """
        Verifies the presence of my platform_id in the response.

        @param response Dictionary returned by _rsn_oms

        @retval response[self._platform_id]
        """
        if self._platform_id not in response:
            msg = "unexpected: response does not contain entry for %r" % self._platform_id
            log.error(msg)
            raise PlatformException(msg=msg)

        if response[self._platform_id] == InvalidResponse.PLATFORM_ID:
            msg = "response reports invalid platform_id for %r" % self._platform_id
            log.error(msg)
            raise PlatformException(msg=msg)
        else:
            return response[self._platform_id]

    ###############################################
    # OMS Commands
    ###############################################

    @verify_rsn_oms
    def _ping(self):
        """
        Verifies communication with external platform returning "PONG" if
        this verification completes OK.

        @retval "PONG" iff all OK.
        @raise PlatformConnectionException Cannot ping external platform or
               got unexpected response.
        """
        try:
            retval = self._rsn_oms.hello.ping()
            if retval is None or retval.upper() != "PONG":
                raise PlatformConnectionException(msg="Unexpected ping response: %r" % retval)
            return "PONG"

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(msg="Cannot ping: %s" % str(e))

    @verify_rsn_oms
    def set_overcurrent_limit(self, port_id, milliamps, microseconds, src):
        oms_port_cntl_id = self._verify_and_return_oms_port(port_id, 'set_overcurrent_limit')

        try:
            response = self._rsn_oms.port.set_over_current(self._platform_id,
                                                           oms_port_cntl_id,
                                                           int(milliamps),
                                                           int(microseconds),
                                                           src)
            response = self._convert_port_id_from_oms_to_ci(port_id, oms_port_cntl_id, response)
            dic_plat = self._verify_platform_id_in_response(response)
            self._verify_response(dic_plat, key=port_id, msg="setting overcurrent")
            return dic_plat  # note: return the dic for the platform

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(msg="Cannot set_overcurrent_limit: %s" % str(e))

    @verify_rsn_oms
    def turn_on_port(self, port_id, src):
        oms_port_cntl_id = self._verify_and_return_oms_port(port_id, 'turn_on_port')

        try:
            response = self._rsn_oms.port.turn_on_platform_port(self._platform_id, oms_port_cntl_id, src)
            response = self._convert_port_id_from_oms_to_ci(port_id, oms_port_cntl_id, response)
            dic_plat = self._verify_platform_id_in_response(response)
            self._verify_response(dic_plat, key=port_id, msg="turn on port")
            return dic_plat  # note: return the dic for the platform

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(msg="Cannot turn_on_platform_port: %s" % str(e))

    @verify_rsn_oms
    def turn_off_port(self, port_id, src):
        oms_port_cntl_id = self._verify_and_return_oms_port(port_id, 'turn_off_port')

        try:
            response = self._rsn_oms.port.turn_off_platform_port(self._platform_id, oms_port_cntl_id, src)
            response = self._convert_port_id_from_oms_to_ci(port_id, oms_port_cntl_id, response)
            dic_plat = self._verify_platform_id_in_response(response)
            self._verify_response(dic_plat, key=port_id, msg="turn off port")
            return dic_plat  # note: return the dic for the platform

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(msg="Cannot turn_off_platform_port: %s" % str(e))

    @verify_rsn_oms
    def start_profiler_mission(self, mission_name, src):
        try:
            response = self._rsn_oms.profiler.start_mission(self._platform_id, mission_name, src)
            dic_plat = self._verify_platform_id_in_response(response)
            self._verify_response(dic_plat, key=mission_name, msg="starting mission")
            return dic_plat  # note: return the dic for the platform

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(msg="Cannot start_profiler_mission: %s" % str(e))

    @verify_rsn_oms
    def stop_profiler_mission(self, flag, src):
        try:
            response = self._rsn_oms.profiler.stop_mission(self._platform_id, flag, src)
            dic_plat = self._verify_platform_id_in_response(response)
            self._verify_response(dic_plat, msg="stopping profiler")
            return dic_plat  # note: return the dic for the platform

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(msg="Cannot stop_profiler_mission: %s" % str(e))

    @verify_rsn_oms
    def get_mission_status(self, *args, **kwargs):
        try:
            response = self._rsn_oms.profiler.get_mission_status(self._platform_id)
            dic_plat = self._verify_platform_id_in_response(response)
            return dic_plat  # note: return the dic for the platform

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(msg="Cannot get_mission_status: %s" % str(e))

    @verify_rsn_oms
    def get_available_missions(self, *args, **kwargs):

        try:
            response = self._rsn_oms.profiler.get_available_missions(self._platform_id)
            dic_plat = self._verify_platform_id_in_response(response)
            return dic_plat  # note: return the dic for the platform

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(msg="Cannot get_available_missions: %s" % str(e))

    def _verify_and_return_oms_port(self, port_id, method_name):
        if port_id not in self.nodeCfg.node_port_info:
            raise PlatformConnectionException("Cannot %s: Invalid Port ID" % method_name)

        return self.nodeCfg.node_port_info[port_id]['port_oms_port_cntl_id']

    def _convert_port_id_from_oms_to_ci(self, port_id, oms_port_cntl_id, response):
        """
        Converts the OMS port id into the original one provided.
        """
        oms_port_cntl_id = str(oms_port_cntl_id)
        if response[self._platform_id].get(oms_port_cntl_id, None):
            return {self._platform_id: {port_id: response[self._platform_id][oms_port_cntl_id]}}

        return response

    ###############################################
    # External event handling:
    ###############################################

    @verify_rsn_oms
    def _register_event_listener(self, url):
        """
        Registers given url for all event types.
        """
        try:
            already_registered = self._rsn_oms.event.get_registered_event_listeners()
            if url in already_registered:
                log.debug("listener %r was already registered", url)
                return

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(
                msg="%r: Cannot get registered event listeners: %s" % (self._platform_id, e))

        try:
            result = self._rsn_oms.event.register_event_listener(url)
            log.debug("%r: register_event_listener(%r) => %s", self._platform_id, url, result)
        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(
                msg="%r: Cannot register_event_listener: %s" % (self._platform_id, e))

    @verify_rsn_oms
    def _unregister_event_listener(self, url):
        """
        Unregisters given url for all event types.
        """
        try:
            result = self._rsn_oms.event.unregister_event_listener(url)
            log.debug("%r: unregister_event_listener(%r) => %s", self._platform_id, url, result)

        except (Fault, ProtocolError, SocketError) as e:
            raise PlatformConnectionException(
                msg="%r: Cannot unregister_event_listener: %s" % (self._platform_id, e))

    ##############################################################
    # GET
    ##############################################################

    def get(self, *args, **kwargs):
        if 'attrs' in kwargs:
            attrs = kwargs['attrs']
            result = self.get_attribute_values(attrs)
            return result

        if 'metadata' in kwargs:
            result = self.get_metadata()
            return result

        return super(RSNPlatformDriver, self).get(*args, **kwargs)

    ##############################################################
    # EXECUTE
    ##############################################################

    def execute(self, cmd, *args, **kwargs):
        """
        Executes the given command.

        @param cmd   command

        @return  result of the execution
        """
        return self._fsm.on_event(cmd, *args, **kwargs)

    def _handler_connected_start_profiler_mission(self, *args, **kwargs):
        """
        """
        profile_mission_name = kwargs.get('profile_mission_name')
        if profile_mission_name is None:
            raise PlatformException('start_profiler_mission: missing profile_mission_name argument')

        src = kwargs.get('src', None)
        if src is None:
            raise PlatformException('set_port_over_current_limits: missing src argument')

        try:
            result = self.start_profiler_mission(profile_mission_name, src)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.START_PROFILER_MISSION,
                                         args, kwargs, e)

    def _handler_connected_stop_profiler_mission(self, *args, **kwargs):
        """
        """
        flag = kwargs.get('flag', None)
        if flag is None:
            raise PlatformException('_handler_connected_stop_profiler_mission: missing flag argument')

        src = kwargs.get('src', None)
        if src is None:
            raise PlatformException('set_port_over_current_limits: missing src argument')

        try:
            result = self.stop_profiler_mission(flag, src)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.STOP_PROFILER_MISSION,
                                         args, kwargs, e)

    def _handler_connected_get_mission_status(self, *args, **kwargs):
        """
        """
        try:
            result = self.get_mission_status()
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.GET_MISSION_STATUS,
                                         args, kwargs, e)

    def _handler_connected_get_available_missions(self, *args, **kwargs):
        """
        """
        try:
            result = self.get_available_missions()
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.GET_AVAILABLE_MISSIONS,
                                         args, kwargs, e)

    def _handler_connected_get_eng_data(self, *args, **kwargs):
        """
        """

        try:
            self.get_eng_data()
            return None, None

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.GET_ENG_DATA,
                                         args, kwargs, e)

    def _handler_connected_set_port_over_current_limits(self, *args, **kwargs):
        """
        """
        port_id = kwargs.get('port_id', None)
        if port_id is None:
            raise PlatformException('set_port_over_current_limits: missing port_id argument')

        milliamps = kwargs.get('milliamps', None)
        if milliamps is None:
            raise PlatformException('set_port_over_current_limits: missing milliamps argument')

        microseconds = kwargs.get('microseconds', None)
        if milliamps is None:
            raise PlatformException('set_port_over_current_limits: missing microseconds argument')

        src = kwargs.get('src', None)
        if src is None:
            raise PlatformException('set_port_over_current_limits: missing src argument')

        try:
            result = self.set_overcurrent_limit(port_id, milliamps, microseconds, src)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.SET_PORT_OVER_CURRENT_LIMITS,
                                         args, kwargs, e)

    def _handler_connected_turn_on_port(self, *args, **kwargs):
        """
        """
        port_id = kwargs.get('port_id', None)
        if port_id is None:
            raise PlatformException('turn_on_port: missing port_id argument')

        src = kwargs.get('src', None)
        if port_id is None:
            raise PlatformException('turn_on_port: missing src argument')

        try:
            result = self.turn_on_port(port_id, src)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.TURN_ON_PORT,
                                         args, kwargs, e)

    def _handler_connected_turn_off_port(self, *args, **kwargs):
        """
        """
        port_id = kwargs.get('port_id', None)
        if port_id is None:
            raise PlatformException('turn_off_port: missing port_id argument')

        src = kwargs.get('src', None)
        if port_id is None:
            raise PlatformException('turn_off_port: missing src argument')

        try:
            result = self.turn_off_port(port_id, src)
            return None, result

        except PlatformConnectionException as e:
            return self._connection_lost(RSNPlatformDriverEvent.TURN_OFF_PORT,
                                         args, kwargs, e)

    ##############################################################
    # RSN Platform driver FSM setup
    ##############################################################

    def _construct_fsm(self,
                       states=RSNPlatformDriverState,
                       events=RSNPlatformDriverEvent,
                       enter_event=RSNPlatformDriverEvent.ENTER,
                       exit_event=RSNPlatformDriverEvent.EXIT):
        """
        """
        super(RSNPlatformDriver, self)._construct_fsm(states, events,
                                                      enter_event, exit_event)

        # CONNECTED state event handlers we add in this class:
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.TURN_ON_PORT,
                              self._handler_connected_turn_on_port)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.SET_PORT_OVER_CURRENT_LIMITS,
                              self._handler_connected_set_port_over_current_limits)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.TURN_OFF_PORT,
                              self._handler_connected_turn_off_port)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.START_PROFILER_MISSION,
                              self._handler_connected_start_profiler_mission)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.STOP_PROFILER_MISSION,
                              self._handler_connected_stop_profiler_mission)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.GET_MISSION_STATUS,
                              self._handler_connected_get_mission_status)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.GET_AVAILABLE_MISSIONS,
                              self._handler_connected_get_available_missions)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, RSNPlatformDriverEvent.GET_ENG_DATA,
                              self._handler_connected_get_eng_data)
        self._fsm.add_handler(PlatformDriverState.CONNECTED, ScheduledJob.ACQUIRE_SAMPLE,
                              self._handler_connected_get_eng_data)
