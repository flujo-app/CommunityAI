from __future__ import annotations

import ast
import unittest
from pathlib import Path

FORBIDDEN_IMPORTS = {"accelerate", "drift", "hivemind", "torch", "transformers"}
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "communityai_desktop"


class RuntimeBoundaryTests(unittest.TestCase):
    def test_desktop_source_does_not_import_model_or_network_runtimes(self):
        violations = []
        for path in sorted(PACKAGE_ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    root = name.split(".", 1)[0]
                    if root in FORBIDDEN_IMPORTS:
                        violations.append(f"{path.name}:{node.lineno} imports {name}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
