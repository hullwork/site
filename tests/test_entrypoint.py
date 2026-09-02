"""Entrypoint smoke tests for the container command form.

On 2026-08-22, ``python -m sites.api`` exposed a circular import through the composition
root that ordinary ``import sites.api_xxx`` tests never exercised. Container commands must
be tested in their actual entrypoint shape; image-level failures are invisible to mixin-only
unit tests.
"""

import os
import subprocess
import sys
import unittest


class EntrypointTests(unittest.TestCase):
    def test_m_entrypoint_gets_past_imports(self) -> None:
        """`python -m sites.api` must pass through all module-level imports.

        The circular import occurs during the execution of the module body, earlier than safe_stdout.install in __main__.
        The stack must be exposed on stderr - based on "whether there is an ImportError in the output" as the criterion. pass through
        After the import chain: When there is a DB environment, hang in serve_forever (killed by timeout), when not
        serve quickly exits due to lack of configuration (the output has been wrapped by safe_stdout, which is allowed).
        """
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "sites.api"],
                capture_output=True,
                text=True,
                timeout=5,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            combined = (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ""
            combined = stderr if isinstance(stderr, str) else stderr.decode()
        self.assertNotIn(
            "ImportError",
            combined,
            f"The entry is broken in the module-level import chain:\n{combined[-2000:]}",
        )


if __name__ == "__main__":
    unittest.main()
