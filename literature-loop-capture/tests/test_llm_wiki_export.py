from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "literature-loop-capture" / "scripts" / "llm_wiki_export.py"


def load_module():
    spec = importlib.util.spec_from_file_location("llm_wiki_export", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class LlmWikiExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Path(self.tmp.name) / "run"
        self.run.mkdir()
        self.module = load_module()
        self._build_fixture()

    def _build_fixture(self) -> None:
        (self.run / "overview.md").write_text("# Final overview\n", encoding="utf-8")
        (self.run / "subquestion-final-summaries-zh.md").write_text("# Final summaries\n", encoding="utf-8")
        subq = self.run / "_knowledge" / "subquestions" / "01_resources"
        subq.mkdir(parents=True)
        for name, body in {
            "overview.md": "# SQ overview\n",
            "papers.md": "# Papers\n",
            "reading_notes_index.md": "- Paper -> `subquestions/group/01_resources/sources/elsevier/articles/primary_001/reading-note-zh.md`\n",
            "important_seeds.md": "# Seeds\n",
            "recommended_references.md": "# References\n",
            "query_journey.md": "# Query Journey\n",
            "coverage.md": "# Coverage\n",
            "figures_tables.md": "# Figures\n",
            "subquestion_summaries.md": "# Subquestion summaries\n",
        }.items():
            (subq / name).write_text(body, encoding="utf-8")
        cards = subq / "paper_cards"
        cards.mkdir()
        (cards / "paper-card.md").write_text("# Paper card\n", encoding="utf-8")
        article = self.run / "subquestions/group/01_resources/sources/elsevier/articles/primary_001"
        write_json(
            article / "metadata.json",
            {
                "title": "Flavor Resource Paper",
                "doi": "10.1000/example",
                "publisher": "elsevier",
                "subquestion_id": "01_resources",
            },
        )
        write_json(
            article / "fulltext.json",
            {
                "title": "Flavor Resource Paper",
                "abstract": "Click to copy section linkSection link copied!Complete abstract text for the paper.",
            },
        )
        (article / "captured-fulltext.md").write_text("# Full text\n\nBody.\n", encoding="utf-8")
        (article / "reading-note-zh.md").write_text("# 阅读笔记\n", encoding="utf-8")
        (article / "references.md").write_text("# References\n\n- Important reference.\n", encoding="utf-8")
        (article / "recommended-references.md").write_text(
            "# Recommended References\n\n- Follow-up reference.\n", encoding="utf-8"
        )
        (article / "source.pdf").write_bytes(b"%PDF-1.7\n")
        figure = article / "figures" / "fig1.png"
        figure.parent.mkdir(parents=True)
        figure.write_bytes(b"png")
        table = article / "tables" / "table1.csv"
        table.parent.mkdir(parents=True)
        table.write_text("a,b\n1,2\n", encoding="utf-8")
        attachments = self.run / "_knowledge" / "llm_wiki"
        attachments.mkdir(parents=True)
        (attachments / "attachments_manifest.csv").write_text("path,sha256\nfigure.png,abc\n", encoding="utf-8")

    def test_exports_knowledge_as_llm_wiki_raw_sources(self) -> None:
        out = self.module.export_llm_wiki(self.run)
        self.assertTrue((out / "purpose.md").exists())
        self.assertTrue((out / "schema.md").exists())
        self.assertTrue((out / "raw_sources" / "final" / "overview.md").exists())
        self.assertTrue((out / "raw_sources" / "final" / "subquestion-final-summaries-zh.md").exists())
        self.assertTrue((out / "raw_sources" / "subquestions" / "01_resources" / "query_journey.md").exists())
        notes = list((out / "raw_sources" / "subquestions" / "01_resources" / "reading_notes").glob("*.md"))
        self.assertEqual(len(notes), 1)
        note_text = notes[0].read_text(encoding="utf-8")
        self.assertIn('source_kind: "reading_note"', note_text)
        self.assertIn('doi: "10.1000/example"', note_text)
        self.assertTrue((out / "raw_sources" / "attachments_manifest.csv").exists())

    def test_manifest_records_provenance(self) -> None:
        out = self.module.export_llm_wiki(self.run)
        with (out / "manifest.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        kinds = {row["source_kind"] for row in rows}
        self.assertIn("final_overview", kinds)
        self.assertIn("reading_note", kinds)
        self.assertIn("paper_card", kinds)
        reading = next(row for row in rows if row["source_kind"] == "reading_note")
        self.assertEqual(reading["subquestion_id"], "01_resources")
        self.assertEqual(reading["doi"], "10.1000/example")
        self.assertIn("subquestions/group/01_resources", reading["source_path"])

    def test_project_article_omits_missing_optional_markdown_sections(self) -> None:
        article = self.run / "subquestions/group/01_resources/sources/elsevier/articles/primary_001"
        (article / "references.md").unlink()
        (article / "recommended-references.md").unlink()

        out = self.module.export_llm_wiki_project(self.run)

        article_text = next((out / "raw" / "sources" / "articles").rglob("article.md")).read_text(encoding="utf-8")
        self.assertIn("## Reading Note", article_text)
        self.assertNotIn("## References", article_text)
        self.assertNotIn("## Recommended References", article_text)

    def test_project_export_skips_unresolved_manual_pdf_placeholders(self) -> None:
        placeholder = self.run / "subquestions/group/01_resources/references/pdf/manual/articles/ref_001"
        write_json(
            placeholder / "metadata.json",
            {
                "title": "Manual PDF Not Supplied",
                "doi": "10.1000/missing",
                "publisher": "manual",
                "subquestion_id": "01_resources",
                "source_role": "reference",
            },
        )

        out = self.module.export_llm_wiki_project(self.run)

        article_texts = [
            path.read_text(encoding="utf-8")
            for path in (out / "raw" / "sources" / "articles").rglob("article.md")
        ]
        self.assertEqual(len(article_texts), 1)
        self.assertNotIn("Manual PDF Not Supplied", "\n".join(article_texts))

    def test_project_export_skips_pdf_only_articles_without_normalized_text(self) -> None:
        pdf_only = self.run / "subquestions/group/01_resources/references/pdf/manual/articles/ref_002"
        write_json(
            pdf_only / "metadata.json",
            {
                "title": "PDF Only Not Normalized",
                "doi": "10.1000/pdf-only",
                "publisher": "manual",
                "subquestion_id": "01_resources",
                "source_role": "reference",
            },
        )
        (pdf_only / "source.pdf").write_bytes(b"%PDF-1.7\n")

        out = self.module.export_llm_wiki_project(self.run)

        article_texts = [
            path.read_text(encoding="utf-8")
            for path in (out / "raw" / "sources" / "articles").rglob("article.md")
        ]
        self.assertEqual(len(article_texts), 1)
        self.assertNotIn("PDF Only Not Normalized", "\n".join(article_texts))

    def test_exports_llm_wiki_project_layout(self) -> None:
        out = self.module.export_llm_wiki_project(self.run)
        self.assertTrue((out / "purpose.md").exists())
        self.assertTrue((out / "schema.md").exists())
        self.assertTrue((out / "raw" / "sources" / "dossier" / "subquestions" / "01_resources" / "query_journey.md").exists())
        self.assertFalse((out / "wiki" / "sources").exists())
        self.assertTrue((out / "wiki" / "index.md").exists())
        self.assertTrue((out / "wiki" / "overview.md").exists())
        self.assertTrue((out / "wiki" / "log.md").exists())
        self.assertTrue((out / "wiki" / "subquestions").is_dir())
        self.assertTrue((out / "wiki" / "queries").is_dir())
        self.assertTrue((out / "wiki" / "ledgers").is_dir())
        article_text = next((out / "raw" / "sources" / "articles").rglob("article.md")).read_text(encoding="utf-8")
        self.assertIn("type: \"article_source\"", article_text)
        self.assertIn("subquestion_id: \"01_resources\"", article_text)
        self.assertIn("## Bibliographic Metadata", article_text)
        self.assertIn("Complete abstract text for the paper.", article_text)
        self.assertNotIn("Click to copy section link", article_text)
        self.assertIn("## Local Assets", article_text)
        self.assertIn("assets/figures/fig1.png", article_text)
        self.assertIn("## Full Text", article_text)
        self.assertIn("# Full text", article_text)
        self.assertIn("## Reading Note", article_text)
        self.assertIn("# 阅读笔记", article_text)
        self.assertIn("## References", article_text)
        self.assertIn("Important reference.", article_text)
        self.assertIn("## Recommended References", article_text)
        self.assertIn("Follow-up reference.", article_text)
        self.assertFalse(list((out / "raw" / "sources" / "articles").rglob("captured-fulltext.md")))
        self.assertFalse(list((out / "raw" / "sources" / "articles").rglob("assets/*")))
        self.assertTrue(list((out / "raw" / "assets" / "articles").rglob("assets/figures/fig1.png")))
        self.assertTrue(list((out / "raw" / "assets" / "articles").rglob("assets/tables/table1.csv")))
        self.assertFalse(list((out / "raw" / "assets" / "articles").rglob("source.pdf")))
        self.assertTrue(list((out / "raw" / "provenance" / "articles").rglob("captured-fulltext.md")))
        subq_text = next((out / "wiki" / "subquestions").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("## Raw Dossier Pages", subq_text)
        self.assertIn("## Raw Article Sources", subq_text)
        self.assertIn("raw/sources/dossier/subquestions/01_resources/query_journey.md", subq_text)
        self.assertIn("raw/sources/articles/01_resources", subq_text)
        self.assertNotIn("## Source Pages", subq_text)
        self.assertIn("raw/assets", (out / "README.md").read_text(encoding="utf-8"))
        with (out / "manifest.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        kinds = {row["kind"] for row in rows}
        self.assertNotIn("source_page", kinds)
        self.assertIn("article_source", kinds)
        self.assertIn("article_provenance", kinds)
        self.assertIn("asset", kinds)
        article_sources = [
            row for row in rows if row["kind"] == "article_source" and row["asset_kind"] == "article.md"
        ]
        assets = [row for row in rows if row["kind"] == "asset"]
        self.assertEqual(len(article_sources), 1)
        self.assertEqual(len(assets), 2)
        log_text = (out / "wiki" / "log.md").read_text(encoding="utf-8")
        self.assertIn(f"- Manifest rows: {len(rows)}", log_text)
        self.assertIn("- Article sources: 1", log_text)
        self.assertIn("- Assets: 2", log_text)


if __name__ == "__main__":
    unittest.main()
