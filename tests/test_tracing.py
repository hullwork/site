"""Contract tests for W3C trace context, against the shared three-service specification.

🔴 Every other guard in this repository fails closed. This one fails **open**, on purpose,
and that inversion is what most of these cases assert: a malformed trace header must not be
able to fail a request. Diagnostic plumbing that can cause the outage it exists to explain
is worse than no plumbing.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib import error as urlerror
from urllib import request as urlrequest

from sites import telemetry
from sites import tracing
from sites.api import Handler
from sites.validation import DEFAULT_MERCHANT_ID

from tests.test_sites import _FakeTenantStore, _merchant_row


@contextlib.contextmanager
def capture_logs():
    """Collect what telemetry really writes, as JSON, including debug lines."""
    stream = io.StringIO()
    with mock.patch.object(telemetry, "_LOG_FORMAT", "json"), mock.patch.object(
        telemetry, "_MIN_LEVEL", "debug"
    ), mock.patch("sys.stderr", stream):
        yield stream


ADMIN_TOKEN = "s" * 32
VALID_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
VALID_SPAN_ID = "00f067aa0ba902b7"
VALID_TRACEPARENT = f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01"


class ParsingTests(unittest.TestCase):
    """Shape rules. The specification is shared verbatim by three services."""

    def test_a_well_formed_header_yields_its_trace_id(self) -> None:
        self.assertEqual(tracing.parse_traceparent(VALID_TRACEPARENT), VALID_TRACE_ID)
        # Surrounding whitespace is a proxy's doing, not a caller's mistake.
        self.assertEqual(
            tracing.parse_traceparent(f"  {VALID_TRACEPARENT}  "), VALID_TRACE_ID
        )

    def test_every_malformed_shape_is_treated_as_absent(self) -> None:
        cases = {
            "empty": "",
            "wrong version": f"01-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01",
            "version not hex": f"zz-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01",
            "three segments": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}",
            "five segments": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01-extra",
            "trace id all zero": f"00-{'0' * 32}-{VALID_SPAN_ID}-01",
            "span id all zero": f"00-{VALID_TRACE_ID}-{'0' * 16}-01",
            "trace id short": f"00-{VALID_TRACE_ID[:31]}-{VALID_SPAN_ID}-01",
            "trace id long": f"00-{VALID_TRACE_ID}a-{VALID_SPAN_ID}-01",
            "trace id upper": f"00-{VALID_TRACE_ID.upper()}-{VALID_SPAN_ID}-01",
            "trace id not hex": f"00-{'g' * 32}-{VALID_SPAN_ID}-01",
            "span id short": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID[:15]}-01",
            "flags not hex": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-zz",
            "flags too long": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-011",
            "not a header at all": "hello world",
        }
        for name, value in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(tracing.parse_traceparent(value))

    def test_generated_ids_have_the_right_shape_and_are_never_zero(self) -> None:
        # All-zero is what broken instrumentation emits when it has nothing to say. Two
        # requests carrying it would look like one trace, which is worse than none.
        for _ in range(64):
            trace_id = tracing.new_trace_id()
            span_id = tracing.new_span_id()
            self.assertRegex(trace_id, r"^[0-9a-f]{32}$")
            self.assertRegex(span_id, r"^[0-9a-f]{16}$")
            self.assertNotEqual(trace_id, "0" * 32)
            self.assertNotEqual(span_id, "0" * 16)
        self.assertNotEqual(tracing.new_trace_id(), tracing.new_trace_id())


class PriorityTests(unittest.TestCase):
    class _Headers(dict):
        def get(self, key, default=""):
            return super().get(key, default)

    def test_traceparent_wins_over_request_id(self) -> None:
        headers = self._Headers(
            {"traceparent": VALID_TRACEPARENT, "X-Request-Id": "abc"}
        )
        self.assertEqual(tracing.trace_id_for(headers), VALID_TRACE_ID)

    def test_request_id_is_used_when_no_traceparent_is_usable(self) -> None:
        headers = self._Headers({"X-Request-Id": "order-42"})
        self.assertEqual(
            tracing.trace_id_for(headers), tracing.trace_id_from_request_id("order-42")
        )

    def test_a_malformed_traceparent_falls_through_to_request_id(self) -> None:
        # The point of the priority order: a broken header must not shadow a usable one.
        headers = self._Headers(
            {"traceparent": "00-not-a-trace", "X-Request-Id": "order-42"}
        )
        self.assertEqual(
            tracing.trace_id_for(headers), tracing.trace_id_from_request_id("order-42")
        )

    def test_the_request_id_derivation_is_deterministic_and_shared(self) -> None:
        """Same request id, same trace id, in every service on this contract.

        A caller that only knows X-Request-Id still gets one trace across all of them
        without having to learn what traceparent is, and that only holds if the
        derivation is spelled the same way everywhere: sha256 of the UTF-8 bytes,
        first 16 bytes, hex.
        """
        derived = tracing.trace_id_from_request_id("order-42")
        self.assertEqual(derived, tracing.trace_id_from_request_id("order-42"))
        self.assertEqual(
            derived, hashlib.sha256(b"order-42").digest()[:16].hex()
        )
        self.assertRegex(derived, r"^[0-9a-f]{32}$")
        self.assertNotEqual(derived, tracing.trace_id_from_request_id("order-43"))

    def test_nothing_at_all_still_produces_a_usable_id(self) -> None:
        first = tracing.trace_id_for(self._Headers({}))
        second = tracing.trace_id_for(self._Headers({}))
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first, "0" * 32)
        self.assertNotEqual(first, second)


class SamplingFlagTests(unittest.TestCase):
    """🔴 An inbound sampling decision is inherited, never overridden.

    Rewriting an upstream's ``00`` to ``01`` reverses a decision that was already made,
    at a hop the upstream cannot observe. The only flags this service chooses are the
    ones for a trace it started itself.
    """

    def test_inbound_flags_are_carried_through(self) -> None:
        for flags in ("00", "01", "ff"):
            with self.subTest(flags=flags):
                headers = PriorityTests._Headers(
                    {"traceparent": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-{flags}"}
                )
                self.assertEqual(
                    tracing.inbound_context(headers), (VALID_TRACE_ID, flags)
                )

    def test_a_locally_started_trace_samples_itself(self) -> None:
        for headers in (
            PriorityTests._Headers({}),
            PriorityTests._Headers({"X-Request-Id": "order-42"}),
            PriorityTests._Headers({"traceparent": "garbage"}),
        ):
            self.assertEqual(tracing.inbound_context(headers)[1], "01")

    def test_an_unsampled_caller_stays_unsampled_downstream(self) -> None:
        trace_id, flags = tracing.inbound_context(
            PriorityTests._Headers(
                {"traceparent": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-00"}
            )
        )
        token = tracing.bind(trace_id, flags)
        try:
            self.assertTrue(
                tracing.outbound_headers()["traceparent"].endswith("-00")
            )
        finally:
            tracing.release(token)

    def test_the_header_name_is_matched_case_insensitively(self) -> None:
        """Header names are case-insensitive on the wire; a sender may use any casing.

        The real handlers read through ``email.message.Message``, which already folds
        case. This pins the property rather than the mechanism, so replacing that reader
        with a plain dict would fail here instead of silently ignoring ``Traceparent``.
        """
        from http.client import HTTPMessage

        message = HTTPMessage()
        message["TraceParent"] = VALID_TRACEPARENT
        self.assertEqual(tracing.inbound_context(message)[0], VALID_TRACE_ID)


class ForwardedHeaderTests(unittest.TestCase):
    """The activator rewrites the context it forwards, whatever casing arrived."""

    def test_the_callers_context_is_replaced_not_joined(self) -> None:
        from sites.activator import retrace_headers

        for spelling in ("traceparent", "Traceparent", "TRACEPARENT", "TraceParent"):
            with self.subTest(spelling=spelling):
                headers = {spelling: VALID_TRACEPARENT, "Accept": "*/*"}
                retrace_headers(headers, VALID_TRACE_ID, "01")
                present = [k for k in headers if k.lower() == "traceparent"]
                # Exactly one context leaves this hop. Two would let the upstream pick.
                self.assertEqual(len(present), 1, headers)
                forwarded = headers[present[0]]
                self.assertEqual(tracing.parse_traceparent(forwarded), VALID_TRACE_ID)
                self.assertNotEqual(forwarded, VALID_TRACEPARENT)
                self.assertEqual(headers["Accept"], "*/*")

    def test_the_sampling_decision_survives_the_hop(self) -> None:
        from sites.activator import retrace_headers

        headers = {"traceparent": f"00-{VALID_TRACE_ID}-{VALID_SPAN_ID}-00"}
        retrace_headers(headers, VALID_TRACE_ID, "00")
        self.assertTrue(headers["traceparent"].endswith("-00"))


class OutboundTests(unittest.TestCase):
    def test_the_trace_is_kept_and_the_span_is_new(self) -> None:
        first = tracing.outbound_traceparent(VALID_TRACE_ID)
        second = tracing.outbound_traceparent(VALID_TRACE_ID)
        self.assertEqual(tracing.parse_traceparent(first), VALID_TRACE_ID)
        self.assertEqual(tracing.parse_traceparent(second), VALID_TRACE_ID)
        # Each hop is its own span; reusing the caller's would attribute this hop's time
        # to the caller.
        self.assertNotEqual(first.split("-")[2], second.split("-")[2])

    def test_an_outbound_call_inherits_the_bound_context(self) -> None:
        token = tracing.bind(VALID_TRACE_ID)
        try:
            self.assertEqual(
                tracing.parse_traceparent(
                    tracing.outbound_headers()["traceparent"]
                ),
                VALID_TRACE_ID,
            )
        finally:
            tracing.release(token)

    def test_an_outbound_call_outside_any_context_still_emits_a_valid_header(self) -> None:
        self.assertIsNotNone(
            tracing.parse_traceparent(tracing.outbound_headers()["traceparent"])
        )

    def test_an_outbound_hop_is_parented_to_the_active_span(self) -> None:
        with tracing.span("parent", trace_id=VALID_TRACE_ID) as parent:
            self.assertEqual(
                tracing.outbound_headers()["traceparent"].split("-")[2],
                parent.span_id,
            )


class OtlpExporterTests(unittest.TestCase):
    class _CaptureExporter:
        def __init__(self):
            self.spans = []

        def submit(self, span):
            self.spans.append(span)

    def test_span_exports_otlp_identifiers_and_parent(self) -> None:
        capture = self._CaptureExporter()
        with mock.patch.object(tracing, "_exporter", capture):
            with tracing.span(
                "sites.api.request",
                kind=2,
                trace_id=VALID_TRACE_ID,
                parent_span_id=VALID_SPAN_ID,
                flags="01",
            ) as server:
                server.set_attribute("http.route", "/v1/deployments")
        self.assertEqual(len(capture.spans), 1)
        emitted = capture.spans[0]
        self.assertEqual(emitted.trace_id, VALID_TRACE_ID)
        self.assertEqual(emitted.parent_span_id, VALID_SPAN_ID)
        self.assertEqual(emitted.kind, 2)
        self.assertEqual(emitted.attributes["http.route"], "/v1/deployments")

    def test_unsampled_span_is_not_exported_but_still_propagates(self) -> None:
        capture = self._CaptureExporter()
        with mock.patch.object(tracing, "_exporter", capture):
            with tracing.span("unsampled", trace_id=VALID_TRACE_ID, flags="00"):
                self.assertTrue(tracing.outbound_headers()["traceparent"].endswith("-00"))
        self.assertEqual(capture.spans, [])

    def test_otlp_json_has_resource_scope_and_status(self) -> None:
        exporter = object.__new__(tracing._Exporter)
        exporter.service = "sites-api"
        span = tracing._SpanData(
            VALID_TRACE_ID, VALID_SPAN_ID, "", "request", 2, 1, 2,
            {"http.response.status_code": 200}, "",
        )
        payload = json.loads(exporter._payload([span]))
        resource = payload["resourceSpans"][0]
        self.assertEqual(
            resource["resource"]["attributes"][0]["value"]["stringValue"],
            "sites-api",
        )
        encoded = resource["scopeSpans"][0]["spans"][0]
        self.assertEqual(encoded["traceId"], VALID_TRACE_ID)
        self.assertEqual(encoded["status"]["code"], 1)

    def test_full_queue_drops_without_blocking_and_is_observable(self) -> None:
        outcomes = []
        exporter = object.__new__(tracing._Exporter)
        exporter.queue = __import__("queue").Queue(maxsize=1)
        exporter.observe = lambda outcome, amount: outcomes.append((outcome, amount))
        sample = tracing._SpanData(
            VALID_TRACE_ID, VALID_SPAN_ID, "", "request", 2, 1, 2, {}, ""
        )
        exporter.submit(sample)
        exporter.submit(sample)
        self.assertEqual(outcomes, [("queued", 1), ("dropped_queue_full", 1)])

    def test_collector_failure_is_counted_and_not_raised(self) -> None:
        outcomes = []
        exporter = object.__new__(tracing._Exporter)
        exporter.endpoint = "http://collector.invalid/v1/traces"
        exporter.service = "sites-api"
        exporter.timeout_seconds = 0.01
        exporter.observe = lambda outcome, amount: outcomes.append((outcome, amount))
        sample = tracing._SpanData(
            VALID_TRACE_ID, VALID_SPAN_ID, "", "request", 2, 1, 2, {}, ""
        )
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            exporter._export([sample])
        self.assertEqual(outcomes, [("dropped_export_failure", 1)])

    def test_close_drains_a_queued_span_before_worker_exit(self) -> None:
        exported = []
        exporter = tracing._Exporter(
            "http://collector.invalid",
            "sites-api",
            queue_size=4,
            batch_size=4,
            flush_seconds=10,
            timeout_seconds=0.01,
            observe=None,
        )
        exporter._export = lambda spans: exported.extend(spans)
        sample = tracing._SpanData(
            VALID_TRACE_ID, VALID_SPAN_ID, "", "request", 2, 1, 2, {}, ""
        )
        exporter.submit(sample)
        exporter.close(timeout=1)
        self.assertEqual(exported, [sample])
        self.assertFalse(exporter._thread.is_alive())


class _FakeKube:
    def get(self, path: str) -> dict:
        return {"items": []}


class ApiRequestTests(unittest.TestCase):
    """Real HTTP against the real handler: the header, the log field, and the response."""

    @classmethod
    def setUpClass(cls) -> None:
        Handler.kube = _FakeKube()
        Handler.store = _FakeTenantStore({}, merchants=[_merchant_row()])
        Handler.service_token = ADMIN_TOKEN
        Handler.session_key = "k" * 32
        Handler.local_login_enabled = True
        Handler.oidc_config = None
        Handler.mutation_lock = threading.Lock()
        Handler.synchronizer = None
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, headers: dict | None = None, path: str = "/v1/capabilities"):
        request = urlrequest.Request(
            f"{self.url}{path}",
            headers={"X-Sites-Service-Token": ADMIN_TOKEN, **(headers or {})},
        )
        try:
            with urlrequest.urlopen(request, timeout=10) as response:
                return int(response.status), dict(response.headers), response.read()
        except urlerror.HTTPError as exc:
            return int(exc.code), dict(exc.headers), exc.read()

    def emitted_records(self, headers: dict | None = None, **kwargs):
        """Run a request and return (the records telemetry actually wrote, response headers).

        🔴 The records are captured from the log stream, not by mocking ``telemetry.log``.
        Mocking it would replace the very function that injects ``trace_id``, so the test
        would be asserting against its own stub - green whether or not the field is ever
        emitted.
        """
        with capture_logs() as stream:
            _, response_headers, _ = self.call(headers, **kwargs)
        records = [
            json.loads(line)
            for line in stream.getvalue().splitlines()
            if line.startswith("{")
        ]
        return records, response_headers

    def test_an_inbound_traceparent_becomes_the_trace_id(self) -> None:
        status, headers, _ = self.call({"traceparent": VALID_TRACEPARENT})
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Request-Id"], VALID_TRACE_ID)

    def _await_server_span(self, exporter, deadline_s: float = 5.0):
        """The span this request emitted, waiting for the server to emit it.

        ``span.__exit__`` submits, and it runs on the server thread *after* the
        response has been written, so the client returning is not evidence that
        the span exists yet. Checking immediately made this a race with two
        faces: sometimes the exporter held nothing, sometimes it held another
        request's span, and neither message pointed at the timing.

        Selecting by the trace id this request sent is not circular. The
        property is that exactly one server span carries the inbound id; broken
        propagation emits none, and the deadline below is what reports it.
        """
        end = time.monotonic() + deadline_s
        while True:
            emitted = [call.args[0] for call in exporter.submit.call_args_list]
            server_spans = [s for s in emitted if s.name == "sites.api.request"]
            mine = [s for s in server_spans if s.trace_id == VALID_TRACE_ID]
            if len(mine) == 1 or time.monotonic() > end:
                self.assertEqual(
                    1, len(mine),
                    f"waited {deadline_s}s for exactly one sites.api.request "
                    f"span carrying the inbound trace id; got {len(mine)} of "
                    f"{len(server_spans)} server spans "
                    f"(trace ids: {[s.trace_id for s in server_spans]})",
                )
                return mine[0]

    def test_request_emits_a_server_span_parented_to_the_inbound_span(self) -> None:
        exporter = mock.Mock()
        with mock.patch.object(tracing, "_exporter", exporter):
            status, _, _ = self.call({"traceparent": VALID_TRACEPARENT})
            server = self._await_server_span(exporter)
        self.assertEqual(status, 200)
        self.assertEqual(server.kind, 2)
        self.assertEqual(server.parent_span_id, VALID_SPAN_ID)

    def test_a_malformed_traceparent_does_not_fail_the_request(self) -> None:
        """🔴 The inversion. A diagnostic header must never cost a request.

        Whoever set it is usually a proxy the caller cannot even see, so refusing would
        fail requests that the caller has no way to fix.
        """
        for broken in (
            "00-not-a-trace",
            f"99-{VALID_TRACE_ID}-{VALID_SPAN_ID}-01",
            f"00-{'0' * 32}-{VALID_SPAN_ID}-01",
            "garbage",
            "-" * 60,
        ):
            with self.subTest(traceparent=broken):
                status, headers, _ = self.call({"traceparent": broken})
                self.assertEqual(status, 200)
                # Locally generated, valid, and not the broken input.
                self.assertRegex(headers["X-Request-Id"], r"^[0-9a-f]{32}$")
                self.assertNotIn(headers["X-Request-Id"], broken)

    def test_only_a_request_id_is_derived_deterministically(self) -> None:
        expected = tracing.trace_id_from_request_id("order-42")
        first = self.call({"X-Request-Id": "order-42"})[1]["X-Request-Id"]
        second = self.call({"X-Request-Id": "order-42"})[1]["X-Request-Id"]
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)

    def test_a_request_with_no_context_gets_a_fresh_valid_id(self) -> None:
        first = self.call()[1]["X-Request-Id"]
        second = self.call()[1]["X-Request-Id"]
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first, "0" * 32)
        self.assertNotEqual(first, second)

    def test_the_response_header_is_the_id_in_the_logs(self) -> None:
        """The property that makes the header worth sending at all.

        A caller holding an X-Request-Id can find this request in our logs; if the two
        were allowed to differ, the header would be decoration.
        """
        records, response_headers = self.emitted_records(
            {"traceparent": VALID_TRACEPARENT}
        )
        # Self-check: a run that logged nothing would satisfy every assertion below.
        self.assertTrue(records, "no log records were captured")
        traced = [record for record in records if "trace_id" in record]
        self.assertTrue(traced, records)
        self.assertEqual({record["trace_id"] for record in traced}, {VALID_TRACE_ID})
        self.assertEqual(response_headers["X-Request-Id"], VALID_TRACE_ID)

    def test_a_refused_request_is_traced_too(self) -> None:
        # Failures are what anyone actually goes looking for.
        status, headers, _ = self.call(
            {"X-Sites-Service-Token": "wrong", "traceparent": VALID_TRACEPARENT}
        )
        self.assertEqual(status, 401)
        self.assertEqual(headers["X-Request-Id"], VALID_TRACE_ID)

    def test_an_unrouted_path_is_traced_too(self) -> None:
        status, headers, _ = self.call(path="/v1/nothing-here")
        self.assertEqual(status, 404)
        self.assertRegex(headers["X-Request-Id"], r"^[0-9a-f]{32}$")

    def test_capabilities_reports_the_impersonation_grant(self) -> None:
        status, _, body = self.call()
        self.assertEqual(status, 200)
        self.assertIs(json.loads(body)["mayActAsSubjects"], False)

        Handler.store = _FakeTenantStore(
            {}, merchants=[_merchant_row(may_act_as_subjects=True)]
        )
        try:
            status, _, body = self.call()
            self.assertIs(json.loads(body)["mayActAsSubjects"], True)
        finally:
            Handler.store = _FakeTenantStore({}, merchants=[_merchant_row()])


class LogFieldTests(unittest.TestCase):
    def _emit(self, event: str) -> dict:
        with capture_logs() as stream:
            telemetry.log(event)
        lines = [line for line in stream.getvalue().splitlines() if line.startswith("{")]
        self.assertEqual(len(lines), 1, stream.getvalue())
        return json.loads(lines[0])

    def test_a_bound_scope_puts_the_id_on_every_line(self) -> None:
        token = tracing.bind(VALID_TRACE_ID)
        try:
            self.assertEqual(self._emit("inside_scope")["trace_id"], VALID_TRACE_ID)
        finally:
            tracing.release(token)

    def test_the_field_is_omitted_outside_a_traced_scope(self) -> None:
        """Omitted, not empty.

        An empty string is a value: it matches log queries, groups in dashboards, and
        makes startup and background lines look like untraced requests. Absent says the
        only true thing - this line did not happen inside a request.
        """
        self.assertNotIn("trace_id", self._emit("outside_scope"))


if __name__ == "__main__":
    unittest.main()
