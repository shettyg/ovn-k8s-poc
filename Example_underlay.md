This document provides an example of using OVN with k8s in the underlay mode.

This requires that you have a OpenStack setup pre-installed with OVN
providing the underlay networking.  It is out of scope of this documentation
to describe how to create a OpenStack setup with OVN. Instead, please refer
http://docs.openstack.org/developer/networking-ovn/ for that.

Create a logical switch "ls-kube-mgmt" in OVN. Create another logical switch
called "ls-master".

Create two Ubuntu VMs (These VMs will be called 'worker1' and 'worker2' in
the rest of the document) on the "ls-kube-mgmt" OVN logical switch.  It is
sometimes easier to plug an additional network interface purely for
ssh connection.

Create an additional Ubuntu VM to run your k8s master components. This VM
will be called 'master' in the rest of the document. This VM needs 2 network
interfaces. One of the network interfaces should be on "ls-kube-mgmt" and
another on "ls-master".

Install Docker on 'worker1' and 'worker2' by following the instructions in
www.docker.com.

Install docker python package on 'worker1' and 'worker2'.

```
pip install docker-py
```

Prep 'worker1' and 'worker2' with Open vSwitch and Linux bridge
==============================================================

You will need to install openvswitch in the VMs. On Ubuntu, you can do it with:
```
apt-get install openvswitch-switch openvswitch-common -y
```

Create a linux bridge via which you plan to send your container
traffic. If your "ls-kube-mgmt" interface is 'eth1', you will need to create
a bridge 'breth1' and add 'eth1' as a port of that bridge. i.e.

```
brctl addbr breth1
brctl setageing breth1 0  # XXX: Do we need this?
brctl addif breth1 eth1
```

You will also need to remove the IP address from eth1 and move it to breth1
along with any route information.

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

OS_VIF_ID above is the neutron port id for your interface 'eth1' (connected
to OVN logical switch "ls-kube-mgmt".)

Create the Open vSwitch integration bridge "br-int" and attach it to the
Linux bridge via a veth interface.

```
ovs-vsctl add-br br-int

ip link add br-int-veth type veth peer name breth1-veth
ifconfig br-int-veth up
ifconfig breth1-veth up

ovs-vsctl add-port br-int br-int-veth

brctl addif breth1 breth1-veth
```

Due to a terrible hack in k8 wherein they enable hairpin mode
in every linux bridge on the host (even the ones they control),
we need to run a script that disables it. This hack can be removed
once k8s moves to a plugin.

```
while true; do
brctl hairpin breth1 breth1-veth off
brctl hairpin breth1 eth1 off
done
```

Prep 'master' with Kubernetes components
========================================

On your 'master' VM, you will need to start etcd and k8s' central
components.

Download a stable version of etcd from https://github.com/coreos/etcd/releases.
Start etcd. For e.g:

```
nohup ./etcd --addr=127.0.0.1:4001 --bind-addr=0.0.0.0:4001 --data-dir=/var/etcd/data 2>&1 > /dev/null &
```

Download the latest stable kubernetes.tar.gz from:
https://github.com/kubernetes/kubernetes/releases

Untar the file and look for kubernetes-server-linux-amd64.tar.gz. Untar that
file too. Copy kube-apiserver, kube-controller-manager, kube-scheduler,
kubelet and kubectl to a directory and start them. For e.g:

```
nohup ./kube-apiserver --portal-net=172.16.2.0/24 --address=0.0.0.0 --etcd-servers=http://127.0.0.1:4001 --cluster-name=kubernetes --v=2 2>&1 > /dev/null &

nohup ./kube-controller-manager --master=127.0.0.1:8080 --v=2 1>&2 > /dev/null &

nohup ./kube-scheduler --master=127.0.0.1:8080 --v=2 2>&1 > /dev/null &
```

The '--portal-net' option in the above command tells the subnet from which K8s
allocate VIP for your services. So choose a subnet which does not conflict with
real IP addresses.

Prep 'worker1' and 'worker2' with Kubernetes components
=======================================================

Before you start the kubelet, you will need to copy the OVN network plugin to
a specific location:

```
mkdir -p /usr/libexec/kubernetes/kubelet-plugins/net/exec/ovn
cp ovn-k8-underlay.py /usr/libexec/kubernetes/kubelet-plugins/net/exec/ovn/ovn
```

Now start the kubelet and kube-proxy

```
MASTER_IP=$IP_OF_MASTER
LOCAL_IP=$IP_OF_LOCAL_MACHINE_REACHABLE_FROM_MASTER
nohup ./kubelet --api-servers=http://$IP_OF_MASTER:8080 --v=2 --address=0.0.0.0 --enable-server --hostname-override=$LOCAL_IP  --network-plugin=ovn --cluster-dns=172.16.1.2 --cluster-domain=cluster.local 2>&1 > /dev/null &

nohup ./kube-proxy --master=$IP_OF_MASTER:8080 --v=2 2>&1 > /dev/null &
```

Cluster Setup for networking
===========================

Create a logical router, 'k8-router'  in OVN (via OpenStack).
(This support is currently not part of OpenStack OVN plugin. So you should do
this manually in OVN after the below steps)

On worker1, create a logical switch and all its logical ports in advance. You
then cache the ip, mac and vlan of the logical port locally. You can do this
by:

```
./ovn-k8-underlay.py lswitch-setup ls-worker1 192.168.1.0/29 k8-router_id
route add -net 192.168.1.0 netmask 255.255.255.248 dev breth1
```

On worker2, do the same, but with a different subnet

```
./ovn-k8-underlay.py lswitch-setup ls-worker2 192.168.2.0/29 k8-router_id
route add -net 192.168.2.0 netmask 255.255.255.248 dev breth1
```

Eventually the above script will automatically attach the created logical
switch to a logical router. But since OpenStack does not yet have the
required feature, you will have to use 'ovn-nbctl' to connect the two
logical switches together to a logical router.

The above will provide the required connectivity for all the pods to be
able to speak to all the other pods in the cluster.

There is a k8s requirement for your pods to be able to communicate with
all the daemons in the 'master' VM. So now connect your 'ls-master' OVN
logical switch as a port of your k8-router.

Load Balancing
==============

K8 kube-proxy provides iptables based stateful and distributed load-balancing.
To integrate this with OVN, a few commands need to be run on each host.

On each host, for the logical switch of that host with subnet $SUBNET, you will
need get the mac address of the default gateway. For e.g., with OpenStack, if
the $SUBNET is 192.168.1.0/29, the ip address of default gateway is usually
192.168.1.1.  Let us say the mac address of the default gateway in the logical
space is is $GW_MAC and its ip address is $GW_IP

Also you will need to know the larger subnet of all the pods in your k8
cluster. For e.g., if host1 $SUBNET is 192.168.1.0/24 and host2 $SUBNET is
192.168.2.0/24 and so on, your subnet for the entire cluster can be
192.168.0.0/16. Let us say that larger subnet is $CLUSTER_SUBNET

On each host, run the following commands:

```
sysctl -w  net.bridge.bridge-nf-filter-vlan-tagged=1
ip neigh add $GW_IP lladdr $GW_MAC dev breth1 nud perm
route add -net $GW_IP  netmask 255.255.255.255 dev breth1
route add -net $SUBNET_IP netmask $SUBNET_NETMASK dev breth1
route add -net $CLUSTER_SUBNET_IP netmask $CLUSTER_SUBNET_NETMASK gw $GW_IP dev breth1
```

Start the pods
==============

You can now use kubectl to start some pods. The pods should be able to talk
to each other via IP adderss.

Start the services
==================

You should be able to access the service ip addresses from inside the pods.
