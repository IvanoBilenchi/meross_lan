"""
    meross_lan module interface to access Meross Cloud services
"""
from __future__ import annotations

from json import dumps as json_dumps, loads as json_loads
from logging import DEBUG, INFO, WARNING
from time import time
import typing

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
import paho.mqtt.client as mqtt

from .const import (
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_PAYLOAD,
    CONF_PROFILE_ID,
    CONF_PROFILE_ID_LOCAL,
    DOMAIN,
    PARAM_CLOUDAPI_DELAYED_SETUP_TIMEOUT,
    PARAM_CLOUDAPI_QUERY_DEVICELIST_TIMEOUT,
    PARAM_HEARTBEAT_PERIOD,
    PARAM_UNAVAILABILITY_TIMEOUT,
)
from .helpers import LOGGER, ConfigEntriesHelper, LOGGER_trap, schedule_async_callback
from .merossclient import (
    MEROSSDEBUG,
    KeyType,
    build_payload,
    const as mc,
    get_default_arguments,
    get_namespacekey,
    get_replykey,
)
from .merossclient.cloudapi import (
    APISTATUS_TOKEN_ERRORS,
    CloudApiError,
    MerossCloudCredentials,
    MerossMQTTClient,
    async_cloudapi_devicelist,
    async_cloudapi_logout,
)

if typing.TYPE_CHECKING:
    import asyncio
    from typing import ClassVar

    from homeassistant.core import HomeAssistant

    from . import MerossApi
    from .meross_device import MerossDevice
    from .merossclient.cloudapi import DeviceInfoType

KEY_DEVICE_INFO = "deviceInfo"
KEY_DEVICE_INFO_TIME = "deviceInfoTime"

class ApiProfile:
    """
    base class for both MerossCloudProfile and MerossApi
    allowing lightweight sharing of globals and defining
    a common interface
    """
    hass: ClassVar[HomeAssistant]
    api: ClassVar[MerossApi]
    devices: ClassVar[dict[str, MerossDevice]] = {}
    profiles: ClassVar[dict[str, MerossCloudProfile]] = {}

    # instance attributes
    key: str | None


class MQTTConnection:
    """
    Base abstract class representing a connection to an MQTT
    broker. Historically, MQTT support was only through MerossApi
    and the HA core MQTT broker. The introduction of Meross cloud
    connection has 'generalized' the concept of the MQTT broker.
    This interface is used by devices to actually send/receive
    MQTT messages (in place of the legacy approach using MerossApi)
    and represents a link to a broker (either through HA or a
    merosss cloud mqtt)
    """

    _KEY_STARTTIME = "__starttime"
    _KEY_REQUESTTIME = "__requesttime"
    _KEY_REQUESTCOUNT = "__requestcount"

    _mqtt_is_connected = False

    def __init__(self, profile: MerossCloudProfile | MerossApi, connection_id: str):
        self.profile = profile
        self.id = connection_id
        self.mqttdevices: dict[str, MerossDevice] = {}
        self.mqttdiscovering: dict[str, dict] = {}
        self._unsub_discovery_callback: asyncio.TimerHandle | None = None

    def shutdown(self):
        if self._unsub_discovery_callback is not None:
            self._unsub_discovery_callback.cancel()
            self._unsub_discovery_callback = None

    def attach(self, device: MerossDevice):
        assert device.device_id not in self.mqttdevices
        self.mqttdevices[device.device_id] = device
        device.mqtt_attached(self)

    def detach(self, device: MerossDevice):
        assert device.device_id in self.mqttdevices
        self.mqttdevices.pop(device.device_id)

    def mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ) -> asyncio.Future:
        """
        throw and forget..usually schedules to a background task since
        the actual mqtt send could be sync/blocking
        """
        raise NotImplementedError()

    async def async_mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ):
        """
        awaits message publish in asyncio style
        """
        raise NotImplementedError()

    async def async_mqtt_message(self, msg):
        try:
            message = json_loads(msg.payload)
            header = message[mc.KEY_HEADER]
            device_id = header[mc.KEY_FROM].split("/")[2]
            if LOGGER.isEnabledFor(DEBUG):
                LOGGER.debug(
                    "MQTTConnection(%s): MQTT RECV device_id:(%s) method:(%s) namespace:(%s)",
                    self.id,
                    device_id,
                    header[mc.KEY_METHOD],
                    header[mc.KEY_NAMESPACE],
                )
            if device_id in self.mqttdevices:
                self.mqttdevices[device_id].mqtt_receive(
                    header, message[mc.KEY_PAYLOAD]
                )
                return

            if device_id in ApiProfile.devices:
                # we have the device registered but somehow it is not 'mqtt binded'
                # either it's configuration is ONLY_HTTP or it is paired to the
                # Meross cloud. In this case we shouldn't receive 'local' MQTT
                LOGGER_trap(
                    WARNING,
                    14400,
                    "MQTTConnection(%s): device(%s) not registered for MQTT handling",
                    self.id,
                    ApiProfile.devices[device_id].name,
                )
                return

            # lookout for any disabled/ignored entry
            config_entries_helper = ConfigEntriesHelper(ApiProfile.hass)
            if (
                (self.id is CONF_PROFILE_ID_LOCAL)
                and (config_entries_helper.get_config_entry(DOMAIN) is None)
                and (config_entries_helper.get_config_flow(DOMAIN) is None)
            ):
                await ApiProfile.hass.config_entries.flow.async_init(
                    DOMAIN,
                    context={"source": "hub"},
                    data=None,
                )

            if (
                config_entry := config_entries_helper.get_config_entry(device_id)
            ) is not None:
                # entry already present...skip discovery
                LOGGER_trap(
                    INFO,
                    14400,
                    "MQTTConnection(%s): ignoring discovery for already configured device_id: %s (ConfigEntry is %s)",
                    self.id,
                    device_id,
                    "disabled"
                    if config_entry.disabled_by is not None
                    else "ignored"
                    if config_entry.source == "ignore"
                    else "unknown",
                )
                return

            # also skip discovered integrations waiting in HA queue
            if config_entries_helper.get_config_flow(device_id) is not None:
                LOGGER_trap(
                    INFO,
                    14400,
                    "MQTTConnection(%s): ignoring discovery for device_id: %s (ConfigFlow is in progress)",
                    self.id,
                    device_id,
                )
                return

            key = self.profile.key
            if (replykey := get_replykey(header, key)) is not key:
                LOGGER_trap(
                    WARNING,
                    300,
                    "Meross discovery key error for device_id: %s",
                    device_id,
                )
                if key is not None:
                    return

            discovered = self.get_or_set_discovering(device_id)
            if header[mc.KEY_METHOD] == mc.METHOD_GETACK:
                namespace = header[mc.KEY_NAMESPACE]
                if namespace in (
                    mc.NS_APPLIANCE_SYSTEM_ALL,
                    mc.NS_APPLIANCE_SYSTEM_ABILITY,
                ):
                    discovered.update(message[mc.KEY_PAYLOAD])

            if await self.async_progress_discovery(discovered, device_id):
                return

            self.mqttdiscovering.pop(device_id)
            discovered.pop(MQTTConnection._KEY_REQUESTTIME, None)
            discovered.pop(MQTTConnection._KEY_STARTTIME, None)
            discovered.pop(MQTTConnection._KEY_REQUESTCOUNT, None)
            await ApiProfile.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    CONF_DEVICE_ID: device_id,
                    CONF_PAYLOAD: discovered,
                    CONF_KEY: key,
                    CONF_PROFILE_ID: self.profile.id,
                },
            )
        except Exception as error:
            LOGGER.error(
                "MQTTConnection(%s) %s %s",
                self.id,
                type(error).__name__,
                str(error),
            )

    @property
    def mqtt_is_connected(self):
        return self._mqtt_is_connected

    @callback
    def set_mqtt_connected(self):
        for device in self.mqttdevices.values():
            device.mqtt_connected()
        self._mqtt_is_connected = True

    @callback
    def set_mqtt_disconnected(self):
        for device in self.mqttdevices.values():
            device.mqtt_disconnected()
        self._mqtt_is_connected = False

    def get_or_set_discovering(self, device_id: str):
        if device_id not in self.mqttdiscovering:
            # new device discovered: add to discovery state-machine
            self.mqttdiscovering[device_id] = {
                MQTTConnection._KEY_STARTTIME: time(),
                MQTTConnection._KEY_REQUESTTIME: 0,
                MQTTConnection._KEY_REQUESTCOUNT: 0,
            }
            if self._unsub_discovery_callback is None:
                self._unsub_discovery_callback = schedule_async_callback(
                    ApiProfile.hass,
                    PARAM_UNAVAILABILITY_TIMEOUT + 2,
                    self._async_discovery_callback,
                )
        return self.mqttdiscovering[device_id]

    async def async_progress_discovery(self, discovered: dict, device_id: str):
        for namespace in (mc.NS_APPLIANCE_SYSTEM_ALL, mc.NS_APPLIANCE_SYSTEM_ABILITY):
            if get_namespacekey(namespace) not in discovered:
                await self.async_mqtt_publish(
                    device_id,
                    *get_default_arguments(namespace),
                    self.profile.key,
                )
                discovered[MQTTConnection._KEY_REQUESTTIME] = time()
                discovered[MQTTConnection._KEY_REQUESTCOUNT] += 1
                return True

        return False

    async def _async_discovery_callback(self):
        """
        async task to keep alive the discovery process:
        activated when any device is initially detected
        this task is not renewed when the list of devices
        under 'discovery' is empty or these became stale
        """
        self._unsub_discovery_callback = None
        if len(discovering := self.mqttdiscovering) == 0:
            return

        epoch = time()
        for device_id, discovered in discovering.copy().items():
            if not self._mqtt_is_connected:
                break
            if (discovered[MQTTConnection._KEY_REQUESTCOUNT]) > 5:
                # stale entry...remove
                discovering.pop(device_id)
                continue
            if (
                epoch - discovered[MQTTConnection._KEY_REQUESTTIME]
            ) > PARAM_UNAVAILABILITY_TIMEOUT:
                await self.async_progress_discovery(discovered, device_id)

        if len(discovering):
            self._unsub_discovery_callback = schedule_async_callback(
                ApiProfile.hass,
                PARAM_UNAVAILABILITY_TIMEOUT + 2,
                self._async_discovery_callback,
            )


class MerossMQTTConnection(MQTTConnection, MerossMQTTClient):
    def __init__(
        self, profile: MerossCloudProfile, connection_id: str, host: str, port: int
    ):
        MerossMQTTClient.__init__(self, profile)
        MQTTConnection.__init__(self, profile, connection_id)
        self._host = host
        self._port = port
        self.user_data_set(ApiProfile.hass)  # speedup hass lookup in callbacks
        self.on_message = self._mqttc_message
        self.on_connect = self._mqttc_connect
        self.on_disconnect = self._mqttc_disconnect
        profile.mqttconnections[connection_id] = self

        if MEROSSDEBUG:

            def _random_disconnect(*_):
                # this should run in an executor
                if self.state_inactive:
                    if MEROSSDEBUG.mqtt_random_connect():
                        LOGGER.debug(
                            "MerossMQTTConnection(%s) random connect",
                            self.id,
                        )
                        self.safe_connect(self._host, self._port)
                else:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        LOGGER.debug(
                            "MerossMQTTConnection(%s) random disconnect",
                            self.id,
                        )
                        self.safe_disconnect()
                ApiProfile.hass.loop.call_soon_threadsafe(
                    async_call_later, ApiProfile.hass, 60, _random_disconnect
                )

            async_call_later(ApiProfile.hass, 60, _random_disconnect)

    def schedule_connect(self):
        # even if safe_connect should be as fast as possible and thread-safe
        # we still might incur some contention with thread stop/restart
        # so we delegate its call to an executor
        ApiProfile.hass.async_add_executor_job(self.safe_connect, self._host, self._port)

    def schedule_disconnect(self):
        # same as connect. safe_disconnect should be even faster and less
        # contending but...
        ApiProfile.hass.async_add_executor_job(self.safe_disconnect)

    def attach(self, device: MerossDevice):
        super().attach(device)
        if self.state_inactive:
            self.schedule_connect()

    def detach(self, device: MerossDevice):
        super().detach(device)
        if not self.mqttdevices:
            self.schedule_disconnect()

    def mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ) -> asyncio.Future:
        def _publish():
            LOGGER.debug(
                "MerossMQTTConnection(%s): MQTT SEND device_id:(%s) method:(%s) namespace:(%s)",
                self.id,
                device_id,
                method,
                namespace,
            )
            self.publish(
                mc.TOPIC_REQUEST.format(device_id),
                json_dumps(
                    build_payload(
                        namespace,
                        method,
                        payload,
                        key,
                        self.topic_command,
                        messageid,
                    )
                ),
            )

        return ApiProfile.hass.async_add_executor_job(_publish)

    async def async_mqtt_publish(
        self,
        device_id: str,
        namespace: str,
        method: str,
        payload: dict,
        key: KeyType = None,
        messageid: str | None = None,
    ):
        await self.mqtt_publish(device_id, namespace, method, payload, key, messageid)

    def _mqttc_message(self, client, userdata: HomeAssistant, msg: mqtt.MQTTMessage):
        userdata.create_task(self.async_mqtt_message(msg))

    def _mqttc_connect(self, client, userdata: HomeAssistant, rc, other):
        MerossMQTTClient._mqttc_connect(self, client, userdata, rc, other)
        userdata.add_job(self.set_mqtt_connected)

    def _mqttc_disconnect(self, client, userdata: HomeAssistant, rc):
        MerossMQTTClient._mqttc_disconnect(self, client, userdata, rc)
        userdata.add_job(self.set_mqtt_disconnected)


class MerossCloudProfile(MerossCloudCredentials, ApiProfile):
    """
    Represents and manages a cloud account profile used to retrieve keys
    and/or to manage cloud mqtt connection(s)
    """

    def __init__(self, data: dict):
        self.mqttconnections: dict[str, MerossMQTTConnection] = {}
        self.update(data)
        if not isinstance(self.get(KEY_DEVICE_INFO), dict):
            self[KEY_DEVICE_INFO] = {}
        self._last_query_devices = data.get(KEY_DEVICE_INFO_TIME, 0.0)
        self._unsub_setup_callback: asyncio.TimerHandle | None = None
        ApiProfile.profiles[self.id] = self

    def shutdown(self):
        if self._unsub_setup_callback is not None:
            self._unsub_setup_callback.cancel()
            self._unsub_setup_callback = None
        ApiProfile.profiles.pop(self.id)

    @property
    def id(self):
        return self.userid

    async def async_update_credentials(self, credentials: MerossCloudCredentials):
        assert self.userid == credentials.userid
        assert self.key == credentials.key
        if credentials.token != self.token:
            await self.async_release_token()
        self.update(credentials)

    async def async_release_token(self):
        if mc.KEY_TOKEN in self:
            if token := self.pop(mc.KEY_TOKEN):
                await async_cloudapi_logout(
                    token, async_get_clientsession(ApiProfile.hass)
                )

    def need_query_devices(self):
        return (
            time() - self._last_query_devices
        ) > PARAM_CLOUDAPI_QUERY_DEVICELIST_TIMEOUT

    async def async_query_devices(self):
        error = None
        try:
            if token := self.token:
                self._last_query_devices = time()
                device_info_list = await async_cloudapi_devicelist(
                    token, async_get_clientsession(ApiProfile.hass)
                )
                self[KEY_DEVICE_INFO] = {
                    device_info[mc.KEY_UUID]: device_info
                    for device_info in device_info_list
                }
                self[KEY_DEVICE_INFO_TIME] = self._last_query_devices
                ApiProfile.api.schedule_save_store()
            return
        except CloudApiError as clouderror:
            if clouderror.apistatus in APISTATUS_TOKEN_ERRORS:
                self.pop(mc.KEY_TOKEN, None)
            error = clouderror
        except Exception as e:
            error = e
        LOGGER.warning(
            "MerossCloudProfile(%s): %s %s in async_query_devices",
            self.id,
            type(error).__name__,
            str(error),
        )

    async def async_check_query_devices(self):
        if self.need_query_devices():
            await self.async_query_devices()

    def schedule_setup(self):
        """
        Performs a delayed initialization of the profile with low priority
        check tasks. This is called on integration setup when profiles are
        loaded and scheduled to be run some time after the staggering boot.
        Here we'll eventually download the updated device list
        from the cloud profile and update our registry with data recovered
        Also. We'll eventually setup the mqtt listeners in case our
        configured devices don't match the profile list. This usually means
        the user has binded a new device and we need to 'discover' it.
        Keep in mind discovery might already be working if any of our configured
        devices is already setup (not disabled!) and is using the same
        cloud mqtt broker. This code then will just check that..
        """
        assert self._unsub_setup_callback is None

        async def _async_callback():
            self._unsub_setup_callback = None
            await self.async_check_query_devices()

            devs: dict[str, DeviceInfoType] = self[KEY_DEVICE_INFO]

            config_entries_helper = ConfigEntriesHelper(ApiProfile.hass)
            unknown_devs = []
            for uuid in devs.keys():
                try:
                    if config_entries_helper.get_config_entry(uuid) is not None:
                        continue
                    if config_entries_helper.get_config_flow(uuid) is not None:
                        continue
                    # cloud conf has new devices
                    unknown_devs.append(uuid)
                except:
                    pass

            for uuid in unknown_devs:
                try:
                    device_info = devs[uuid]
                    mqttprofile = self._get_or_create_mqttconnection(
                        device_info[mc.KEY_DOMAIN], 443
                    )
                    if mqttprofile.state_inactive:
                        mqttprofile.schedule_connect()
                    mqttprofile.get_or_set_discovering(uuid)
                except:
                    pass

        self._unsub_setup_callback = schedule_async_callback(
            ApiProfile.hass, PARAM_CLOUDAPI_DELAYED_SETUP_TIMEOUT, _async_callback
        )

    def attach(self, device: MerossDevice):
        fw = device.descriptor.firmware
        self._get_or_create_mqttconnection(
            fw.get(mc.KEY_SERVER), fw.get(mc.KEY_PORT)
        ).attach(device)

    def get_device_info(self, uuid: str) -> DeviceInfoType | None:
        return self[KEY_DEVICE_INFO].get(uuid)

    def _get_or_create_mqttconnection(self, server, port):
        connection_id = f"{self.id}:{server}:{port}"
        if connection_id in self.mqttconnections:
            return self.mqttconnections[connection_id]
        return MerossMQTTConnection(self, connection_id, server, port)
