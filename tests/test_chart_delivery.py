import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "app" / "templates"
BASE_TEMPLATE = TEMPLATE_ROOT / "layouts" / "base.html"
CHART_LIBRARY = PROJECT_ROOT / "app" / "static" / "vendor" / "chart.umd.js"


class ChartDeliveryTest(unittest.TestCase):
    def test_chart_pages_use_the_local_pinned_library(self):
        chart_templates = []
        for template in TEMPLATE_ROOT.rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            if "new C(" not in source and "new Chart(" not in source:
                continue
            chart_templates.append(template)
            self.assertIn(
                "filename='vendor/chart.umd.js'",
                source,
                f"{template.relative_to(PROJECT_ROOT)} must load the local chart library.",
            )
            self.assertNotIn(
                "cdn.jsdelivr.net/npm/chart.js",
                source,
                f"{template.relative_to(PROJECT_ROOT)} still depends on the Chart.js CDN.",
            )

        self.assertGreater(len(chart_templates), 0)

    def test_local_chart_library_is_the_expected_version(self):
        self.assertTrue(CHART_LIBRARY.is_file())
        source_header = CHART_LIBRARY.read_text(encoding="utf-8")[:200]
        self.assertIn("Chart.js v4.4.1", source_header)

    def test_deployment_url_helper_is_available_before_page_scripts(self):
        source = BASE_TEMPLATE.read_text(encoding="utf-8")
        helper_position = source.index("window.nmbUrl = function")
        page_head_position = source.index("{% block head_extra %}")
        self.assertLess(helper_position, page_head_position)


if __name__ == "__main__":
    unittest.main()
