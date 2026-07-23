import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "app" / "templates"
APP_JAVASCRIPT = PROJECT_ROOT / "app" / "static" / "js" / "app.js"
APP_STYLESHEET = PROJECT_ROOT / "app" / "static" / "css" / "app.css"


class TablePaginationCoverageTest(unittest.TestCase):
    def test_every_table_template_uses_the_paginated_app_layout(self):
        table_templates = []
        for template in TEMPLATE_ROOT.rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            if "<table" not in source:
                continue
            table_templates.append(template)
            self.assertIn(
                '{% extends "layouts/base.html" %}',
                source,
                f"{template.relative_to(PROJECT_ROOT)} must use the app layout.",
            )
            self.assertNotIn(
                "data-no-pagination",
                source,
                f"{template.relative_to(PROJECT_ROOT)} opts out of shared pagination.",
            )

        self.assertGreater(
            len(table_templates),
            0,
            "The pagination audit did not find any table templates.",
        )

    def test_shared_paginator_covers_tables_and_live_refreshes(self):
        source = APP_JAVASCRIPT.read_text(encoding="utf-8")
        self.assertIn("const DEFAULT_PAGE_SIZE = 10;", source)
        self.assertIn(
            'document.querySelectorAll("main table").forEach(initTablePagination);',
            source,
        )
        self.assertIn(
            'region.querySelectorAll("table").forEach(table => '
            "window.initTablePagination(table));",
            source,
        )
        self.assertIn(
            "window.refreshAllTablePagination({ preservePage: true });",
            source,
        )

    def test_table_layout_preserves_long_and_wide_content(self):
        source = APP_STYLESHEET.read_text(encoding="utf-8")
        self.assertIn("overflow-x: auto;", source)
        self.assertIn("overflow-wrap: anywhere;", source)
        self.assertIn("max-width: none;", source)


if __name__ == "__main__":
    unittest.main()
