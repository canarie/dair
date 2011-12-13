# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Openstack, LLC.
# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Hardware Aware Scheduler
"""

import datetime

from nova import db
from nova import flags
from nova import log as logging
from nova.scheduler import driver
from nova.scheduler import simple 

LOG = logging.getLogger('nova.scheduler.hardware')

FLAGS = flags.FLAGS

class HardwareScheduler(simple.SimpleScheduler):
    """Implements Naive Scheduler that tries to find least loaded host taking into account the hardware available on each host."""

    def schedule_run_instance(self, context, instance_id, *_args, **_kwargs):
        """Picks a host that is up and has the fewest running instances."""
        instance_ref = db.instance_get(context, instance_id)

        if (instance_ref['availability_zone']
            and ':' in instance_ref['availability_zone']
            and context.is_admin):

            zone, _x, host = instance_ref['availability_zone'].partition(':')
            service = db.service_get_by_args(context.elevated(), host,
                                             'nova-compute')
            if not self.service_is_up(service):
                raise driver.WillNotSchedule(_("Host %s is not alive") % host)

            # TODO(vish): this probably belongs in the manager, if we
            #             can generalize this somehow
            now = datetime.datetime.utcnow()
            db.instance_update(context, instance_id, {'host': host,
                                                      'scheduled_at': now})
            return host

        results = db.service_get_all_compute_sorted(context)

        for result in results:
            (service, instance_cores) = result
            
            compute_ref = db.service_get_all_compute_by_host(context, service['host'])[0]
            compute_node_ref = compute_ref['compute_node'][0]

            if (instance_ref['vcpus'] + instance_cores > compute_node_ref['vcpus'] * FLAGS.max_cores):
                raise driver.NoValidHost(_("All hosts have too many cores"))

            LOG.debug(_("requested instance cores = %s + used compute node cores = %s < total compute node cores = %s * max cores = %s") % 
                       (instance_ref['vcpus'], instance_cores, compute_node_ref['vcpus'], FLAGS.max_cores))

            if self.service_is_up(service):
                # NOTE(vish): this probably belongs in the manager, if we
                #             can generalize this somehow
                now = datetime.datetime.utcnow()
                db.instance_update(context,
                                   instance_id,
                                   {'host': service['host'],
                                    'scheduled_at': now})

                LOG.debug(_("instance = %s scheduled to host = %s") % (instance_id, service['host']))

                return service['host']

        raise driver.NoValidHost(_("Scheduler was unable to locate a host"
                                   " for this request. Is the appropriate"
                                   " service running?"))

    def schedule_create_volume(self, context, volume_id, *_args, **_kwargs):
        """Picks a host that is up and has the fewest volumes."""
        volume_ref = db.volume_get(context, volume_id)
        if (volume_ref['availability_zone']
            and ':' in volume_ref['availability_zone']
            and context.is_admin):
            zone, _x, host = volume_ref['availability_zone'].partition(':')
            service = db.service_get_by_args(context.elevated(), host,
                                             'nova-volume')
            if not self.service_is_up(service):
                raise driver.WillNotSchedule(_("Host %s not available") % host)

            # TODO(vish): this probably belongs in the manager, if we
            #             can generalize this somehow
            now = datetime.datetime.utcnow()
            db.volume_update(context, volume_id, {'host': host,
                                                  'scheduled_at': now})
            return host

        results = db.service_get_all_volume_sorted(context)

        for result in results:
            (service, volume_gigabytes) = result

            compute_ref = db.service_get_all_compute_by_host(context, service['host'])[0]
            compute_node_ref = compute_ref['compute_node'][0]

            if volume_ref['size'] + volume_gigabytes > compute_node_ref['local_gb']:
                raise driver.NoValidHost(_("All hosts have too many "
                                           "gigabytes"))

            LOG.debug(_("requested volume GBs = %s + used compute node GBs = %s < total compute node GBs = %s") % (volume_ref['size'], volume_gigabytes, compute_node_ref['local_gb']))

            if self.service_is_up(service):
                # NOTE(vish): this probably belongs in the manager, if we
                #             can generalize this somehow
                now = datetime.datetime.utcnow()
                db.volume_update(context,
                                 volume_id,
                                 {'host': service['host'],
                                  'scheduled_at': now})

                LOG.debug(_("volume = %s scheduled to host = %s") % (volume_id, service['host']))

                return service['host']
        raise driver.NoValidHost(_("Scheduler was unable to locate a host"
                                   " for this request. Is the appropriate"
                                   " service running?"))

