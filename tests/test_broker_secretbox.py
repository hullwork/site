"""Tests for sites.broker.secretbox: AES-GCM with AAD = appEnvId + NUL + name."""
from __future__ import annotations

import unittest

from sites.broker import secretbox

from tests import broker_fixtures as fx


class SecretBoxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = fx.secret_key()

    def test_round_trip(self) -> None:
        blob = secretbox.encrypt(self.key, "app-1", "ZENDESK_TOKEN", "s3cr3t")
        self.assertEqual(secretbox.decrypt(self.key, "app-1", "ZENDESK_TOKEN", blob), "s3cr3t")

    def test_ciphertext_is_versioned(self) -> None:
        blob = secretbox.encrypt(self.key, "app-1", "NAME", "value")
        self.assertEqual(blob[:1], b"\x01")

    def test_wrong_app_env_id_fails_aad(self) -> None:
        blob = secretbox.encrypt(self.key, "app-1", "NAME", "value")
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt(self.key, "app-2", "NAME", blob)

    def test_wrong_name_fails_aad(self) -> None:
        blob = secretbox.encrypt(self.key, "app-1", "NAME", "value")
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt(self.key, "app-1", "OTHER_NAME", blob)

    def test_wrong_key_fails(self) -> None:
        blob = secretbox.encrypt(self.key, "app-1", "NAME", "value")
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt(fx.secret_key(), "app-1", "NAME", blob)

    def test_unsupported_version_is_refused(self) -> None:
        blob = secretbox.encrypt(self.key, "app-1", "NAME", "value")
        forged = b"\x02" + blob[1:]
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt(self.key, "app-1", "NAME", forged)

    def test_truncated_ciphertext_is_refused(self) -> None:
        with self.assertRaises(secretbox.SecretBoxError):
            secretbox.decrypt(self.key, "app-1", "NAME", b"\x01tooshort")

    def test_key_must_be_32_bytes(self) -> None:
        import tempfile

        for size in (16, 31, 33):
            with self.subTest(size=size):
                handle = tempfile.NamedTemporaryFile(delete=False)
                handle.write(b"x" * size)
                handle.close()
                with self.assertRaises(RuntimeError):
                    secretbox.load_key(handle.name)


if __name__ == "__main__":
    unittest.main()
