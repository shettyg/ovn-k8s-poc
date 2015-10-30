This document provides an example of using OVN with k8 in the overlay mode.

Prep the host with Docker
=========================

Install the latest stable version of Docker from www.docker.com.
Install docker python package.

```
easy_install -U pip
pip install docker-py
```

Prep the host with Open vSwitch
==============================

You will need to install openvswitch in the VMs. You need a minimum
Open vSwitch version of 2.5.

Start the OVN central components
===============================

OVN architecture has a central component which stores your networking intent
in a database. So on any machine, with an IP Address of $CENTRAL_IP, where you
have installed and started Open vSwitch, you will need to start some central
components.

Begin by making ovsdb-server listen on a TCP port by running:

```
ovs-appctl -t ovsdb-server ovsdb-server/add-remote ptcp:6640
```

Start ovn_northd daemon. This daemon translates networking intent from
Docker stored in OVN_Northbound database to logical flows in
OVN_Southbound database.

```
/usr/share/openvswitch/scripts/ovn-ctl start_northd
```

You will need to create a Logical Router for your cluster.

```
ROUTER_NAME="kubecluster"
lruuid=`ovn-nbctl create Logical_Router name=$ROUTER_NAME`
```

The $lruuid above will be used in the later commands. So this variable
has to be set in all your hosts.

One time setup on each host
===========================

On each host, where you plan to spawn your containers, you will need to run
the following commands once.

$LOCAL_IP in the below command is the IP address via which other hosts can
reach this host. This acts as your local tunnel endpoint.

$ENCAP_TYPE is the type of tunnel that you would like to use for overlay
networking. The options are "geneve" or "stt".

```
ovs-vsctl set Open_vSwitch . external_ids:ovn-remote="tcp:$CENTRAL_IP:6640" \
    external_ids:ovn-encap-ip=$LOCAL_IP external_ids:ovn-encap-type="geneve"
```

Start the ovn controller

```
/usr/share/openvswitch/scripts/ovn-ctl start_controller
```

On each host, you need to choose a unique subnet and pass it to docker daemon
via the --bip value. For e.g. if you choose the subnet 192.168.1.0/24, you
should start the Docker daemon with --bip=192.168.1.1/24. This value
(192.168.1.1/24) will be referenced in future commands as $SUBNET

Once you choose the subnet, create the corresponding Logical switch in OVN
by running:

```
SWITCH_NAME=`cat /etc/openvswitch/system-id.conf`
ovn-nbctl --db=tcp:$CENTRAL_IP:6640 lswitch-add $SWITCH_NAME
ovs-vsctl set open_vswitch . external_ids:lswitch=$SWITCH_NAME
```

Now create a logical router port and attach the above switch to it.
First choose a random, but unique mac address.

for e.g.:
LRP_MAC="00:00:00:00:ff:01"

For the below commands, you need $CENTRAL_IP, $SUBNET and $lruuid set
correctly in advance.

```
lrp_uuid=`ovn-nbctl  --db=tcp:$CENTRAL_IP:6640  -- --id=@lrp create \
Logical_Router_port name=$SWITCH_NAME network=$SUBNET mac=\"$LRP_MAC\" \
 -- add Logical_Router $lruuid ports @lrp \
 -- lport-add $SWITCH_NAME "$SWITCH_NAME"-rp`

ovn-nbctl --db=tcp:$CENTRAL_IP:6640 set Logical_port "$SWITCH_NAME"-rp \
type=router options:router-port=$lrp_uuid addresses=\"$LRP_MAC\"
```

Prep the host with Kubernetes components
========================================

On one of your VMs, you will need to start etcd and k8's central
components.

Download a stable version of etcd from https://github.com/coreos/etcd/releases.
Start etcd. For e.g:

```
nohup ./etcd --addr=127.0.0.1:4001 --bind-addr=0.0.0.0:4001 --data-dir=/var/etcd/data 2>&1 > /dev/null &
```

Download the latest stable kubernetes.tar.gz from:
https://github.com/kubernetes/kubernetes/releases

untar the file and look for kubernetes-server-linux-amd64.tar.gz. Untar that
file too. Copy kube-apiserver, kube-controller-manager, kube-scheduler,
kubelet and kubectl to a directory and start them. For e.g:

```
nohup ./kube-apiserver --portal-net=192.168.200.0/24 --address=0.0.0.0 --etcd-servers=http://127.0.0.1:4001 --cluster-name=kubernetes --v=2 2>&1 > /dev/null &

nohup ./kube-controller-manager --master=127.0.0.1:8080 --v=2 1>&2 > /dev/null &

nohup ./kube-scheduler --master=127.0.0.1:8080 --v=2 2>&1 > /dev/null &
```

Before you start the kubelet, you will need to copy the OVN network plugin to
a specific location:

```
mkdir -p /usr/libexec/kubernetes/kubelet-plugins/net/exec/ovn
cp ovn-k8-overlay.py /usr/libexec/kubernetes/kubelet-plugins/net/exec/ovn/ovn
```

Now start the kubelet

```
nohup ./kubelet --api-servers=http://0.0.0.0:8080 --v=2 --address=0.0.0.0 --enable-server --hostname-override=$(hostname -i)  --network-plugin=ovn 2>&1 > /dev/null &
```

On your second VM, you will need to copy the OVN plugin too and then start
only the kubelet. You can do this by:

```
MASTER_IP=$IP_OF_MASTER
nohup ./kubelet --api-servers=http://${MASTER_IP}:8080 --v=2 --address=0.0.0.0 --enable-server --hostname-override=$(hostname -i) --network-plugin=ovn 2>&1 > /dev/null &
```

Start the pods
=============

You can now use kubectl to start some pods. The pods should be able to talk
to each other via IP address.
