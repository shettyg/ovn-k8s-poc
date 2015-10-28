This document provides an example of using OVN with k8 in the underlay mode.

This requires that you have a OpenStack setup pre-installed with OVN
providing the underlay networking.  It is out of scope of this documentation
to describe how to create a OpenStack setup with OVN. Instead, please refer
http://docs.openstack.org/developer/networking-ovn/ for that.

Create two Ubuntu VMs on the same OVN logical switch.  The VMs need a minimum
of one network interface.  It is sometimes easier to plug an additional
network interface purely for mgmt connection.

Install docker python package

```
pip install docker-py
```

Prep the host with Open vSwitch
==============================

You will need to install openvswitch in the VMs. On Ubuntu, you can do it with:
```
apt-get install openvswitch-switch openvswitch-common -y
```

Create the openvswitch bridge via which you plan to send your container
traffic. If your mgmt interface is 'eth0' and if your interface connected to
the OVN logical switch is 'eth1', you will need to create a bridge 'breth1'
and add 'eth1' as a port of that bridge. i.e.

```
ovs-vsctl add-br breth1
ovs-vsctl add-port breth1 eth1
```

You will also need to remove the IP address from eth1 and move it to breth1

Set ovn bridge in the database

```
ovs-vsctl set open_vswitch . external_ids:ovn-bridge="breth1"
```

Set neutron source file.

ovs-vsctl set open_vswitch . external_ids:neutron-config="/root/openrc"

Where the contents of /root/openrc will look like

```
OS_AUTH_URL=http://10.33.74.255:5000/v2.0
OS_TENANT_ID=015058d74d1c444b871fc39dbd4442b8
OS_TENANT_NAME="demo"
OS_USERNAME="demo"
OS_PASSWORD="password"
OS_VIF_ID=2233c32a-4092-4d8b-b44d-1c54f666c507
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
cp ovn-k8-underlay.py /usr/libexec/kubernetes/kubelet-plugins/net/exec/ovn/ovn
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

Cluster Setup
=============

Create a logical router in OVN (via OpenStack). (This support is currently
not part of OpenStack OVN plugin. So you should do this manually in OVN after
the below steps)

On host1, create a logical switch and all its logical ports in advance. You
then cache the ip, mac and vlan of the logical port locally. You can do this
by:

```
./ovn-k8-underlay.py lswitch-setup host1 192.168.1.0/29 router_id
```

On host2, do the same, but with a different subnet

```
./ovn-k8-underlay.py lswitch-setup host2 192.168.2.0/29 router_id
```

Eventually the above script will automatically attach the created logical
switch to a logical router. But since OpenStack does not yet have the
required feature, you will have to use 'ovn-nbctl' to connect the two
logical switches together to a logical router.

The above will provide the required connectivity for all the pods to be
able to speak to all the other pods in the cluster.

Start the pods
=============

You can now use kubectl to start some pods. The pods should be able to talk
to each other via IP adderss.
