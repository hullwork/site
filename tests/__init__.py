"""Contract suite for the site control plane.

The Pod CIDR has no default in the product: it is a fact about the cluster an
install targets, and a wrong value silently turns every "allow 0.0.0.0/0 except
the Pod CIDR" NetworkPolicy into "allow everyone" (see
sites/k8s_resources.cluster_pod_cidr).  The suite therefore has to declare one,
exactly as an operator does.

It is set here rather than defaulted in the product so there is one visible
place where the suite states its assumption, and so the "unconfigured refuses"
cases in tests/test_cluster_network.py stay meaningful -- they clear it for the
duration of a single case rather than relying on it never having been set.
"""
import os

os.environ.setdefault("SITES_CLUSTER_POD_CIDR", "10.201.0.0/16")
