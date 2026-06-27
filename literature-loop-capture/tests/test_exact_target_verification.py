from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "literature-loop-capture" / "scripts"


def load_script(name: str):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class ExactTargetVerificationTests(unittest.TestCase):
    def test_openalex_candidate_requires_agent_verification_before_routing(self) -> None:
        module = load_script("query_iteration_review")

        def fake_openalex_best_work(query: str) -> dict[str, object]:
            self.assertEqual(query, "NATPROD")
            return {
                "id": "https://openalex.org/W123",
                "display_name": "Nanofiber packaging from natural product waste",
                "publication_year": 2011,
                "doi": "https://doi.org/10.1016/j.carbpol.2010.01.059",
                "cited_by_count": 10,
                "primary_location": {
                    "landing_page_url": "https://www.sciencedirect.com/science/article/pii/S0144861710000601",
                    "source": {
                        "display_name": "Carbohydrate Polymers",
                        "host_organization_name": "Elsevier BV",
                    },
                },
            }

        module.openalex_best_work = fake_openalex_best_work

        rows = module.ground_exact_targets(["NATPROD"])

        self.assertEqual(rows[0]["publisher_route"], "manual_hold")
        self.assertEqual(rows[0]["candidate_publisher_route"], "elsevier")
        self.assertEqual(rows[0]["agent_openalex_verified"], "false")
        self.assertEqual(rows[0]["openalex_verification_status"], "needs_agent_disambiguation")
        self.assertIn("needs_agent_openalex_verification", rows[0]["manual_reason"])

    def test_supplemental_exact_rows_ignore_unverified_openalex_matches(self) -> None:
        module = load_script("supplemental_followup")
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            write_json(
                run / "subquestions" / "group" / "01_resources" / "subquestion.json",
                {
                    "subquestion_id": "01_resources",
                    "subquestion": "Which resources seed the graph?",
                },
            )
            write_json(
                run / "loop-state" / "01_resources" / "iteration-02" / "exact-targets.json",
                [
                    {
                        "exact_query": "NATPROD",
                        "openalex_title": "Nanofiber packaging from natural product waste",
                        "doi": "10.1016/j.carbpol.2010.01.059",
                        "publisher_route": "elsevier",
                        "agent_openalex_verified": "false",
                        "openalex_verification_status": "needs_agent_disambiguation",
                    },
                    {
                        "exact_query": "NATPROD natural products database",
                        "openalex_title": "NATPROD: a curated open natural products database",
                        "doi": "10.1093/nar/gkae1063",
                        "publisher_route": "manual_hold",
                        "agent_openalex_verified": "true",
                        "openalex_verification_status": "agent_verified",
                    },
                ],
            )

            rows = module.exact_rows(run)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_exact_query"], "NATPROD natural products database")
        self.assertEqual(rows[0]["doi"], "10.1093/nar/gkae1063")


if __name__ == "__main__":
    unittest.main()
