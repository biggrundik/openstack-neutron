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
#

import abc
import copy
import random

from oslo_log import log

from neutron.common.ovn import constants as ovn_const
from neutron.common.ovn import utils
from neutron.conf.plugins.ml2.drivers.ovn import ovn_conf


LOG = log.getLogger(__name__)

OVN_SCHEDULER_CHANCE = 'chance'
OVN_SCHEDULER_LEAST_LOADED = 'leastloaded'


class OVNGatewayScheduler(object, metaclass=abc.ABCMeta):

    def __init__(self):
        pass

    @abc.abstractmethod
    def select(self, nb_idl, gateway_name, candidates=None,
               existing_chassis=None):
        """Schedule the gateway port of a router to an OVN chassis.

        Schedule the gateway router port only if it is not already
        scheduled.
        """

    def filter_existing_chassis(self, nb_idl, gw_chassis,
                                physnet, chassis_physnets,
                                existing_chassis, az_hints, chassis_with_azs):
        chassis_list = copy.copy(existing_chassis)
        for chassis_name in existing_chassis:
            if utils.is_gateway_chassis_invalid(chassis_name, gw_chassis,
                                                physnet, chassis_physnets,
                                                az_hints, chassis_with_azs):
                LOG.debug("Chassis %(chassis)s is invalid for scheduling "
                          "router in physnet: %(physnet)s.",
                          {'chassis': chassis_name,
                           'physnet': physnet})
                chassis_list.remove(chassis_name)
        return chassis_list

    def _schedule_gateway(self, nb_idl, gateway_name, candidates,
                          existing_chassis):
        existing_chassis = existing_chassis or []
        candidates = candidates or []
        candidates = list(set(candidates) - set(existing_chassis))
        # If no candidates, or gateway scheduled on MAX_GATEWAY_CHASSIS nodes
        # or all candidates in existing_chassis, return existing_chassis.
        # Otherwise, if more candidates present, then schedule them.
        if existing_chassis:
            if not candidates or (
                    len(existing_chassis) == ovn_const.MAX_GW_CHASSIS):
                return existing_chassis
        if not candidates:
            LOG.warning('Gateway %s was not scheduled on any chassis, no '
                        'candidates are available', gateway_name)
            return [ovn_const.OVN_GATEWAY_INVALID_CHASSIS]
        chassis_count = min(
            ovn_const.MAX_GW_CHASSIS - len(existing_chassis),
            len(candidates)
        )
        # The actual binding of the gateway to a chassis via the options
        # column or gateway_chassis column in the OVN_Northbound is done
        # by the caller
        chassis = self._select_gateway_chassis(
            nb_idl, candidates, 1, chassis_count
        )[:chassis_count]
        # priority of existing chassis is higher than candidates
        chassis = existing_chassis + chassis

        LOG.debug("Gateway %s scheduled on chassis %s",
                  gateway_name, chassis)
        return chassis

    @abc.abstractmethod
    def _select_gateway_chassis(self, nb_idl, candidates,
                                priority_min, priority_max):
        """Choose a chassis from candidates based on a specific policy.

        Returns a list of chassis to use for scheduling. The value at
        ``ret[0]`` will be used for the chassis with ``priority_max``, the
        value at ``ret[-1]`` will be used for the chassis with ``priority_min``
        """


class OVNGatewayChanceScheduler(OVNGatewayScheduler):
    """Randomly select an chassis for a gateway port of a router"""

    def select(self, nb_idl, gateway_name, candidates=None,
               existing_chassis=None):
        return self._schedule_gateway(nb_idl, gateway_name,
                                      candidates, existing_chassis)

    def _select_gateway_chassis(self, nb_idl, candidates,
                                priority_min, priority_max):
        candidates = copy.deepcopy(candidates)
        random.shuffle(candidates)
        return candidates


class OVNGatewayLeastLoadedScheduler(OVNGatewayScheduler):
    """Select the least loaded chassis for a gateway port of a router"""

    def select(self, nb_idl, gateway_name, candidates=None,
               existing_chassis=None):
        return self._schedule_gateway(nb_idl, gateway_name,
                                      candidates, existing_chassis)

    def _select_gateway_chassis(self, nb_idl, candidates,
                                priority_min, priority_max):
        """Returns a lit of chassis from candidates ordered by priority
        (highest first). Each chassis in every priority will be selected, as it
        is the least loaded for that specific priority.
        """
        selected_chassis = []
        priorities = list(range(priority_max, priority_min - 1, -1))
        all_chassis_bindings = nb_idl.get_all_chassis_gateway_bindings(
                candidates, priorities=priorities)
        for priority in priorities:
            chassis_load = {}
            for chassis, lrps in all_chassis_bindings.items():
                if chassis in selected_chassis:
                    continue
                chassis_load[chassis] = len(
                    [lrp for lrp, prio in lrps if prio == priority])
            if len(chassis_load) == 0:
                break
            leastload = min(chassis_load.values())
            chassis = random.choice(
                [chassis for chassis, load in chassis_load.items()
                 if load == leastload])
            selected_chassis.append(chassis)
        return selected_chassis


OVN_SCHEDULER_STR_TO_CLASS = {
    OVN_SCHEDULER_CHANCE: OVNGatewayChanceScheduler,
    OVN_SCHEDULER_LEAST_LOADED: OVNGatewayLeastLoadedScheduler}


def get_scheduler():
    return OVN_SCHEDULER_STR_TO_CLASS[ovn_conf.get_ovn_l3_scheduler()]()
