import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node is not installed; the layout and stats tests are JavaScript")
class JavaScriptUnitTests(unittest.TestCase):
    """The room's layout and connection-stats logic are plain JavaScript functions with their own tests in tests\\js."""

    def test_the_layout_and_stats_tests_pass(self):
        files = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
        self.assertTrue(files, "no JavaScript tests found")
        run = subprocess.run(
            [NODE, "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-1000:])


if __name__ == "__main__":
    unittest.main()
