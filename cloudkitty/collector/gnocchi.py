# -*- coding: utf-8 -*-
# Copyright 2015 Objectif Libre
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
import decimal

from gnocchiclient import client as gclient
from keystoneauth1 import loading as ks_loading
from oslo_config import cfg

from cloudkitty import collector

GNOCCHI_COLLECTOR_OPTS = 'gnocchi_collector'
ks_loading.register_session_conf_options(
    cfg.CONF,
    GNOCCHI_COLLECTOR_OPTS)
ks_loading.register_auth_conf_options(
    cfg.CONF,
    GNOCCHI_COLLECTOR_OPTS)
CONF = cfg.CONF


class GnocchiCollector(collector.BaseCollector):
    collector_name = 'gnocchi'
    dependencies = ('GnocchiTransformer',
                    'CloudKittyFormatTransformer')
    retrieve_mappings = {
        'compute': 'instance',
        'image': 'image',
        'volume': 'volume',
        'network.bw.out': 'instance_network_interface',
        'network.bw.in': 'instance_network_interface',
    }
    metrics_mappings = {
        'compute': [
            ('vcpus', 'max'),
            ('memory', 'max'),
            ('cpu', 'max'),
            ('disk.root.size', 'max'),
            ('disk.ephemeral.size', 'max')],
        'image': [
            ('image.size', 'max'),
            ('image.download', 'max'),
            ('image.serve', 'max')],
        'volume': [
            ('volume.size', 'max')],
        'network.bw.out': [
            ('network.outgoing.bytes', 'max')],
        'network.bw.in': [
            ('network.incoming.bytes', 'max')],
    }
    units_mappings = {
        'compute': (1, 'instance'),
        'image': ('image.size', 'MB'),
        'volume': ('volume.size', 'GB'),
        'network.bw.out': ('network.outgoing.bytes', 'MB'),
        'network.bw.in': ('network.incoming.bytes', 'MB'),
    }
    default_unit = (1, 'unknown')

    def __init__(self, transformers, **kwargs):
        super(GnocchiCollector, self).__init__(transformers, **kwargs)

        self.t_gnocchi = self.transformers['GnocchiTransformer']
        self.t_cloudkitty = self.transformers['CloudKittyFormatTransformer']

        self.auth = ks_loading.load_auth_from_conf_options(
            CONF,
            GNOCCHI_COLLECTOR_OPTS)
        self.session = ks_loading.load_session_from_conf_options(
            CONF,
            GNOCCHI_COLLECTOR_OPTS,
            auth=self.auth)
        self._conn = gclient.Client(
            '1',
            session=self.session)

    @classmethod
    def gen_filter(cls, cop='=', lop='and', **kwargs):
        """Generate gnocchi filter from kwargs.

        :param cop: Comparison operator.
        :param lop: Logical operator in case of multiple filters.
        """
        q_filter = []
        for kwarg in sorted(kwargs):
            q_filter.append({cop: {kwarg: kwargs[kwarg]}})
        if len(kwargs) > 1:
            return cls.extend_filter(q_filter, lop=lop)
        else:
            return q_filter[0] if len(kwargs) else {}

    @classmethod
    def extend_filter(cls, *args, **kwargs):
        """Extend an existing gnocchi filter with multiple operations.

        :param lop: Logical operator in case of multiple filters.
        """
        lop = kwargs.get('lop', 'and')
        filter_list = []
        for cur_filter in args:
            if isinstance(cur_filter, dict) and cur_filter:
                filter_list.append(cur_filter)
            elif isinstance(cur_filter, list):
                filter_list.extend(cur_filter)
        if len(filter_list) > 1:
            return {lop: filter_list}
        else:
            return filter_list[0] if len(filter_list) else {}

    def _generate_time_filter(self, start, end=None, with_revision=False):
        """Generate timeframe filter.

        :param start: Start of the timeframe.
        :param end: End of the timeframe if needed.
        :param with_revision: Filter on the resource revision.
        :type with_revision: bool
        """
        time_filter = list()
        time_filter.append(self.extend_filter(
            self.gen_filter(ended_at=None),
            self.gen_filter(cop=">=", ended_at=start),
            lop='or'))
        if end:
            time_filter.append(self.extend_filter(
                self.gen_filter(ended_at=None),
                self.gen_filter(cop="<=", ended_at=end),
                lop='or'))
            time_filter.append(
                self.gen_filter(cop="<=", started_at=end))
            if with_revision:
                time_filter.append(
                    self.gen_filter(cop="<=", revision_start=end))
        return time_filter

    def _expand_metrics(self, resources, mappings, start, end=None):
        for resource in resources:
            metrics = resource.get('metrics', {})
            for name, aggregate in mappings:
                try:
                    values = self._conn.metric.get_measures(
                        metric=metrics[name],
                        start=start,
                        stop=end,
                        aggregation=aggregate)
                    # NOTE(sheeprine): Get the list of values for the current
                    # metric and get the first result value.
                    # [point_date, granularity, value]
                    # ["2015-11-24T00:00:00+00:00", 86400.0, 64.0]
                    resource[name] = values[0][2]
                except IndexError:
                    resource[name] = None
                except KeyError:
                    # Skip metrics not found
                    pass

    def get_resources(self,
                      resource_name,
                      start,
                      end=None,
                      resource_id=None,
                      project_id=None,
                      reverse_revision=False,
                      q_filter=None):
        """Get resources during the timeframe.

        Set the resource_id if you want to get a specific resource.
        :param resource_name: Resource name to filter on.
        :type resource_name: str
        :param start: Start of the timeframe.
        :param end: End of the timeframe if needed.
        :param resource_id: Retrieve a specific resource based on its id.
        :type resource_id: str
        :param project_id: Filter on a specific tenant/project.
        :type project_id: str
        :param reverse_revision: Reverse the revision information from search.
        :type reverse_revision: str
        :param q_filter: Append a custom filter.
        :type q_filter: list
        """
        # NOTE(sheeprine): We first get the list of every resource running
        # without any details or history.
        # Then we get information about the resource getting details and
        # history.

        # Translating the resource name if needed
        resource_type = self.retrieve_mappings.get(
            resource_name,
            resource_name)
        query_parameters = self._generate_time_filter(
            start,
            end,
            with_revision=True if resource_id and not reverse_revision
            else False)
        if resource_id:
            query_parameters.append(
                self.gen_filter(id=resource_id))
        else:
            query_parameters.append(
                self.gen_filter(cop="=", type=resource_type))
        if project_id:
            query_parameters.append(
                self.gen_filter(project_id=project_id))
        if q_filter:
            query_parameters.append(q_filter)
        search_opts = {
            'history': True,
            'limit': 1,
            'sorts': [
                'revision_start:desc' if not reverse_revision
                else 'revision_start:asc']} if resource_id else dict()
        resources = self._conn.resource.search(
            resource_type='generic' if end and not resource_id
            else resource_type,
            query=self.extend_filter(*query_parameters),
            **search_opts)
        if resource_id or not end:
            if not resources:
                resources = self.get_resources(
                    resource_type,
                    start,
                    end,
                    resource_id=resource_id,
                    reverse_revision=True)
            return resources
        result = []
        for resource in resources:
            populated_resource = self.get_resources(
                resource_type,
                start,
                end,
                resource_id=resource.get('id', ''))[0]
            result.append(populated_resource)
        return result

    def resource_info(self,
                      resource_name,
                      start,
                      end=None,
                      resource_id=None,
                      project_id=None,
                      reverse_revision=False,
                      q_filter=None):
        qty, unit = self.units_mappings.get(
            resource_name,
            self.default_unit)
        resources = self.get_resources(
            resource_name,
            start,
            end,
            resource_id=resource_id,
            project_id=project_id,
            q_filter=q_filter)
        formated_resources = list()
        for resource in resources:
            resource_data = self.t_gnocchi.strip_resource_data(
                resource_name,
                resource)
            self._expand_metrics(
                [resource_data],
                self.metrics_mappings[resource_name],
                start,
                end)
            resource_data.pop('metrics', None)
            data = self.t_cloudkitty.format_item(
                resource_data,
                unit,
                decimal.Decimal(
                    qty if isinstance(qty, int) else resource_data[qty]))
            # NOTE(sheeprine): Reference to gnocchi resource used by storage
            data['resource_id'] = data['desc']['resource_id']
            formated_resources.append(data)
        return formated_resources

    def generic_retrieve(self,
                         resource_name,
                         start,
                         end=None,
                         project_id=None,
                         q_filter=None):
        resources = self.resource_info(
            resource_name,
            start,
            end,
            project_id=project_id,
            q_filter=q_filter)
        if not resources:
            raise collector.NoDataCollected(self.collector_name, resource_name)
        return self.t_cloudkitty.format_service(
            resource_name,
            resources)

    def retrieve(self,
                 resource,
                 start,
                 end=None,
                 project_id=None,
                 q_filter=None):
        trans_resource = resource.replace('_', '.')
        return self.generic_retrieve(
            trans_resource,
            start,
            end,
            project_id,
            q_filter)
