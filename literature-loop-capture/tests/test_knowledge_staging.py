from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "literature-loop-capture" / "scripts" / "knowledge_staging.py"


def load_module():
    spec = importlib.util.spec_from_file_location("knowledge_staging", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class KnowledgeStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "run"
        self.run.mkdir()
        self.module = load_module()
        self._build_fixture()

    def _article(self, rel: str, title: str, doi: str, role: str = "primary") -> Path:
        article = self.run / rel
        write_json(
            article / "metadata.json",
            {
                "title": title,
                "doi": doi,
                "year": "2024",
                "publisher": "Example Publisher",
                "journal": "Example Journal",
                "source_role": role,
                "source_bucket": "elsevier",
                "subquestion_id": "01_resources",
                "subquestion_text": "Which resources seed the graph?",
            },
        )
        write_json(article / "fulltext.json", {"title": title, "doi": doi, "abstract": "abstract"})
        (article / "fulltext.md").write_text("# Full text\n", encoding="utf-8")
        (article / "reading-note-zh.md").write_text("- 高价值seed: FlavorDB resource\n", encoding="utf-8")
        write_csv(
            article / "recommended-references.csv",
            [{"reference_title": "Useful Reference", "doi": "10.5555/ref"}],
            ["reference_title", "doi"],
        )
        figures = article / "figures"
        figures.mkdir(parents=True, exist_ok=True)
        (figures / "figure-01.png").write_bytes(b"same-figure")
        tables = article / "tables"
        tables.mkdir(parents=True, exist_ok=True)
        (tables / "table-01.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        return article

    def _build_fixture(self) -> None:
        self._article(
            "subquestions/group/01_resources/sources/elsevier/articles/primary_001",
            "Flavor Resource Paper",
            "10.1000/example",
        )
        self._article(
            "subquestions/group/01_resources/references/pdf/manual/articles/ref_001",
            "Flavor Resource Paper",
            "10.1000/example",
            "reference",
        )
        write_json(
            self.run / "coverage-review" / "subquestion-coverage-review.json",
            {
                "subquestions": [
                    {
                        "subquestion_id": "01_resources",
                        "subquestion_text": "Which resources seed the graph?",
                        "coverage_decision": "sufficient",
                        "coverage_score_0_to_5": "4.2",
                        "remaining_gaps": "No major gaps.",
                    }
                ]
            },
        )
        iteration = self.run / "loop-state" / "01_resources" / "iteration-02"
        write_json(
            iteration / "query-rationale-review.json",
            {
                "review_mode": "subagent",
                "coverage_decision": "iterate_query",
                "coverage_score_0_to_5": "3.8",
                "missing_evidence_or_terms": ["resource schema"],
                "broad_discovery_queries": ["flavor database schema"],
                "exact_openalex_grounded_targets": [
                    {
                        "exact_query": "FlavorDB",
                        "doi": "10.1000/example",
                        "publisher_route": "manual_hold",
                        "status": "matched",
                        "openalex_title": "Flavor Resource Paper",
                    }
                ],
            },
        )
        (iteration / "query-rationale-review.md").write_text("# Rationale\n", encoding="utf-8")
        write_json(iteration / "query-plan-amendment.json", {"broad_discovery_queries": ["flavor database schema"]})
        (iteration / "query-plan-amendment.md").write_text("# Amendment\n", encoding="utf-8")
        write_csv(
            self.run / "manual-pdf-needed-only.csv",
            [
                {
                    "subquestion_id": "01_resources",
                    "title": "Manual Paper",
                    "doi": "10.2000/manual",
                    "publisher": "Manual Publisher",
                    "venue": "Manual Venue",
                    "put_pdf_here": str(self.run / "subquestions/group/01_resources/references/pdf/manual/articles/ref_002"),
                }
            ],
            ["subquestion_id", "title", "doi", "publisher", "venue", "put_pdf_here"],
        )
        write_csv(self.run / "manual-pdf-priority-needed.csv", [], ["subquestion_id", "title", "doi", "publisher", "venue", "put_pdf_here"])
        write_csv(self.run / "manual-pdf-optional-needed.csv", [], ["subquestion_id", "title", "doi", "publisher", "venue", "put_pdf_here"])

    def test_builds_subquestion_dossier(self) -> None:
        out = self.module.build_knowledge(self.run)
        subq = out / "subquestions" / "01_resources"
        self.assertTrue((subq / "overview.md").exists())
        self.assertTrue((subq / "papers.md").exists())
        self.assertTrue((subq / "paper_cards").is_dir())
        self.assertTrue((subq / "figures_tables.md").exists())
        self.assertTrue((subq / "manual_pdf_dropbox" / "README.md").exists())
        self.assertIn("flavor database schema", (subq / "query_journey.md").read_text(encoding="utf-8"))

    def test_dedupes_papers_and_reports_occurrences(self) -> None:
        out = self.module.build_knowledge(self.run)
        canonical = (out / "papers" / "canonical-index.md").read_text(encoding="utf-8")
        duplicate = (out / "papers" / "duplicate-report.md").read_text(encoding="utf-8")
        self.assertIn("Flavor Resource Paper", canonical)
        self.assertIn("occurrences: `2`", canonical)
        self.assertIn("10.1000/example", duplicate)

    def test_indexes_attachments_without_copying(self) -> None:
        out = self.module.build_knowledge(self.run)
        attachments = (out / "subquestions" / "01_resources" / "figures_tables.md").read_text(encoding="utf-8")
        self.assertIn("figure-01.png", attachments)
        self.assertIn("table-01.csv", attachments)
        self.assertFalse((out / "obsidian_export" / "attachments").exists())
        manifest = (out / "obsidian_export" / "attachments_manifest.csv").read_text(encoding="utf-8")
        self.assertIn("figure-01.png", manifest)

    def test_manual_pdf_short_list_only_contains_needed_downloads(self) -> None:
        out = self.module.build_knowledge(self.run)
        csv_path = out / "subquestions" / "01_resources" / "manual_pdf_to_download.csv"
        with csv_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "needs_user_pdf")
        self.assertEqual(rows[0]["doi"], "10.2000/manual")


if __name__ == "__main__":
    unittest.main()
