import os
import json
import sys
import uuid
import netifaces as ni
import warnings
import contextlib
import requests
from datetime import datetime
from simple_rest_client.api import API
from simple_rest_client.resource import Resource
from basicauth import encode
from pprint import pprint
from random import randint
from urllib3.exceptions import InsecureRequestWarning


old_merge_environment_settings = requests.Session.merge_environment_settings

@contextlib.contextmanager
def _no_ssl_verification():
    opened_adapters = set()

    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        # Verification happens only once per connection so we need to close
        # all the opened adapters once we're done. Otherwise, the effects of
        # verify=False persist beyond the end of this context manager.
        opened_adapters.add(self.get_adapter(url))

        settings = old_merge_environment_settings(self, url, proxies, stream, verify, cert)
        settings['verify'] = False

        return settings

    requests.Session.merge_environment_settings = merge_environment_settings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', InsecureRequestWarning)
            yield
    finally:
        requests.Session.merge_environment_settings = old_merge_environment_settings

        for adapter in opened_adapters:
            try:
                adapter.close()
            except:
                pass


def _get_aai_rel_link_data(data, related_to, search_key=None, match_dict=None):
    # some strings that we will encounter frequently
    rel_lst = "relationship-list"
    rkey = "relationship-key"
    rval = "relationship-value"
    rdata = "relationship-data"
    response = list()
    if match_dict:
        m_key = match_dict.get('key')
        m_value = match_dict.get('value')
    else:
        m_key = None
        m_value = None
    rel_dict = data.get(rel_lst)
    if rel_dict:  # check if data has relationship lists
        for key, rel_list in rel_dict.items():
            for rel in rel_list:
                if rel.get("related-to") == related_to:
                    dval = None
                    matched = False
                    link = rel.get("related-link")
                    r_data = rel.get(rdata, [])
                    if search_key:
                        for rd in r_data:
                            if rd.get(rkey) == search_key:
                                dval = rd.get(rval)
                                if not match_dict:  # return first match
                                    response.append(
                                        {"link": link, "d_value": dval}
                                    )
                                    break  # go to next relation
                            if rd.get(rkey) == m_key \
                                    and rd.get(rval) == m_value:
                                matched = True
                        if match_dict and matched:  # if matching required
                            response.append(
                                {"link": link, "d_value": dval}
                            )
                            # matched, return search value corresponding
                            # to the matched r_data group
                    else:  # no search key; just return the link
                        response.append(
                            {"link": link, "d_value": dval}
                        )
    if len(response) == 0:
        response.append(
            {"link": None, "d_value": None}
        )
    return response


class AAIResource(Resource):
    actions = {
        'generic_vnf': {'method': 'GET', 'url': 'network/generic-vnfs/generic-vnf/{}'},
        'link': {'method': 'GET', 'url': '{}'},
        'service_instance': {'method': 'GET',
                             'url': 'business/customers/customer/{}/service-subscriptions/service-subscription/{}/service-instances/service-instance/{}'}
    }


def _init_python_aai_api(onap_ip):
    api = API(
        api_root_url="https://{}:30233/aai/v14/".format(onap_ip),
        params={},
        headers={
            'Authorization': encode("AAI", "AAI"),
            'X-FromAppId': 'SCRIPT',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-TransactionId': str(uuid.uuid4()),
        },
        timeout=30,
        append_slash=False,
        json_encode_body=True # encode body as json
    )
    api.add_resource(resource_name='aai', resource_class=AAIResource)
    return api


def load_aai_data(vfw_vnf_id, onap_ip):
    api = _init_python_aai_api(onap_ip)
    aai_data = {}
    aai_data['service-info'] = {'global-customer-id': '', 'service-instance-id': '', 'service-type': ''}
    aai_data['vfw-model-info'] = {'model-invariant-id': '', 'model-version-id': '', 'vnf-name': ''}
    aai_data['vpgn-model-info'] = {'model-invariant-id': '', 'model-version-id': '', 'vnf-name': ''}
    with _no_ssl_verification():
        response = api.aai.generic_vnf(vfw_vnf_id, body=None, params={'depth': 2}, headers={})
        aai_data['vfw-model-info']['model-invariant-id'] = response.body.get('model-invariant-id')
        aai_data['vfw-model-info']['model-version-id'] = response.body.get('model-version-id')
        aai_data['vfw-model-info']['vnf-name'] = response.body.get('vnf-name')

        related_to = "service-instance"
        search_key = "customer.global-customer-id"
        rl_data_list = _get_aai_rel_link_data(data=response.body, related_to=related_to, search_key=search_key)
        aai_data['service-info']['global-customer-id'] = rl_data_list[0]['d_value']

        search_key = "service-subscription.service-type"
        rl_data_list = _get_aai_rel_link_data(data=response.body, related_to=related_to, search_key=search_key)
        aai_data['service-info']['service-type'] = rl_data_list[0]['d_value']

        search_key = "service-instance.service-instance-id"
        rl_data_list = _get_aai_rel_link_data(data=response.body, related_to=related_to, search_key=search_key)
        aai_data['service-info']['service-instance-id'] = rl_data_list[0]['d_value']

        service_link = rl_data_list[0]['link']
        response = api.aai.link(service_link, body=None, params={}, headers={})

        related_to = "generic-vnf"
        search_key = "generic-vnf.vnf-id"
        rl_data_list = _get_aai_rel_link_data(data=response.body, related_to=related_to, search_key=search_key)
        for i in range(0, len(rl_data_list)):
            vnf_id = rl_data_list[i]['d_value']

            if vnf_id != vfw_vnf_id:
                vnf_link = rl_data_list[i]['link']
                response = api.aai.link(vnf_link, body=None, params={}, headers={})
                if aai_data['vfw-model-info']['model-invariant-id'] != response.body.get('model-invariant-id'):
                    aai_data['vpgn-model-info']['model-invariant-id'] = response.body.get('model-invariant-id')
                    aai_data['vpgn-model-info']['model-version-id'] = response.body.get('model-version-id')
                    aai_data['vpgn-model-info']['vnf-name'] = response.body.get('vnf-name')
                    break
    return aai_data


def execute_workflow(vfw_vnf_id, onap_ip):
    print("Executing workflow for VNF ID '{}' on ONAP with IP {}".format(vfw_vnf_id, onap_ip))
    aai_data = load_aai_data(vfw_vnf_id, onap_ip)

    print(json.dumps(aai_data, indent=4))

execute_workflow(sys.argv[1], sys.argv[2])
