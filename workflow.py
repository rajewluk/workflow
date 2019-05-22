import os
import json
import sys
import uuid
import time
import copy
import netifaces as ni
import warnings
import contextlib
import requests
from datetime import datetime
from datetime import timedelta
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


class AAIApiResource(Resource):
    actions = {
        'generic_vnf': {'method': 'GET', 'url': 'network/generic-vnfs/generic-vnf/{}'},
        'link': {'method': 'GET', 'url': '{}'},
        'service_instance': {'method': 'GET',
                             'url': 'business/customers/customer/{}/service-subscriptions/service-subscription/{}/service-instances/service-instance/{}'}
    }


class HASApiResource(Resource):
    actions = {
        'plans': {'method': 'POST', 'url': 'plans/'},
        'plan': {'method': 'GET', 'url': 'plans/{}'}
    }


class APPCLcmApiResource(Resource):
    actions = {
        'distribute_traffic': {'method': 'POST', 'url': 'appc-provider-lcm:distribute-traffic/'},
        'distribute_traffic_check': {'method': 'POST', 'url': 'appc-provider-lcm:distribute-traffic-check/'},
        'action_status': {'method': 'POST', 'url': 'appc-provider-lcm:action-status/'},
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
    api.add_resource(resource_name='aai', resource_class=AAIApiResource)
    return api


def _init_python_has_api(onap_ip):
    api = API(
        api_root_url="https://{}:30275/v1/".format(onap_ip),
        params={},
        headers={
            'Authorization': encode("admin1", "plan.15"),
            'X-FromAppId': 'SCRIPT',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-TransactionId': str(uuid.uuid4()),
        },
        timeout=30,
        append_slash=False,
        json_encode_body=True # encode body as json
    )
    api.add_resource(resource_name='has', resource_class=HASApiResource)
    return api


def _init_python_appc_lcm_api(onap_ip):
    api = API(
        api_root_url="http://{}:30230/restconf/operations/".format(onap_ip),
        params={},
        headers={
            'Authorization': encode("admin", "Kp8bJ4SXszM0WXlhak3eHlcse2gAw84vaoGGmJvUy2U"),
            'X-FromAppId': 'SCRIPT',
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        },
        timeout=300,
        append_slash=False,
        json_encode_body=True # encode body as json
    )
    api.add_resource(resource_name='lcm', resource_class=APPCLcmApiResource)
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
        aai_data['vf-module-id'] = response.body['vf-modules']['vf-module'][0]['vf-module-id']

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


def _has_request(onap_ip, aai_data, exclude):
    print('Making HAS request for excluded {}'.format(str(exclude)))
    api = _init_python_has_api(onap_ip)
    request_id = str(uuid.uuid4())
    template = json.loads(open('templates/hasRequest.json').read())
    result = {}
    template['name'] = request_id
    node = template['template']['parameters']
    node['chosen_customer_id'] = aai_data['service-info']['global-customer-id']
    node['service_id'] = aai_data['service-info']['service-instance-id']
    node = template['template']['demands']['vFW-SINK'][0]
    node['attributes']['model-invariant-id'] = aai_data['vfw-model-info']['model-invariant-id']
    node['attributes']['model-version-id'] = aai_data['vfw-model-info']['model-version-id']
    if exclude:
        node['excluded_candidates'][0]['candidate_id'] = aai_data['vf-module-id']
        del node['required_candidates']
    else:
        node['required_candidates'][0]['candidate_id'] = aai_data['vf-module-id']
        del node['excluded_candidates']
    node = template['template']['demands']['vPGN'][0]
    node['attributes']['model-invariant-id'] = aai_data['vpgn-model-info']['model-invariant-id']
    node['attributes']['model-version-id'] = aai_data['vpgn-model-info']['model-version-id']

    with _no_ssl_verification():
        response = api.has.plans(body=template, params={}, headers={})
        if response.body.get('error_message') is not None:
            raise Exception(response.body['error_message']['explanation'])
        else:
            plan_id = response.body['id']
            response = api.has.plan(plan_id, body=None, params={}, headers={})
            status = response.body['plans'][0]['status']
            while status != 'done' and status != 'error':
                print(status)
                response = api.has.plan(plan_id, body=None, params={}, headers={})
                status = response.body['plans'][0]['status']
            if status == 'done':
                result = response.body['plans'][0]['recommendations'][0]
            else:
                raise Exception(response.body['plans'][0]['message'])
    return result


def _extract_has_appc_identifiers(has_result, demand):
    if demand == 'vPGN':
        v_server = has_result[demand]['attributes']['vservers'][0]
    else:
        if len(has_result[demand]['attributes']['vservers'][0]['l-interfaces']) == 4:
            v_server = has_result[demand]['attributes']['vservers'][0]
        else:
            v_server = has_result[demand]['attributes']['vservers'][1]
    for itf in v_server['l-interfaces']:
        if itf['ipv4-addresses'][0].startswith("10.0."):
            ip = itf['ipv4-addresses'][0]
            break

    config = {
        'vnf-id': has_result[demand]['attributes']['nf-id'],
        'vf-module-id': has_result[demand]['attributes']['vf-module-id'],
        'ip': ip,
        'vserver-id': v_server['vserver-id']
    }
    print("{} ansible_ssh_host={} ansible_ssh_user=ubuntu".format(config['vserver-id'], config['ip']))
    return config


def _extract_has_appc_dt_config(has_result, demand):
    if demand == 'vPGN':
        return {}
    else:
        config = {
            "nf-type": has_result[demand]['attributes']['nf-type'],
            "nf-name": has_result[demand]['attributes']['nf-name'],
            "vf-module-name": has_result[demand]['attributes']['vf-module-name'],
            "vnf-type": has_result[demand]['attributes']['vnf-type'],
            "service_instance_id": "319e60ef-08b1-47aa-ae92-51b97f05e1bc",
            "cloudClli": has_result[demand]['attributes']['physical-location-id'],
            "nf-id": has_result[demand]['attributes']['nf-id'],
            "vf-module-id": has_result[demand]['attributes']['vf-module-id'],
            "aic_version": has_result[demand]['attributes']['aic_version'],
            "ipv4-oam-address": has_result[demand]['attributes']['ipv4-oam-address'],
            "vnfHostName": has_result[demand]['candidate']['host_id'],
            "ipv6-oam-address": has_result[demand]['attributes']['ipv6-oam-address'],
            "cloudOwner": has_result[demand]['candidate']['cloud_owner'],
            "isRehome": has_result[demand]['candidate']['is_rehome'],
            "locationId": has_result[demand]['candidate']['location_id'],
            "locationType": has_result[demand]['candidate']['location_type'],
            'vservers': has_result[demand]['attributes']['vservers']
        }
        return config


def _build_config_from_has(has_result):
    v_pgn_result = _extract_has_appc_identifiers(has_result, 'vPGN')
    v_fw_result = _extract_has_appc_identifiers(has_result, 'vFW-SINK')
    dt_config = _extract_has_appc_dt_config(has_result, 'vFW-SINK')

    config = {
        'vPGN': v_pgn_result,
        'vFW-SINK': v_fw_result
    }
    #print(json.dumps(config, indent=4))
    config['dt-config'] = {
        'destinations': [dt_config]
    }
    return config


def _build_appc_lcm_dt_payload(is_vpkg, oof_config, book_name, if_test, traffic_presence):
    is_check = traffic_presence is not None
    oof_config = copy.deepcopy(oof_config)
    if is_vpkg:
        node_list = "[ {} ]".format(oof_config['vPGN']['vserver-id'])
    else:
        node_list = "[ {} ]".format(oof_config['vFW-SINK']['vserver-id'])

    if is_check:
        oof_config['dt-config']['traffic-presence'] = traffic_presence

    if if_test:
        file_content = {"test": "test"}
    else:
        file_content = oof_config['dt-config']
    config = {
        "configuration-parameters": {
            "node_list": node_list,
            "file_parameter_content":  json.dumps(file_content)
        }
    }
    if book_name != '':
        config["configuration-parameters"]["book_name"] =  book_name
    payload = json.dumps(config)
    return payload


def _build_appc_lcm_status_body(req):
    payload = {
        'request-id': req['input']['common-header']['request-id'],
        'sub-request-id': req['input']['common-header']['sub-request-id'],
        'originator-id': req['input']['common-header']['originator-id']
    }
    payload = json.dumps(payload)
    template = json.loads(open('templates/appcRestconfLcm.json').read())
    template['input']['action'] = 'ActionStatus'
    template['input']['payload'] = payload
    template['input']['common-header']['request-id'] = req['input']['common-header']['request-id']
    template['input']['common-header']['sub-request-id'] = str(uuid.uuid4())
    template['input']['action-identifiers']['vnf-id'] = req['input']['action-identifiers']['vnf-id']
    return template

def _build_appc_lcm_request_body(is_vpkg, config, req_id, action, book_name, if_test, traffic_presence=None):
    if is_vpkg:
        vnf_id = config['vPGN']['vnf-id']
    else:
        vnf_id = config['vFW-SINK']['vnf-id']
    if if_test:
        book_name = 'test@0.00.yml'
    payload = _build_appc_lcm_dt_payload(is_vpkg, config, book_name, if_test, traffic_presence)
    template = json.loads(open('templates/appcRestconfLcm.json').read())
    template['input']['action'] = action
    template['input']['payload'] = payload
    template['input']['common-header']['request-id'] = req_id
    template['input']['common-header']['sub-request-id'] = str(uuid.uuid4())
    template['input']['action-identifiers']['vnf-id'] = vnf_id
    return template


def _set_appc_lcm_timestamp(body, timestamp=None):
    if timestamp is None:
        t = datetime.utcnow() + timedelta(seconds=-10)
        timestamp = t.strftime('%Y-%m-%dT%H:%M:%S.244Z')
    body['input']['common-header']['timestamp'] = timestamp


def build_appc_lcms_requests_body(onap_ip, aai_data, simulate_oof, if_close_loop_vfw, if_test):
    if simulate_oof:
        migrate_from = json.loads(open('templates/sample-has-required.json').read())
        migrate_to = json.loads(open('templates/sample-has-excluded.json').read())
    else:
        migrate_from = _has_request(onap_ip, aai_data, False)
        f = open('templates/sample-has-required.json', 'w')
        f.write(json.dumps(migrate_from, indent=4))
        f.close()
        if not if_close_loop_vfw:
            migrate_to = _has_request(onap_ip, aai_data, True)
            f = open('templates/sample-has-excluded.json', 'w')
            f.write(json.dumps(migrate_to, indent=4))
            f.close()
        else:
            migrate_to = migrate_from
    migrate_from = _build_config_from_has(migrate_from)
    migrate_to = _build_config_from_has(migrate_to)
    req_id = str(uuid.uuid4())
    payload_dt_check_vpkg = _build_appc_lcm_request_body(True, migrate_to, req_id, 'DistributeTrafficCheck',
                                                         "vpkg-distributetrafficcheck@0.00.yml", if_test, False)
    payload_dt_vpkg_to = _build_appc_lcm_request_body(True, migrate_to, req_id, 'DistributeTraffic',
                                                      "distributetraffic@0.00.yml", if_test)
    payload_dt_check_vfw_from = _build_appc_lcm_request_body(False, migrate_from, req_id, 'DistributeTrafficCheck',
                                                             "vfw-distributetrafficcheck@0.00.yml", if_test, False)
    payload_dt_check_vfw_to = _build_appc_lcm_request_body(False, migrate_to, req_id, 'DistributeTrafficCheck',
                                                           "vfw-distributetrafficcheck@0.00.yml", if_test, True)
    result = list()
    result.append(payload_dt_check_vpkg)
    #result.append(payload_dt_vpkg_to)
    #result.append(payload_dt_check_vfw_from)
    #result.append(payload_dt_check_vfw_to)
    return result


def appc_lcm_request(onap_ip, req, if_test):
    api = _init_python_appc_lcm_api(onap_ip)
    print(json.dumps(req, indent=4))
    if req['input']['action'] == "DistributeTraffic":
        result = api.lcm.distribute_traffic(body=req, params={}, headers={})
    elif req['input']['action'] == "DistributeTrafficCheck":
        result = api.lcm.distribute_traffic_check(body=req, params={}, headers={})
    else:
        raise Exception("{} action not supported".format(req['input']['action']))

    if result.body['output']['status']['code'] == 400:
        print("Request Completed")
    elif result.body['output']['status']['code'] == 100:
        print("Request Accepted. Receiving result status...")
#    elif result.body['output']['status']['code'] == 311:
#        timestamp = result.body['output']['common-header']['timestamp']
#        _set_appc_lcm_timestamp(req, timestamp)
#        appc_lcm_request(onap_ip, req)
#        return
    else:
        raise Exception("{} - {}".format(result.body['output']['status']['code'],
                                         result.body['output']['status']['message']))
    #print(result)
    return result.body['output']['status']['code']


def appc_lcm_status_request(onap_ip, req):
    api = _init_python_appc_lcm_api(onap_ip)
    status_body = _build_appc_lcm_status_body(req)
    _set_appc_lcm_timestamp(status_body)

    result = api.lcm.action_status(body=status_body, params={}, headers={})

    if result.body['output']['status']['code'] == 400:
        status = json.loads(result.body['output']['payload'])
        return status
    else:
        raise Exception("{} - {}".format(result.body['output']['status']['code'],
                                         result.body['output']['status']['message']))


def confirm_appc_lcm_action(onap_ip, req):
    print("Checking LCM {} Status".format(req['input']['action']))

    while True:
        time.sleep(2)
        status = appc_lcm_status_request(onap_ip, req)
        print(status['status'])
        if status['status'] == 'SUCCESSFUL':
            return
        elif status['status'] == 'IN_PROGRESS':
            continue
        else:
            raise Exception("LCM {} {} - {}".format(req['input']['action'], status['status'], status['status-reason']))


def execute_workflow(vfw_vnf_id, onap_ip, simulate_oof, if_close_loop_vfw, if_test):
    print("Executing workflow for VNF ID '{}' on ONAP with IP {}".format(vfw_vnf_id, onap_ip))
    print("Simulate OOF {}, is CL vFW {}, is test {}".format(simulate_oof, if_close_loop_vfw, if_test))
    aai_data = load_aai_data(vfw_vnf_id, onap_ip)
    print(json.dumps(aai_data, indent=4))
    lcm_requests = build_appc_lcms_requests_body(onap_ip, aai_data, simulate_oof, if_close_loop_vfw, if_test)

    for i in range(len(lcm_requests)):
        print("APPC REQ {}".format(i))
        req = lcm_requests[i]
        _set_appc_lcm_timestamp(req)
        result = appc_lcm_request(onap_ip, req, if_test)
        if result == 100:
            confirm_appc_lcm_action(onap_ip, req)


#vnf_id, K8s node IP, simualate OOF, if close loop, if_test
execute_workflow(sys.argv[1], sys.argv[2], sys.argv[3].lower() == 'true', sys.argv[4].lower() == 'true', sys.argv[5].lower() == 'true')
