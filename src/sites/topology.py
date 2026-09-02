"""Standalone local deployment topology.

Runtime constants remain in :mod:`sites.exposure` and
:mod:`sites.k8s_resources`; this module is the deployment-side expression used
by local cluster scripts and contract tests. Keeping it in this repository
prevents a consumer repository from becoming a hidden build prerequisite.
"""

from sites.k8s_resources import cluster_pod_cidr, cluster_service_cidr

# 🔴 Re-exported, never restated.
#
# This module used to hold its own `POD_CIDR = "10.201.0.0/16"` and
# `SERVICE_CIDR = "10.221.0.0/16"`. That made the contract test which claimed to
# pin these against the provisioner a comparison of this repository against
# itself -- and its comment said it was reading a `kubeadm_profile.py` that
# lives in another repository and is on no test's path. Two copies, one
# tautological check, and a comment describing a check that did not exist.
#
# They are function references rather than string constants on purpose: the Pod
# CIDR has no value until it is configured, so there is nothing for a module
# attribute to hold at import time, and anything that reads it has to go through
# the same refusal every other caller does.
__all__ = ["cluster_pod_cidr", "cluster_service_cidr", "PORT_FORWARDS"]

PORT_FORWARDS = tuple(
    {"guest": port, "host": 18090 + (port - 30080)}
    for port in range(30080, 30089)
)
