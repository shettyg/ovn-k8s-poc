Integration of OVN with Kubernetes.
----------------------------------

OVN's integration with k8 is a work in progress.

OVN provides network virtualization to containers.  OVN's integration with
Kubernetes works in two modes - the "underlay" mode or the "overlay" mode.

In the "underlay" mode, OVN requires a OpenStack setup to provide container
networking. In this mode, one can create logical networks and can have
k8 pods running inside VMs, independent VMs and physical machines running some
stateful services connected to the same logical network. (For this mode to
work completely, we need distributed load-balancer suppport in OVN, which
is yet to be implemented, but is in the roadmap.)

In the "overlay" mode, OVN can create virtual networks amongst k8 pods
running on multiple hosts.  In this mode, you do not need a pre-created
OpenStack setup. (This mode needs NAT support. Open vSwitch as of version
2.4 does not support NAT. It is likely that Open vSwitch 2.5 or 2.6 will
support NAT)

For both the modes to work, a user has to install Open vSwitch in each VM/host
that he plans to run his containers.

The "underlay" mode
-------------------

This mode requires that you have a OpenStack setup pre-installed with OVN
providing the underlay networking.  It is out of scope of this documentation
to describe how to create a OpenStack setup with OVN. Instead, please refer
http://docs.openstack.org/developer/networking-ovn/ for that.

Cluster Setup
=============

Once you have the OpenStack setup, you can have a tenant create a bunch
of VMs (with one mgmt network interface or more) that form the k8 cluster.

From the master node, we make a call to OpenStack Neutron to create a
logical router.  The returned UUID is henceforth referred as '$ROUTER_ID'.

With OVN, each k8 worker node is a logical switch. So they get a /x address.
From each worker node, we create a logical switch in OVN via Neutron. We then
make that logical switch as a port of the logical router $ROUTER_ID.

We then create 2^(32-x) logical ports for that logical switch (with the parent
port being the VIF_ID of the hosting VM).  On the worker node, for each
logical port we write data into the local Open vSwitch database to
act as a cache of ip address, its associated mac address and port uuid.

The value 'x' chosen depends on the number of CPUs and memory available
in the VM.

K8 kubelet in each worker node is started with a OVN network plugin to setup
the pod network namespace.

Since one of the k8 requirements is that each pod in a cluster is able to
talk to every other pod in the cluster via IP address, the above architecture
with interconnected logical switches via a logical router acts as the
foundation. In addition, this lets other VMs and physical machines (outside
the k8 cluster) reach the k8 pods via the same IP address.  With a OVN l3
gateway, one could also access each pod from the external world using direct
ip addresses or via using floating IPs.

Pod Creation
============

When a k8 pod is created, it lands on one of the worker nodes. The OVN
plugin is called to setup the network namespace. The plugin looks at the
local cache of IP addresses.  It picks an unused IP address and sets up the
pod with that IP address and MAC address and then marks that IP address as
'used' in the cache.

The above design prevents one from making calls to Neutron for every single
pod creation.

Security
========

The network admin creates a few security profiles in OVN via Neutron.
For each pod spec, if he wants to associate a firewall profile, he adds the
UUIDs from Neutron in the pod spec either as labels or as an annotation.

When the pod is created, the network plugin will make a call to the k8 API
server to fetch the security profile UUIDs for the pod. The network plugin
chooses a unused logical port and makes a call to Neutron to associate the
security profile with the logical port. It also updates the local cache to
indicate the associated security profile with the logical port.

An optimization is possible here. If the amount of distinct security profiles
is limited in a k8 cluster, one can pre-associate the security profiles with
the logical ports. This would mean that one would create a lot more logical
ports per worker node (inspite of limited CPU and Memory).


Loadbalancers
=============

We need to maintain a cache of all the logical port uuids and their ip
addresses in the k8 master node.  This cache can be built during cluster
bringup by providing the $ROUTER_ID as an input to a script.

On k8 master node, we will need to write a new daemon that is equivalent to
k8's kube-proxy. Unlike default k8 setup, where each node has a kube-proxy,
we will need our daemon run only on the master node.

When a k8 service is created, the new daemon will create a load-balancer object
in OVN (via Neutron). Whenever new endpoints get created (via pods) for that
service, the daemon will look at the IP addresses of the endpoints, figure
out the logical port uuid associated with that IP address and then make
a call to OVN (via Neutron) to update the logical port uuids associated with
the load balancer object.

North-South traffic
===================

For external connectivity to k8 services, we will need l3 gateway in OVN.
For every service ip address, the L3 gateway should choose one of the pod
endpoints as the destination.


Overlay mode
------------

TBA.

