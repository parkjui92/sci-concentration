import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import concentrate


class TestCLI(unittest.TestCase):
    def test_invalid_inputs_fail_before_network(self):
        for script in ("concentrate.py", "kipris_patents.py"):
            for extra in (("--years", "2025-2015"), ("--years", "bad"), ("--max-pages", "0")):
                with self.subTest(script=script, extra=extra):
                    p = subprocess.run([sys.executable, "-I", str(ROOT / script), "test", *extra], capture_output=True, text=True)
                    self.assertEqual(p.returncode, 2)
                    self.assertNotIn("Traceback", p.stderr)

    def test_empty_keywords_and_parent_directory(self):
        for term in (" ", "label / "):
            p = subprocess.run([sys.executable, "-I", str(ROOT / "concentrate.py"), term], capture_output=True, text=True)
            self.assertEqual(p.returncode, 2)
        self.assertEqual(concentrate.safe_dirname(".."), "keyword")
