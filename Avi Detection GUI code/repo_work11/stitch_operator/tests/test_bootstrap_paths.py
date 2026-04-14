from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stitch_operator.bootstrap import ensure_repo_paths, project_root


class BootstrapPathTests(unittest.TestCase):
    def test_code_directory_precedes_fin6_for_shared_module_names(self):
        ensure_repo_paths()
        root = project_root()
        code_dir = str((root / "CodeDirectory").resolve())
        fin6_dir = str((root / "fin6").resolve())
        self.assertIn(code_dir, sys.path)
        self.assertIn(fin6_dir, sys.path)
        self.assertLess(sys.path.index(code_dir), sys.path.index(fin6_dir))


if __name__ == "__main__":
    unittest.main()
