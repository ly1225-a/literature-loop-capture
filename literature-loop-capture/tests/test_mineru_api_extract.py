from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "literature-loop-capture" / "scripts"
SCRIPT = SCRIPTS / "mineru_api_extract.py"


def load_module():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("mineru_api_extract", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MineruApiExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "run"
        self.run.mkdir()
        self.module = load_module()

    def test_scan_pdf_article_dirs_includes_manual_ref_article_ids(self) -> None:
        ref_dir = self.run / "subquestions/group/01/references/pdf/manual/articles/ref_001"
        manual_dir = self.run / "subquestions/group/01/references/pdf/manual/articles/manual_ref_001_seed"
        for article_dir in [ref_dir, manual_dir]:
            article_dir.mkdir(parents=True)
            (article_dir / "source.pdf").write_bytes(b"%PDF-1.7\n")

        scanned = {path.relative_to(self.run).as_posix() for path in self.module.scan_pdf_article_dirs(self.run)}

        self.assertIn("subquestions/group/01/references/pdf/manual/articles/ref_001", scanned)
        self.assertIn("subquestions/group/01/references/pdf/manual/articles/manual_ref_001_seed", scanned)


if __name__ == "__main__":
    unittest.main()
