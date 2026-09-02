"""The published authentication contract must keep matching the code.

``docs/AUTH.md`` is what a third-party client author builds against, and a client
author has no way to notice that it drifted. Documentation that quietly stops being true is
worse than none: it is followed. These checks are deliberately coarse - they pin the facts a
client branches on (status codes, header names, endpoint paths, configuration names), not
the prose around them.
"""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path

from sites import console_session
from sites.client import MIN_ACTING_SUBJECT_SALT_BYTES, acting_subject
from sites.identity import ACTING_SUBJECT_HEADER
from sites.validation import DEFAULT_MERCHANT_KEY_TTL_SECONDS


ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "src" / "sites"
DOC = (ROOT / "docs" / "AUTH.md").read_text(encoding="utf-8")
SOURCES = "\n".join(
    (ROOT / "src" / "sites" / name).read_text(encoding="utf-8")
    for name in ("identity.py", "api_auth.py", "oidc.py", "api.py", "api_mcp.py")
)
# String literals in the source are split across lines and f-string placeholders, so compare
# against a form with quotes removed and whitespace collapsed.
FLAT_SOURCE = re.sub(r"\s+", " ", SOURCES.replace('"', "").replace("'", ""))


def _documented_refusals() -> list[tuple[str, str]]:
    """(status, error prose) for every row of the refusal table."""
    section = DOC.split("## 6. Refusals", 1)[1].split("## 7.", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        for fragment in cells[1].split(" / "):
            text = fragment.strip().strip("`").replace("`", "")
            if text:
                rows.append((cells[0], text))
    return rows


class RefusalTableTests(unittest.TestCase):
    def test_the_table_was_actually_parsed(self) -> None:
        # Without this, a table this test can no longer read would make every check below
        # vacuously true - the exact shape of green that means nothing.
        rows = _documented_refusals()
        self.assertGreaterEqual(len(rows), 12, rows)
        self.assertIn("401", {status for status, _ in rows})
        self.assertIn("429", {status for status, _ in rows})

    def test_every_documented_error_is_one_the_code_emits(self) -> None:
        for status, prose in _documented_refusals():
            with self.subTest(status=status, error=prose):
                # A row may document a machine-readable code instead of prose; then the
                # code itself is the thing that has to exist.
                if prose.startswith("code "):
                    self.assertIn(prose.removeprefix("code "), FLAT_SOURCE)
                    continue
                # A leading header name is an f-string placeholder in the source, and a
                # leading ellipsis is the table eliding one; match the literal remainder.
                remainder = re.sub(r"^(X-[A-Za-z-]+|…)\s+", "", prose)
                self.assertIn(re.sub(r"\s+", " ", remainder), FLAT_SOURCE)

    def test_the_refusals_a_client_must_be_able_to_distinguish_are_documented(self) -> None:
        # The reverse direction: deleting one of these rows has to fail. Each is a case
        # where a client's correct reaction differs from every other case's.
        #
        # Matched against the parsed table rather than against the document text: several of
        # these phrases also occur in the prose above, so searching the whole file would
        # stay green after the row a client actually reads was deleted.
        table = " | ".join(prose for _, prose in _documented_refusals())
        for prose in (
            "is not accepted; the merchant and tenant are determined by the credential",
            "this key is not authorized to act for a subject",
            "merchant is disabled",
            "tenant is disabled",
            "local login is disabled",
            "no merchant is mapped to this account",
            "this account has no tenant and signups are closed",
            "merchant_tenant_quota_exceeded",
            "invalid service token",
        ):
            with self.subTest(error=prose):
                self.assertIn(prose, table)


class ContractSurfaceTests(unittest.TestCase):
    def test_the_documented_headers_are_the_ones_in_use(self) -> None:
        for header in (
            ACTING_SUBJECT_HEADER,
            console_session.CSRF_HEADER,
            "X-Sites-Service-Token",
        ):
            with self.subTest(header=header):
                self.assertIn(header, DOC)
        self.assertIn(console_session.COOKIE, DOC)
        self.assertIn(console_session.CSRF_COOKIE, DOC)

    def test_the_documented_auth_endpoints_are_routed(self) -> None:
        paths = set(re.findall(r"`(/v1/auth/[a-z]+)`", DOC))
        self.assertEqual(
            paths,
            {
                "/v1/auth/methods",
                "/v1/auth/login",
                "/v1/auth/callback",
                "/v1/auth/local",
                "/v1/auth/logout",
            },
        )
        for path in paths:
            with self.subTest(path=path):
                self.assertIn(f'path == "{path}"', SOURCES)

    def test_the_documented_client_variables_exist(self) -> None:
        client = (ROOT / "src" / "sites" / "client.py").read_text(encoding="utf-8")
        for name in re.findall(r"`(SITES_[A-Z_]+)`", DOC):
            with self.subTest(variable=name):
                self.assertIn(name, client + SOURCES)

    def test_the_documented_salt_floor_matches_the_code(self) -> None:
        self.assertEqual(MIN_ACTING_SUBJECT_SALT_BYTES, 32)
        self.assertIn("at least 32 bytes", DOC)

    def test_the_documented_key_lifetime_matches_the_default(self) -> None:
        self.assertEqual(DEFAULT_MERCHANT_KEY_TTL_SECONDS // 86400, 90)
        self.assertIn("90 days", DOC)

    def test_no_reference_to_the_deleted_federation_module_survives(self) -> None:
        """The lesson stays, the symbol goes.

        ``control_sso`` was a module in a different service that no longer exists there
        either. A reader who has only this repository can search the whole tree and never
        find out what it was - so a comment that explains a rule by naming it explains
        nothing, which is the opposite of what those comments are for. The rules it taught
        (do not sign with a token you also hand out; do not let a caller mint the
        assertions you accept) are stated behaviourally instead, in the same places.
        """
        for path in sorted(SOURCE_DIR.glob("*.py")) + sorted(ROOT.glob("docs/*.md")):
            with self.subTest(path=path.name):
                self.assertNotIn("control_sso", path.read_text(encoding="utf-8"))

    def test_the_deliberate_divergence_is_recorded(self) -> None:
        """A sentence that exists to survive the next consistency review.

        Services on this contract are intentionally not uniform about how strictly they
        demand the salt: one whose only purpose is acting for others must refuse to start
        without it, while acting is optional here. Someone comparing implementations will
        find that difference and, with nothing written down, report it as a defect and
        "fix" it - by making this control plane refuse to start over a capability the
        deployment never asked for.
        """
        self.assertIn("difference in responsibility, not drift", DOC)
        self.assertIn("acting is an *optional* capability here", DOC)

    def test_the_rule_that_replaces_the_deleted_behaviour_is_stated(self) -> None:
        """🔴 The one line a client author must not miss.

        The previous API let a caller name its own merchant. Anyone porting a client, or
        writing one from an older example, will reach for that again; if the document does
        not say it is refused, they will rediscover it as a 403 and assume a bug.
        """
        self.assertIn("The merchant is decided by the credential", DOC)
        self.assertIn("refused with 403", DOC)


if __name__ == "__main__":
    unittest.main()


# The payload digest recorded the last time this copy was aligned with the shared
# artifact. 🔴 This is **not** a second copy of an expected value - the file already carries
# the digest of its own data, and that self-consistency is checked separately. This constant
# answers a different question, and the only one a file cannot answer about itself: *is this
# copy still the same data everyone else has?* A vendored file that was edited and had its
# digest recomputed is perfectly self-consistent and would pass every check that only looks
# inside it, while this service quietly runs vectors nobody else has.
CANONICAL_DIGEST = "1a5daa90834c2cc2e4d793dcfd946689f7434e157d9bae9bb5019f83caa2c24e"

# Fields that make up the shared payload. Everything else in the file - the comment, the
# notes, the per-vector ``_why`` - is prose each service rewrites in its own words.
_PAYLOAD_VECTOR_KEYS = ("salt", "tenant_id", "subject_id", "expected")
_PAYLOAD_TOP_KEYS = ("header", "pseudonym_pattern", "min_salt_bytes")


def _payload_digest(document: dict) -> str:
    """Digest of the data alone, canonicalised: sorted keys, no spaces, ASCII-escaped.

    🔴 The anchor is deliberately **not** a hash of the file's bytes. Services rewrite the
    prose in this file - that is allowed, and encouraged, since a note nobody can read in
    their own language is a note nobody reads. A byte hash would therefore report a
    difference every time anyone edited a comment, and a guard that cries wolf on unrelated
    changes gets an exception added to it or gets switched off - after which it no longer
    reports the real thing either. Hashing the payload fires only when the numbers move.
    """
    payload = {
        **{key: document[key] for key in _PAYLOAD_TOP_KEYS},
        "vectors": [
            {key: vector[key] for key in _PAYLOAD_VECTOR_KEYS}
            for vector in document["vectors"]
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SharedVectorTests(unittest.TestCase):
    """The cross-service consistency vectors (contract §1).

    Every service on this contract vendors this same file and runs the same numbers.
    What is shared is the **vectors**, not the code: the derivations are separate
    implementations by design (this service's MCP server is itself a caller and has to
    compute pseudonyms of its own), and only a fixed expected value can prove they have not
    drifted apart. A shared library would have made the dependency the very thing this
    split was meant to remove.
    """

    VECTORS = json.loads(
        (ROOT / "docs" / "acting-subject-vectors.json").read_text(encoding="utf-8")
    )

    def test_the_file_carries_vectors_and_they_were_loaded(self) -> None:
        # Self-check: an empty or restructured file must not make the loop below vacuous.
        self.assertGreaterEqual(len(self.VECTORS["vectors"]), 5)
        for vector in self.VECTORS["vectors"]:
            self.assertTrue(set(vector) >= {"salt", "tenant_id", "subject_id", "expected"})

    def test_every_shared_vector_reproduces(self) -> None:
        for vector in self.VECTORS["vectors"]:
            with self.subTest(
                tenant=vector["tenant_id"], subject=vector["subject_id"]
            ):
                self.assertEqual(
                    acting_subject(
                        vector["salt"], vector["tenant_id"], vector["subject_id"]
                    ),
                    vector["expected"],
                )

    def test_the_vectors_pin_the_two_mistakes_that_actually_happen(self) -> None:
        """Truncating hex instead of bytes, and joining without NUL.

        Both produce a plausible 32-hex string, so neither shows up as an error anywhere -
        only as two deployments that disagree about who a user is.
        """
        by_pair = {
            (vector["tenant_id"], vector["subject_id"]): vector["expected"]
            for vector in self.VECTORS["vectors"]
        }
        # Present in the file: the pair whose concatenation is identical without NUL.
        self.assertIn(("tenant-al", "ice"), by_pair)
        self.assertIn(("tenant-a", "lice"), by_pair)
        self.assertNotEqual(by_pair[("tenant-al", "ice")], by_pair[("tenant-a", "lice")])
        for expected in by_pair.values():
            self.assertRegex(expected, self.VECTORS["pseudonym_pattern"])
            # 32 characters is the digest's first 16 bytes; a hex-side truncation would
            # have produced 16.
            self.assertEqual(len(expected), 32)

    def test_the_payload_digest_matches_the_data_beside_it(self) -> None:
        """Recompute the anchor rather than trusting the number written next to the data.

        This is the case the anchor exists for and the one it would otherwise miss: someone
        edits a vector and does not update ``_payload_sha256``. Comparing the recorded
        digest with a freshly computed one catches that here, before this copy is compared
        with anyone else's and the disagreement is blamed on the other side.
        """
        self.assertEqual(
            self.VECTORS["_payload_sha256"], _payload_digest(self.VECTORS)
        )

    def test_the_anchor_is_the_payload_and_not_the_file(self) -> None:
        # Rewriting prose must not move the anchor - that property is the reason the anchor
        # is defined this way, so it is asserted rather than assumed.
        rewritten = {
            **self.VECTORS,
            "_comment": "prose rewritten locally",
            "_notes": ["a service put this in its own words"],
        }
        self.assertEqual(_payload_digest(rewritten), _payload_digest(self.VECTORS))
        # ... and moving a single data character must move it.
        moved = json.loads(json.dumps(self.VECTORS))
        moved["vectors"][0]["expected"] = "f" + moved["vectors"][0]["expected"][1:]
        self.assertNotEqual(_payload_digest(moved), _payload_digest(self.VECTORS))

    def test_the_canonicalisation_rules_are_written_in_the_file(self) -> None:
        """The rule has to travel with the data.

        Recovering it by trying candidate canonicalisations - which is how this
        implementation was first written - works exactly once, by one person. Anyone else
        guesses, and a wrong guess reports a fork while the numbers are identical.
        """
        rules = " ".join(self.VECTORS["_payload_canonicalization"])
        for clause in (
            "header, pseudonym_pattern, min_salt_bytes, vectors",
            "salt, tenant_id, subject_id, expected",
            "sort_keys=True",
            'separators=(",", ":")',
            "ensure_ascii=True",
            "SHA-256",
        ):
            with self.subTest(clause=clause):
                self.assertIn(clause, rules)

    def test_the_escaping_clause_is_pinned_before_it_can_matter(self) -> None:
        """``ensure_ascii``, checked against a canonical string written out by hand.

        This was written when every value in the file was ASCII, which made both settings
        produce the same digest: the clause was documented, agreed, and entirely
        unverified, and a green suite said nothing about it. The shared data has since
        gained a non-ASCII vector, so the two settings now disagree on the real payload
        (f2101ae4... against fa4c4497...) and the clause is covered by the vectors too.

        This case is kept because the two report different things. When the real vectors
        fail, the message is "this vector does not reproduce" - true, but it does not say
        whether the encoding, the separator, the truncation or the serialisation moved.
        This one fails only if the escaping rule moved, and says so.
        """
        synthetic = {
            "_comment": "not part of the payload",
            "header": "X-Acting-Subject",
            "pseudonym_pattern": "^[0-9a-f]{32}$",
            "min_salt_bytes": 32,
            "vectors": [
                {
                    "salt": "A" * 32,
                    "tenant_id": "t",
                    "subject_id": "\u00fcser",
                    "expected": "0" * 32,
                    "_why": "dropped from the payload",
                }
            ],
        }
        canonical = (
            '{"header":"X-Acting-Subject",'
            '"min_salt_bytes":32,'
            '"pseudonym_pattern":"^[0-9a-f]{32}$",'
            '"vectors":[{"expected":"00000000000000000000000000000000",'
            '"salt":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",'
            '"subject_id":"\\u00fcser",'
            '"tenant_id":"t"}]}'
        )
        self.assertEqual(
            _payload_digest(synthetic),
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        # The same payload with the raw character would digest differently - that is the
        # fork this clause prevents, so it is asserted rather than described.
        self.assertNotEqual(
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            hashlib.sha256(
                canonical.replace("\\u00fcser", "\u00fcser").encode("utf-8")
            ).hexdigest(),
        )

    def test_this_copy_still_matches_the_shared_artifact(self) -> None:
        """The check a file cannot perform on itself.

        Data cannot prove its own provenance: an edit that also recomputes the digest
        leaves the file internally flawless. Only a reference kept outside the file can
        notice, which is what CANONICAL_DIGEST is - a note of what the shared payload was
        when this copy was taken.
        """
        drifted = (
            "This vendored copy of the shared vectors no longer matches the payload "
            "recorded at the last alignment.\n"
            "CANONICAL_DIGEST is not an expected value to be corrected - it is the anchor "
            "for the shared artifact.\n"
            "A mismatch means this copy has diverged from what the other services run. "
            "Re-vendor the shared file; do not edit the constant to agree with the file.\n"
            "Only update the constant when the shared artifact itself was re-issued, and "
            "then re-vendor first."
        )
        self.assertEqual(
            _payload_digest(self.VECTORS), CANONICAL_DIGEST, drifted
        )
        self.assertEqual(
            self.VECTORS["_payload_sha256"], CANONICAL_DIGEST, drifted
        )

    def test_the_shared_data_still_covers_the_non_ascii_case(self) -> None:
        """The coverage that the escaping clause now depends on.

        🔴 Moving a rule into the data makes the data load-bearing. Delete the non-ASCII
        vector and the encoding checks it drives disappear with it - silently, with every
        remaining test green, because nothing else in the file mentions that they were
        ever connected. A guard whose subject can be removed without a failure is a guard
        with an off switch.

        Two things ride on this one vector: the UTF-8 encoding of the HMAC input (Latin-1
        can represent the character, so a narrower encoding does not raise - it derives a
        different pseudonym) and the ensure_ascii clause of the canonicalisation.
        """
        non_ascii = [
            vector["subject_id"]
            for vector in self.VECTORS["vectors"]
            if any(ord(char) > 127 for char in vector["subject_id"])
        ]
        self.assertTrue(
            non_ascii,
            "the shared vectors no longer contain a non-ASCII subject_id; the UTF-8 "
            "encoding and ensure_ascii rules are unguarded again",
        )

    def test_the_shared_salt_floor_is_the_one_enforced_here(self) -> None:
        self.assertEqual(
            self.VECTORS["min_salt_bytes"], MIN_ACTING_SUBJECT_SALT_BYTES
        )

    def test_the_shared_header_and_pattern_are_the_ones_in_use(self) -> None:
        self.assertEqual(self.VECTORS["header"], ACTING_SUBJECT_HEADER)
        from sites.identity import _ACTING_SUBJECT_RE

        self.assertEqual(_ACTING_SUBJECT_RE.pattern, self.VECTORS["pseudonym_pattern"])
