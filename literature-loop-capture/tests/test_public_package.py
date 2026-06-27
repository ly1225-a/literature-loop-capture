import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "literature-loop-capture"


def load_script(name: str):
    path = PUBLIC / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PublicPackageTests(unittest.TestCase):
    def test_public_package_has_no_institutional_access_terms(self):
        forbidden = [
            "S" + "CU",
            "Web" + "VPN",
            "web" + "vpn",
            "yit" + "link",
            "Sich" + "uan",
            "web" + "vpn." + "s" + "cu.edu.cn",
            "publisher." + "s" + "cu.edu.cn",
            "id-" + "s" + "cu",
        ]
        checked_roots = [PUBLIC, ROOT / "README.md", ROOT / "MANIFEST.txt"]
        violations: list[str] = []
        for root in checked_roots:
            paths = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
            for path in paths:
                if any(part in {".git", "__pycache__", ".pytest_cache"} for part in path.parts):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for term in forbidden:
                    if term in text:
                        violations.append(f"{path.relative_to(ROOT)} contains {term}")
        self.assertEqual([], violations)

    def test_structured_publisher_urls_are_direct(self):
        discovery = load_script("discovery_core")
        urls = discovery.search_urls_for_query(
            "flavor knowledge graph",
            2021,
            2026,
            "direct",
            "FLA,REV",
            True,
        )
        joined = "\n".join(urls)
        self.assertIn("https://www.sciencedirect.com/search", joined)
        self.assertIn("https://pubs.acs.org/action/doSearch", joined)
        self.assertIn("https://onlinelibrary.wiley.com/action/doSearch", joined)
        self.assertIn("https://link.springer.com/search", joined)
        self.assertNotIn("/direct/", joined)


if __name__ == "__main__":
    unittest.main()
