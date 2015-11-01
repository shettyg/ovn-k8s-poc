#!/usr/bin/python
import argparse
import ast
import atexit
import getpass
import json
import os
import re
import requests
import shlex
import subprocess
import sys
import time
import uuid

import ovs.util
import ovs.daemon

from neutronclient.v2_0 import client
from docker import Client

AUTH_STRATEGY = ""
AUTH_URL = ""
OVN_BRIDGE = "br-int"
PASSWORD = ""
TENANT_ID = ""
USERNAME = ""
VIF_ID = ""


def call_popen(cmd):
    child = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    output = child.communicate()
    if child.returncode:
        raise RuntimeError("Fatal error executing %s" % (cmd))
    if len(output) == 0 or output[0] == None:
        output = ""
    else:
        output = output[0].strip()
    return output


def call_prog(prog, args_list):
    cmd = [prog, "--timeout=5", "-vconsole:off"] + args_list
    return call_popen(cmd)


def ovs_vsctl(args):
    return call_prog("ovs-vsctl", shlex.split(args))


def neutron_setup():
    global USERNAME, PASSWORD, TENANT_ID, AUTH_URL, AUTH_STRATEGY, VIF_ID

    CONFIG_FILE = ovs_vsctl("--if-exists get open_vswitch . "
                            "external-ids:neutron-config").strip('"')
    if not CONFIG_FILE:
        sys.exit("Neutron config file not specified")

    key_value = {}
    with open(CONFIG_FILE) as fd:
        for line in fd:
            output = line.rstrip('\n').split('=', 1)
            key_value[output[0]] = output[1]

    VIF_ID = key_value.get('OS_VIF_ID', '').strip('"')
    if not VIF_ID:
        sys.exit("OS_VIF_ID not set")
    USERNAME = key_value.get('OS_USERNAME', '').strip('"')
    if not USERNAME:
        sys.exit("OS_USERNAME not set")
    TENANT_ID = key_value.get('OS_TENANT_ID', '').strip('"')
    if not TENANT_ID:
        sys.exit("OS_TENANT_ID not set")
    AUTH_URL = key_value.get('OS_AUTH_URL', '').strip('"')
    if not AUTH_URL:
        sys.exit("OS_AUTH_URL not set")
    AUTH_STRATEGY = "keystone"

    PASSWORD = key_value.get('OS_PASSWORD', '').strip('"')
    if not PASSWORD:
        sys.exit("OS_PASSWORD not set")


def neutron_login():
    neutron_setup()
    try:
        neutron = client.Client(username=USERNAME,
                                password=PASSWORD,
                                tenant_id=TENANT_ID,
                                auth_url=AUTH_URL,
                                auth_strategy=AUTH_STRATEGY)
    except Exception as e:
        raise RuntimeError("Failed to login into Neutron(%s)" % str(e))
    return neutron


def cache_port_init():
    cache = ovs_vsctl("--if-exists  get open_vswitch . "
                      "external-ids:lport-cache")
    if cache:
        sys.exit("cache already built for this node")

    ovs_vsctl("set open_vswitch . external-ids:lport-cache={}")


def cache_port_destroy():
    ovs_vsctl("--if-exists remove open_vswitch . external-ids lport-cache")


def cache_set_port_details(port_id, ip, netmask, mac, vlan, gateway_ip):
    cache = ovs_vsctl("--if-exists  get open_vswitch . "
                      "external-ids:lport-cache").strip('"')
    if not cache:
        sys.exit("fata error. cache not initialized")

    cache_dict = ast.literal_eval(cache)
    cache_dict[port_id] = {"ip": ip, "netmask": str(netmask), "mac": mac,
                           "vlan": str(vlan), "gateway_ip": gateway_ip,
                           "used": "no"}
    ovs_vsctl("set open_vswitch . "
              "external-ids:lport-cache=\"%s\"" % str(cache_dict))


def cache_get_port_details(port_id):
    cache = ovs_vsctl("--if-exists get open_vswitch . "
                      "external-ids:lport-cache").strip('"')
    if not cache:
        sys.exit("fata error. cache not initialized")

    cache_dict = ast.literal_eval(cache)
    lport_id = cache_dict.get(port_id, "")
    if lport_id:
        return cache_dict[lport_id]
    else:
        return None


def cache_get_free_port():
    cache = ovs_vsctl("--if-exists get open_vswitch . "
                      "external-ids:lport-cache").strip('"')
    if not cache:
        sys.exit("fata error. cache not initialized")

    cache_dict = ast.literal_eval(cache)
    lport_details = None
    lport = None
    for port_id in cache_dict.keys():
        if cache_dict[port_id]['used'] == "no":
            lport = port_id
            lport_details = cache_dict[port_id]
            break

    return (lport, lport_details)


def cache_mark_port_usage(port_id, usage):
    cache = ovs_vsctl("--if-exists get open_vswitch . "
                      "external-ids:lport-cache").strip('"')
    if not cache:
        sys.exit("fatal error. cache not initialized")

    cache_dict = ast.literal_eval(cache)
    if cache_dict.get(port_id, ""):
        cache_dict[port_id]['used'] = usage

    ovs_vsctl("set open_vswitch . "
              "external-ids:lport-cache=\"%s\"" % str(cache_dict))


def lswitch_setup(args):
    network = args.network
    subnet = args.subnet
    router_id = args.router_id

    ovs_vsctl("set open_vswitch . external-ids:router_id=%s" % router_id)

    cache_port_init()

    netmask = subnet.rsplit('/', 1)[1]
    num_ports = 2 ** (32 - int(netmask)) - 2
    if num_ports > 4095:
        cache_port_destroy()
        sys.exit("Maximum number of ports that can be created is 4095")

    try:
        neutron = neutron_login()
    except Exception as e:
        cache_port_destroy()
        sys.exit("lswitch_setup: neutron login. (%s)" % (str(e)))

    try:
        body = {'network': {'name': network, 'admin_state_up': True}}
        ret = neutron.create_network(body)
        network_id = ret['network']['id']
    except Exception as e:
        cache_port_destroy()
        sys.exit("lswitch_setup: neutron net-create call. (%s)" % str(e))

    ovs_vsctl("set open_vswitch . external_ids:network_id=%s" % network_id)

    try:
        body = {'subnet': {'network_id': network_id,
                           'ip_version': 4,
                           'cidr': subnet}}
        ret = neutron.create_subnet(body)
        subnet_id = ret['subnet']['id']
        gateway_ip = ret['subnet']['gateway_ip']
    except Exception as e:
        cache_port_destroy()
        neutron.delete_network(network_id)
        sys.exit("lswitch_setup: neutron subnet-create call. (%s)" % str(e))

    ovs_vsctl("set open_vswitch . external_ids:subnet_id=%s" % subnet_id)

    '''
    try:
        body = {'subnet_id': subnet_id}
        ret = neutron.add_interface_router(router_id, body)
    except Exception as e:
        cache_port_destroy()
        neutron.delete_network(network_id)
        sys.exit("lswitch_setup: neutron router-iface-add call. (%s)" % str(e))
    '''

    vlan = 1
    for num in range(1, num_ports):
        try:
            name = "k8"
            body = {'port': {'network_id': network_id,
                             'binding:profile': {'parent_name': VIF_ID,
                                                 'tag': int(vlan)},
                             'name': name,
                             'admin_state_up': True}}
            ret = neutron.create_port(body)
            mac = ret['port']['mac_address']
            ip_address = ret['port']['fixed_ips'][0]['ip_address']
            port_id = ret['port']['id']
            cache_set_port_details(port_id, ip_address, netmask, mac, vlan,
                                   gateway_ip)
        except Exception as e:
            sys.stderr.write("Failed to create port. (%s)" % str(e))

        vlan = vlan + 1


def lswitch_destroy(args):
    router_id = ovs_vsctl("--if-exists get open_vswitch . "
                          "external-ids:router_id").strip('"')
    if not router_id:
        sys.exit("lswitch_destroy: no router-id found in Open vSwitch db")

    cache = ovs_vsctl("--if-exists  get open_vswitch . "
                      "external-ids:lport-cache").strip('"')
    if not cache:
        sys.exit("fata error. cache not initialized")
    cache_dict = ast.literal_eval(cache)

    try:
        neutron = neutron_login()
    except Exception as e:
        sys.exit("lswitch_destroy: neutron login. (%s)" % (str(e)))

    port_delete_error = False
    for port_id in cache_dict.keys():
        try:
            neutron.delete_port(port_id)
        except Exception as e:
            port_delete_error = True
            sys.stderr.write("lswitch_destroy: neutron port-delete. (%s)"
                             % (str(e)))
            continue
        del cache_dict[port_id]

    if not port_delete_error:
        cache_port_destroy()
    else:
        ovs_vsctl("set open_vswitch . "
                  "external-ids:lport-cache=\"%s\"" % str(cache_dict))

    if port_delete_error:
        sys.exit("failed deleting some lports. Look at "
                 "external-ids:lport-cache in Open_vSwitch table for "
                 "undeleted lports and delete them manually")

    router_id = ovs_vsctl("--if-exists get open_vswitch . "
                          "external_ids:router_id").strip('"')
    subnet_id = ovs_vsctl("--if-exists get open_vswitch . "
                          "external_ids:subnet_id").strip('"')

    '''
    if router_id and subnet_id:
        try:
            body = {'subnet_id': subnet_id}
            remove_interface_router(router_id, body)
        except Exception as e:
            sys.exit("failed to delete the lswitch from lrouter")
    else:
        sys.exit("failed to delete lswitch from lrouter as no "
                 "router_id and subnet_id found in open_vswitch table")
    '''

    ovs_vsctl("remove open_vswitch . external_ids router_id")
    ovs_vsctl("remove open_vswitch . external_ids subnet_id")

    network_id = ovs_vsctl("--if-exists get open_vswitch . "
                           "external_ids:network_id").strip('"')
    if network_id:
        try:
            neutron.delete_network(network_id)
        except Exception as e:
            sys.exit("failed to destroy the lswitch, delete it manually (%s)"
                     % (str(e)))

    ovs_vsctl("remove open_vswitch . external_ids network_id")


def plugin_init(args):
    pass


def get_annotations(pod_name, namespace):
    api_server = ovs_vsctl("--if-exists get open_vswitch . "
                           "external-ids:api_server").strip('"')
    if not api_server:
        return None

    url = "http://%s/api/v1/pods" % (api_server)
    response = requests.get("http://0.0.0.0:8080/api/v1/pods")
    if response:
        pods = response.json()['items']
    else:
        return None

    for pod in pods:
        if (pod['metadata']['namespace'] == namespace and
           pod['metadata']['name'] == pod_name):
            annotations = pod['metadata'].get('annotations', "")
            if annotations:
                return annotations
            else:
                return None


def associate_security_group(lport_id, security_group_id):
    try:
        neutron = neutron_login()
    except Exception as e:
        sys.exit("associate_security_group: neutron login. (%s)" % (str(e)))

    try:
        body = {'port': {'security_groups': [security_group_id]}}
        update_port(lport_id, body)
    except Exception as e:
        sys.exit("associate_security_group: failed port association")


def plugin_setup(args):
    ns = args.k8_args[0]
    pod_name = args.k8_args[1]
    container_id = args.k8_args[2]

    client = Client(base_url='unix://var/run/docker.sock')
    try:
        inspect = client.inspect_container(container_id)
        pid = inspect["State"]["Pid"]
    except Exception as e:
        error = "failed to get container pid"
        sys.exit(error)

    if not os.path.isdir("/var/run/netns"):
        os.makedirs("/var/run/netns")

    # Choose an unused logical port. Get its ipaddress and mac
    (lport, lport_details) = cache_get_free_port()
    if not lport:
        sys.exit("No free lports available")

    ip_address = lport_details['ip']
    netmask = lport_details['netmask']
    mac = lport_details['mac']
    vlan = lport_details['vlan']
    gateway_ip = lport_details['gateway_ip']

    netns_dst = "/var/run/netns/%s" % (pid)
    if not os.path.isfile(netns_dst):
        netns_src = "/proc/%s/ns/net" % (pid)
        command = "ln -s %s %s" % (netns_src, netns_dst)

        try:
            call_popen(shlex.split(command))
        except Exception as e:
            error = "failed to create the netns link"
            sys.exit(error)

    # Delete the existing veth pair
    command = "ip netns exec %s ip link del eth0" % (pid)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "failed to delete the default veth pair"
        sys.stderr.write(error)

    veth_outside = container_id[0:15]
    veth_inside = container_id[0:13] + "_c"
    command = "ip link add %s type veth peer name %s" \
              % (veth_outside, veth_inside)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to create veth pair (%s)" % (str(e))
        sys.exit(error)

    # Up the outer interface
    command = "ip link set %s up" % veth_outside
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to admin up veth_outside (%s)" % (str(e))
        sys.exit(error)

    # Move the inner veth inside the container
    command = "ip link set %s netns %s" % (veth_inside, pid)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to move veth inside (%s)" % (str(e))
        sys.exit(error)

    # Change the name of veth_inside to eth0
    command = "ip netns exec %s ip link set dev %s name eth0" \
              % (pid, veth_inside)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to change name to eth0 (%s)" % (str(e))
        sys.exit(error)

    # Up the inner interface
    command = "ip netns exec %s ip link set eth0 up" % (pid)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to admin up veth_inside (%s)" % (str(e))
        sys.exit(error)

    # Set the mtu to handle tunnels
    command = "ip netns exec %s ip link set dev eth0 mtu %s" \
              % (pid, 1450)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to set mtu (%s)" % (str(e))
        sys.exit(error)

    # Set the ip address
    command = "ip netns exec %s ip addr add %s/%s dev eth0" \
              % (pid, ip_address, netmask)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to set ip address (%s)" % (str(e))
        sys.exit(error)

    # Set the mac address
    command = "ip netns exec %s ip link set dev eth0 address %s" % (pid, mac)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to set mac address (%s)" % (str(e))
        sys.exit(error)

    # Set the gateway
    command = "ip netns exec %s ip route add default via %s" \
              % (pid, gateway_ip)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to set gateway (%s)" % (str(e))
        sys.exit(error)

    # Add the port to a OVS bridge and set the vlan
    try:
        ovs_vsctl("add-port %s %s tag=%s -- set interface %s "
                  "external_ids:lport_id=%s external_ids:ip_address=%s"
                  % (OVN_BRIDGE, veth_outside, vlan, veth_outside, lport,
                     ip_address))
    except Exception as e:
        error = "failed to create a OVS port. (%s)" % (str(e))
        sys.exit(error)

    cache_mark_port_usage(lport, "yes")

    annotations = get_annotations(ns, pod_name)
    if annotations:
        security_group = annotations.get("security-group", "")
        if security_group:
            associate_security_group(lport, security_group)


def plugin_status(args):
    ns = args.k8_args[0]
    pod_name = args.k8_args[1]
    container_id = args.k8_args[2]

    veth_outside = container_id[0:15]
    ip_address = ovs_vsctl("--if-exists get interface %s "
                           "external_ids:ip_address"
                           % (veth_outside)).strip('"')
    if ip_address:
        style = {"ip": ip_address}
        print json.dumps(style)


def disassociate_security_group(lport_id):
    try:
        neutron = neutron_login()
    except Exception as e:
        # XXX: This is not good enough. You need to make this lport
        # unusable, if it did have a security profile.
        sys.exit("disassociate_security_group: neutron login. (%s)" % (str(e)))

    try:
        body = {'port': {'security_groups': []}}
        update_port(lport_id, body)
    except Exception as e:
        # XXX: This is not good enough either.
        sys.exit("disassociate_security_group: failed port disassociation")


def plugin_teardown(args):
    ns = args.k8_args[0]
    pod_name = args.k8_args[1]
    container_id = args.k8_args[2]

    veth_outside = container_id[0:15]
    command = "ip link delete %s" % (veth_outside)
    try:
        call_popen(shlex.split(command))
    except Exception as e:
        error = "Failed to delete veth_outside (%s)" % (str(e))
        sys.stderr.write(error)

    lport = ovs_vsctl("--if-exists get interface %s external_ids:lport_id"
                      % (veth_outside)).strip('"')
    if not lport:
        return

    try:
        ovs_vsctl("del-port %s" % (veth_outside))
    except Exception as e:
        error = "failed to delete OVS port (%s)" % (veth_outside)
        sys.stderr.write(error)

    annotations = get_annotations(ns, pod_name)
    if annotations:
        security_group = annotations.get("security-group", "")
        if security_group:
            disassociate_security_group(lport)

    cache_mark_port_usage(lport, "no")


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(title='Subcommands',
                                       dest='command_name')

    # Parser for sub-command lswitch-setup
    parser_lswitch_setup = subparsers.add_parser(
                                'lswitch-setup', help="Create a lswitch, "
                                "create lports for that lswitch, attach "
                                "lswitch to a lrouter and build a local cache")
    parser_lswitch_setup.add_argument('network', metavar="SWITCHNAME",
                                      help="switch name")
    parser_lswitch_setup.add_argument('subnet', metavar="SUBNET",
                                      help="The subnet CIDR for this network")
    parser_lswitch_setup.add_argument('router_id', metavar="ROUTER",
                                      help="The router the switch atteches to")
    parser_lswitch_setup.set_defaults(func=lswitch_setup)

    # Parser for sub-command lswitch-destroy
    parser_lswitch_destroy = subparsers.add_parser(
                                'lswitch-destroy', help="Delete the lswitch"
                                "created for this node and all its lports")
    parser_lswitch_destroy.set_defaults(func=lswitch_destroy)

    # Parser for sub-command init
    parser_plugin_init = subparsers.add_parser('init', help="kubectl init")
    parser_plugin_init.set_defaults(func=plugin_init)

    # Parser for sub-command setup
    parser_plugin_setup = subparsers.add_parser('setup',
                                                help="setup pod networking")
    parser_plugin_setup.add_argument('k8_args', nargs=3,
                                     help='arguments passed by kubectl')
    parser_plugin_setup.set_defaults(func=plugin_setup)

    # Parser for sub-command status
    parser_plugin_status = subparsers.add_parser('status',
                                                 help="pod status")
    parser_plugin_status.add_argument('k8_args', nargs=3,
                                      help='arguments passed by kubectl')
    parser_plugin_status.set_defaults(func=plugin_status)

    # Parser for sub-command teardown
    parser_plugin_teardown = subparsers.add_parser('teardown',
                                                   help="pod teardown")
    parser_plugin_teardown.add_argument('k8_args', nargs=3,
                                        help='arguments passed by kubectl')
    parser_plugin_teardown.set_defaults(func=plugin_teardown)

    args = parser.parse_args()
    args.func(args)

if __name__ == '__main__':
    main()
