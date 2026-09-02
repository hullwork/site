"""The Pod CIDR: configured, not guessed, and checked against reality at startup.

🔴 The value used to be the literal ``10.201.0.0/16`` in five places -- two
Python modules and three NetworkPolicy rules in the chart.  Every rule that uses
it is written as ``{"cidr": "0.0.0.0/0", "except": [pod CIDR]}``, so when the
value does not match the cluster the exception selects nothing and the rule
degrades to "allow everyone".  Nothing reports that: the policy still applies
and ``kubectl get netpol`` still looks right.  The Infra reference clusters run
10.205/10.208/10.250 and stock kubeadm, Calico, Flannel, GKE and EKS each use
something else, so the literal was wrong nearly everywhere it could be
installed.

The failure direction is *open*, so the fix is not only "make it configurable".
An unconfigured value refuses, and the operator checks the declaration against
its own address before it opens a port -- turning an unobservable fail-open into
a CrashLoop somebody can see.

Three outcomes, deliberately three:

* configured and consistent -> start;
* configured and contradicted -> refuse, naming both values;
* **not checkable** -> refuse, and say so *differently*.  "Cannot tell" is not
  "fine"; a guard that answers a question it could not evaluate is worse than no
  guard, because it is now evidence.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

from sites import topology
from sites.k8s_resources import (
    POD_CIDR_ENV,
    POD_IP_ENV,
    ClusterNetworkError,
    cluster_pod_cidr,
    verify_pod_network,
    workload_egress_except_cidrs,
)


ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def environment(**values: str | None):
    """Set or unset variables for one block, restoring whatever was there."""
    previous = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class NoDefaultTests(unittest.TestCase):
    def test_an_unset_pod_cidr_refuses_instead_of_falling_back(self) -> None:
        # Red if a default is reintroduced. A default would not be a
        # convenience: it would preserve today's defect as the behaviour of
        # every unconfigured install.
        with environment(**{POD_CIDR_ENV: None}):
            with self.assertRaises(ClusterNetworkError) as caught:
                cluster_pod_cidr()
        self.assertIn(POD_CIDR_ENV, str(caught.exception))
        # The message has to be actionable, not just fatal.
        self.assertIn("cluster-cidr", str(caught.exception))

    def test_an_empty_pod_cidr_is_the_same_as_unset(self) -> None:
        # `--set-string clusterNetwork.podCIDR=` and a missing key must not
        # behave differently; an empty string is the shape Helm produces.
        with environment(**{POD_CIDR_ENV: "   "}):
            with self.assertRaises(ClusterNetworkError):
                cluster_pod_cidr()

    def test_a_malformed_pod_cidr_refuses(self) -> None:
        for bad in ("10.201.0.0", "not-a-cidr", "10.201.0.5/16", "10.201.0.0/64"):
            with self.subTest(bad=bad):
                with environment(**{POD_CIDR_ENV: bad}):
                    with self.assertRaises(ClusterNetworkError):
                        cluster_pod_cidr()

    def test_a_configured_pod_cidr_is_returned_normalized(self) -> None:
        # The other direction: a guard that raised unconditionally would pass
        # every case above and make the product uninstallable.
        with environment(**{POD_CIDR_ENV: "10.42.0.0/16"}):
            self.assertEqual("10.42.0.0/16", cluster_pod_cidr())
            self.assertIn("10.42.0.0/16", workload_egress_except_cidrs())

    def test_the_egress_exclusions_follow_the_configured_value(self) -> None:
        with environment(**{POD_CIDR_ENV: "10.99.0.0/16"}):
            excluded = workload_egress_except_cidrs()
        self.assertIn("10.99.0.0/16", excluded)
        self.assertNotIn("10.201.0.0/16", excluded)
        # The rest of the list is not conditional on it.
        for fixed in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
            self.assertIn(fixed, excluded)

    def test_topology_re_exports_rather_than_restating(self) -> None:
        self.assertIs(topology.cluster_pod_cidr, cluster_pod_cidr)


class StartupVerificationTests(unittest.TestCase):
    """The three outcomes, each asserted on its own distinguishing evidence."""

    def test_an_address_inside_the_declared_network_starts(self) -> None:
        with environment(**{POD_CIDR_ENV: "10.42.0.0/16", POD_IP_ENV: "10.42.7.9"}):
            self.assertEqual("10.42.0.0/16", verify_pod_network())

    def test_an_address_outside_the_declared_network_refuses(self) -> None:
        with environment(**{POD_CIDR_ENV: "10.42.0.0/16", POD_IP_ENV: "10.205.3.4"}):
            with self.assertRaises(ClusterNetworkError) as caught:
                verify_pod_network()
        message = str(caught.exception)
        # 🔴 Both values, because either one could be the wrong one and the
        # reader cannot tell which without seeing both. Asserting only the
        # exception type would pass on any raise from anywhere in the function --
        # including ip_network() choking on its own input.
        self.assertIn("10.205.3.4", message)
        self.assertIn("10.42.0.0/16", message)

    def test_an_unknown_address_refuses_and_says_it_could_not_check(self) -> None:
        # 🔴 The third state. "Cannot check" must not resolve to "checked", and
        # must not be reported as "contradicted" either -- the operator action
        # is different (restore the downward API entry, versus fix a CIDR).
        with environment(**{POD_CIDR_ENV: "10.42.0.0/16", POD_IP_ENV: None}):
            with self.assertRaises(ClusterNetworkError) as caught:
                verify_pod_network()
        message = str(caught.exception)
        self.assertIn("cannot verify", message)
        self.assertIn(POD_IP_ENV, message)
        # Distinguishable from the mismatch message above, not merely fatal.
        self.assertNotIn("is outside", message)

    def test_a_malformed_address_is_also_unverifiable_not_a_mismatch(self) -> None:
        with environment(**{POD_CIDR_ENV: "10.42.0.0/16", POD_IP_ENV: "10.42.7"}):
            with self.assertRaises(ClusterNetworkError) as caught:
                verify_pod_network()
        message = str(caught.exception)
        self.assertIn("cannot verify", message)
        self.assertNotIn("is outside", message)

    def test_the_three_messages_are_mutually_distinguishable(self) -> None:
        # Pins the property the three cases above each half-assert: an operator
        # reading one line of log must be able to tell which of the three
        # happened. Red if two of them are ever collapsed into one wording.
        messages = {}
        for label, env in (
            ("unconfigured", {POD_CIDR_ENV: None, POD_IP_ENV: "10.42.7.9"}),
            ("unverifiable", {POD_CIDR_ENV: "10.42.0.0/16", POD_IP_ENV: None}),
            ("contradicted", {POD_CIDR_ENV: "10.42.0.0/16", POD_IP_ENV: "10.205.3.4"}),
        ):
            with environment(**env):
                with self.assertRaises(ClusterNetworkError) as caught:
                    verify_pod_network()
            messages[label] = str(caught.exception)
        self.assertEqual(3, len(set(messages.values())), messages)
        self.assertIn("is not set", messages["unconfigured"])
        self.assertIn("cannot verify", messages["unverifiable"])
        self.assertIn("is outside", messages["contradicted"])


class OperatorEntrypointTests(unittest.TestCase):
    """The guard has to actually be on the startup path, before any port opens."""

    def _run_operator(self, **env: str | None) -> subprocess.CompletedProcess[str]:
        child = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        for name, value in env.items():
            if value is None:
                child.pop(name, None)
            else:
                child[name] = value
        return subprocess.run(
            [sys.executable, "-m", "sites.operator"],
            capture_output=True, text=True, timeout=60, cwd=ROOT, env=child,
        )

    def test_the_operator_refuses_to_start_on_a_contradicted_pod_cidr(self) -> None:
        # End to end over the real entrypoint, not just the function: this is
        # the only thing that proves the check is wired in *and* that it runs
        # before the metrics listener binds and before the first apiserver call
        # -- neither of which is available here, so reaching them would show up
        # as a different error.
        result = self._run_operator(
            SITES_CLUSTER_POD_CIDR="10.42.0.0/16", SITES_POD_IP="10.205.3.4",
        )
        self.assertEqual(1, result.returncode, result.stdout[-2000:] + result.stderr[-2000:])
        combined = result.stdout + result.stderr
        self.assertIn("cluster_network_refused", combined)
        self.assertIn("10.205.3.4", combined)

    def test_the_operator_refuses_when_it_cannot_learn_its_own_address(self) -> None:
        result = self._run_operator(
            SITES_CLUSTER_POD_CIDR="10.42.0.0/16", SITES_POD_IP=None,
        )
        self.assertEqual(1, result.returncode, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertIn("cannot verify", result.stdout + result.stderr)

    def test_the_operator_refuses_when_the_pod_cidr_is_unset(self) -> None:
        result = self._run_operator(
            SITES_CLUSTER_POD_CIDR=None, SITES_POD_IP="10.42.7.9",
        )
        self.assertEqual(1, result.returncode, result.stdout[-2000:] + result.stderr[-2000:])
        self.assertIn(POD_CIDR_ENV, result.stdout + result.stderr)

    def test_a_consistent_declaration_gets_past_the_guard(self) -> None:
        # 🔴 The direction that stops this class from passing on a guard that
        # rejects everything. There is no cluster here, so the operator cannot
        # get far -- but it must get *past* the network check, which is proved
        # by the acceptance log line and by the absence of the refusal one.
        result = self._run_operator(
            SITES_CLUSTER_POD_CIDR="10.42.0.0/16", SITES_POD_IP="10.42.7.9",
        )
        combined = result.stdout + result.stderr
        self.assertIn("cluster_network_verified", combined)
        self.assertNotIn("cluster_network_refused", combined)


if __name__ == "__main__":
    unittest.main()
