"""
 ****************************************************************************
 Filename:          realstor_logical_volume_sensor.py
 Description:       Monitors Logical Volume data using RealStor API.
 Creation Date:     09/09/2019
 Author:            Satish Darade

 Do NOT modify or remove this copyright and confidentiality notice!
 Copyright (c) 2001 - $Date: 2015/01/14 $ Seagate Technology, LLC.
 The code contained herein is CONFIDENTIAL to Seagate Technology, LLC.
 Portions are also trade secret. Any use, duplication, derivation, distribution
 or disclosure of this code, for any reason, not expressly authorized is
 prohibited. All other rights are expressly reserved by Seagate Technology, LLC.
 ****************************************************************************
"""
import json
import os
import socket
import time
import uuid
from threading import Event

from zope.interface import implementer

from framework.base.module_thread import SensorThread
from framework.base.internal_msgQ import InternalMsgQ
from framework.utils.service_logging import logger
from framework.utils.severity_reader import SeverityReader
from framework.platforms.realstor.realstor_enclosure import singleton_realstorencl
from framework.utils.store_factory import store

# Modules that receive messages from this module
from message_handlers.real_stor_encl_msg_handler import RealStorEnclMsgHandler

from sensors.Ilogicalvolume import ILogicalVolumesensor


@implementer(ILogicalVolumesensor)
class RealStorLogicalVolumeSensor(SensorThread, InternalMsgQ):
    """Monitors Logical Volume data using RealStor API"""


    SENSOR_NAME = "RealStorLogicalVolumeSensor"
    SENSOR_RESP_TYPE = "enclosure_logical_volume_alert"
    RESOURCE_CATEGORY = "eos"
    RESOURCE_TYPE = "enclosure:eos:logical_volume"

    PRIORITY = 1

    # Dependency list
    DEPENDENCIES = {
                    "plugins": ["RealStorEnclMsgHandler"],
                    "rpms": []
    }

    disk_groups_generic = ["object-name", "name", "size", "freespace", "storage-type", "pool",
         "pool-serial-number", "pool-percentage", "owner", "raidtype", "status", "create-date",
         "disk-description", "serial-number", "pool-sector-format", "health", "health-reason",
         "health-recommendation"]

    volumes_generic = ["volume-description", "blocks", "health", "size", "volume-name", "wwn",
         "storage-pool-name", "total-size", "volume-class", "allocated-size", "owner", "object-name",
         "raidtype", "health-reason", "progress", "blocksize", "serial-number", "virtual-disk-serial",
         "write-policy", "volume-type", "health-recommendation", "virtual-disk-name", "storage-type",
         "capabilities"]

    volumes_extended = ["cache-optimization", "container-serial", "cs-primary", "replication-set",
         "attributes", "preferred-owner", "volume-parent", "allowed-storage-tiers", "cs-copy-dest",
         "cs-copy-src", "container-name", "group-key", "snapshot-retention-priority", "pi-format",
         "reserved-size-in-pages", "cs-secondary", "volume-group", "health-numeric",
         "large-virtual-extents", "cs-replication-role", "durable-id", "threshold-percent-of-pool",
         "tier-affinity", "volume-qualifier", "snapshot", "snap-pool", "read-ahead-size",
         "zero-init-page-on-allocation", "allocate-reserved-pages-first"]

    # Logical Volumes directory name
    LOGICAL_VOLUMES_DIR = "logical_volumes"

    @staticmethod
    def name():
        """@return: name of the monitoring module."""
        return RealStorLogicalVolumeSensor.SENSOR_NAME

    @staticmethod
    def dependencies():
        """Returns a list of plugins and RPMs this module requires
           to function.
        """
        return RealStorLogicalVolumeSensor.DEPENDENCIES

    def __init__(self):
        super(RealStorLogicalVolumeSensor, self).__init__(
            self.SENSOR_NAME, self.PRIORITY)

        self._faulty_disk_group_file_path = None

        self.rssencl = singleton_realstorencl

        # logical volumes persistent cache
        self._logical_volume_prcache = None

        # Holds Logical Volumes with faults. Used for future reference.
        self._previously_faulty_disk_groups = {}

        self.pollfreq_logical_volume_sensor = \
            int(self.rssencl.conf_reader._get_value_with_default(\
                self.rssencl.CONF_REALSTORLOGICALVOLUMESENSOR,\
                "polling_frequency_override", 0))

        if self.pollfreq_logical_volume_sensor == 0:
                self.pollfreq_logical_volume_sensor = self.rssencl.pollfreq

        # Flag to indicate suspension of module
        self._suspended = False

        self._event = Event()

    def initialize(self, conf_reader, msgQlist, products):
        """initialize configuration reader and internal msg queues"""

        # Initialize ScheduledMonitorThread and InternalMsgQ
        super(RealStorLogicalVolumeSensor, self).initialize(conf_reader)

        # Initialize internal message queues for this module
        super(RealStorLogicalVolumeSensor, self).initialize_msgQ(msgQlist)

        self._logical_volume_prcache = os.path.join(self.rssencl.frus,\
             self.LOGICAL_VOLUMES_DIR)

        # Persistence file location. This file stores faulty Logical Volume data
        self._faulty_disk_group_file_path = os.path.join(
            self._logical_volume_prcache, "logicalvolumedata.json")

        # Load faulty Logical Volume data from file if available
        self._previously_faulty_disk_groups = store.get(\
                                                  self._faulty_disk_group_file_path)

        if self._previously_faulty_disk_groups is None:
            self._previously_faulty_disk_groups = {}
            store.put(self._previously_faulty_disk_groups,\
                self._faulty_disk_group_file_path)

        return True

    def read_data(self):
        """This method is part of interface. Currently it is not
        in use.
        """
        return {}

    def run(self):
        """Run the sensor on its own thread"""

        # Do not proceed if module is suspended
        if self._suspended == True:
            self._scheduler.enter(10, self._priority, self.run, ())
            return
        # Check for debug mode being activated
        self._read_my_msgQ_noWait()

        disk_groups = None
        try:
            disk_groups = self._get_disk_groups()

            if disk_groups:
                self._get_msgs_for_faulty_disk_groups(disk_groups)

        except Exception as exception:
            logger.exception(exception)

        # Reset debug mode if persistence is not enabled
        self._disable_debug_if_persist_false()

        # Fire every 10 seconds to see if We have a faulty Logical Volume
        self._scheduler.enter(self.pollfreq_logical_volume_sensor,
                self._priority, self.run, ())

    def _get_disk_groups(self):
        """Receives list of Disk Groups from API.
           URL: http://<host>/api/show/disk-groups
        """
        url = self.rssencl.build_url(self.rssencl.URI_CLIAPI_SHOWDISKGROUPS)

        response = self.rssencl.ws_request(url, self.rssencl.ws.HTTP_GET)

        if not response:
            logger.warn(f"{self.rssencl.EES_ENCL}:: Disk Groups status unavailable as ws request {url} failed")
            return

        if response.status_code != self.rssencl.ws.HTTP_OK:
            if url.find(self.rssencl.ws.LOOPBACK) == -1:
                logger.error(f"{self.rssencl.EES_ENCL}:: http request {url} to get disk groups failed with  \
                     err {response.status_code}")
            return

        response_data = json.loads(response.text)
        disk_groups = response_data.get("disk-groups")
        return disk_groups

    def _get_logical_volumes(self, pool_serial_number):
        """Receives list of Logical Volumes from API.
           URL: http://<host>/api/show/volumes/pool/<pool_serial_number>
        """
        url = self.rssencl.build_url(self.rssencl.URI_CLIAPI_SHOWVOLUMES)

        url = f"{url}/pool/{pool_serial_number}"

        response = self.rssencl.ws_request(url, self.rssencl.ws.HTTP_GET)

        if not response:
            logger.warn(f"{self.rssencl.EES_ENCL}:: Logical Volume status unavailable as ws request {url}"
                " failed")
            return

        if response.status_code != self.rssencl.ws.HTTP_OK:
            logger.error(f"{self.rssencl.EES_ENCL}:: http request {url} to get logical volumes failed with \
                 err {response.status_code}")
            return

        response_data = json.loads(response.text)
        logical_volumes = response_data.get("volumes")
        return logical_volumes

    def _get_msgs_for_faulty_disk_groups(self, disk_groups, send_message=True):
        """Checks for health of logical volumes and returns list of messages to be
           sent to handler if there are any.
        """
        faulty_disk_group_messages = []
        internal_json_msg = None
        disk_group_health = None
        serial_number = None
        alert_type = ""
        logical_volumes = None
        # Flag to indicate if there is a change in _previously_faulty_disk_groups
        state_changed = False

        if not disk_groups:
            return
        for disk_group in disk_groups:
            disk_group_health = disk_group["health"].lower()
            pool_serial_number = disk_group["pool-serial-number"]
            serial_number = disk_group["serial-number"]
            # Check for missing and fault case
            if disk_group_health == self.rssencl.HEALTH_FAULT:
                # Status change from Degraded ==> Fault or OK ==> Fault
                if (serial_number in self._previously_faulty_disk_groups and \
                        self._previously_faulty_disk_groups[serial_number]['health']=="degraded") or \
                        (serial_number not in self._previously_faulty_disk_groups):
                    alert_type = self.rssencl.FRU_FAULT
                    self._previously_faulty_disk_groups[serial_number] = {
                        "health": disk_group_health, "alert_type": alert_type}
                    state_changed : bool = True
                    logical_volumes = self._get_logical_volumes(pool_serial_number)
                    for logical_volume in logical_volumes:
                        internal_json_msg = self._create_internal_msg(
                            logical_volume, alert_type, disk_group)
                        faulty_disk_group_messages.append(internal_json_msg)
                        # Send message to handler
                        if send_message:
                            self._send_json_msg(internal_json_msg)
                            internal_json_msg = None
            # Check for fault case
            elif disk_group_health == self.rssencl.HEALTH_DEGRADED:
                # Status change from Fault ==> Degraded or OK ==> Degraded
                if (serial_number in self._previously_faulty_disk_groups and \
                        self._previously_faulty_disk_groups[serial_number]['health']=="fault") or \
                        (serial_number not in self._previously_faulty_disk_groups):
                    alert_type = self.rssencl.FRU_FAULT
                    self._previously_faulty_disk_groups[serial_number] = {
                        "health": disk_group_health, "alert_type": alert_type}
                    state_changed : bool = True
                    logical_volumes = self._get_logical_volumes(pool_serial_number)
                    for logical_volume in logical_volumes:
                        internal_json_msg = self._create_internal_msg(
                            logical_volume, alert_type, disk_group)
                        faulty_disk_group_messages.append(internal_json_msg)
                        # Send message to handler
                        if send_message:
                            self._send_json_msg(internal_json_msg)
            # Check for healthy case
            elif disk_group_health == self.rssencl.HEALTH_OK:
                # Status change from Fault ==> OK or Degraded ==> OK
                if serial_number in self._previously_faulty_disk_groups:
                    # Send message to handler
                    if send_message:
                        alert_type = self.rssencl.FRU_FAULT_RESOLVED
                        logical_volumes = self._get_logical_volumes(pool_serial_number)
                        for logical_volume in logical_volumes:
                            internal_json_msg = self._create_internal_msg(
                                logical_volume, alert_type, disk_group)
                            faulty_disk_group_messages.append(internal_json_msg)
                            self._send_json_msg(internal_json_msg)
                    del self._previously_faulty_disk_groups[serial_number]
                    state_changed = True
            # Persist faulty Logical Volume list to file only if something is changed
            if state_changed:
                # Wait till msg is sent to rabbitmq or added in consul for resending.
                # If timed out, do not update cache and revert in-memory cache.
                # So, in next iteration change can be detected
                if self._event.wait(self.rssencl.PERSISTENT_DATA_UPDATE_TIMEOUT):
                    store.put(self._previously_faulty_disk_groups,\
                        self._faulty_disk_group_file_path)
                else:
                    self._previously_faulty_disk_groups = store.get(self._faulty_disk_group_file_path)
                state_changed = False
            alert_type = ""
        return faulty_disk_group_messages

    def _create_internal_msg(self, logical_volume_detail, alert_type, disk_group):
        """Forms a dictionary containing info about Logical Volumes to send to
           message handler.
        """
        if not logical_volume_detail:
            return {}

        generic_info = dict.fromkeys(self.volumes_generic, "NA")
        extended_info = dict.fromkeys(self.volumes_extended, "NA")
        disk_groups_info = dict.fromkeys(self.disk_groups_generic, "NA")

        severity_reader = SeverityReader()
        severity = severity_reader.map_severity(alert_type)
        epoch_time = str(int(time.time()))

        alert_id = self._get_alert_id(epoch_time)
        resource_id = logical_volume_detail.get("volume-name", "")
        host_name = socket.gethostname()

        for key, value in logical_volume_detail.items():
            if key in self.volumes_generic:
                generic_info.update({key : value})
            elif key in self.volumes_extended:
                extended_info.update({key : value})

        for key, value in disk_group.items():
            if key in self.disk_groups_generic:
                disk_groups_info.update({key : value})
        generic_info['disk-group'] = [disk_groups_info]
        generic_info.update(extended_info)

        info = {
                "site_id": self.rssencl.site_id,
                "cluster_id": self.rssencl.cluster_id,
                "rack_id": self.rssencl.rack_id,
                "node_id": self.rssencl.node_id,
                "resource_type": self.RESOURCE_TYPE,
                "resource_id": resource_id,
                "event_time": epoch_time
                }

        internal_json_msg = json.dumps(
            {"sensor_request_type": {
                "enclosure_alert": {
                    "host_id": host_name,
                    "severity": severity,
                    "alert_id": alert_id,
                    "alert_type": alert_type,
                    "status": "update",
                    "info": info,
                    "specific_info": generic_info
                }
            }})
        return internal_json_msg

    def _get_alert_id(self, epoch_time):
        """Returns alert id which is a combination of
           epoch_time and salt value
        """
        salt = str(uuid.uuid4().hex)
        alert_id = epoch_time + salt
        return alert_id

    def _send_json_msg(self, json_msg):
        """Sends JSON message to Handler"""
        if not json_msg:
            return

        self._event.clear()
        self._write_internal_msgQ(RealStorEnclMsgHandler.name(), json_msg, self._event)

    def suspend(self):
        """Suspends the module thread. It should be non-blocking"""
        super(RealStorLogicalVolumeSensor, self).suspend()
        self._suspended = True

    def resume(self):
        """Resumes the module thread. It should be non-blocking"""
        super(RealStorLogicalVolumeSensor, self).resume()
        self._suspended = False

    def shutdown(self):
        """Clean up scheduler queue and gracefully shutdown thread"""
        super(RealStorLogicalVolumeSensor, self).shutdown()
