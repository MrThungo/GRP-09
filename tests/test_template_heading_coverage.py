import html
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = PROJECT_ROOT / "app" / "templates"
PAGE_TITLE_PATTERN = re.compile(
    r"{%\s*block\s+page_title\s*%}(.*?){%\s*endblock\s*%}",
    re.DOTALL,
)
HEADING_PATTERN = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.DOTALL)


def normalize_label(value):
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split()).casefold()


class TemplateHeadingCoverageTest(unittest.TestCase):
    def test_page_title_is_not_repeated_in_page_content(self):
        duplicates = []

        for template in TEMPLATE_ROOT.rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            title_match = PAGE_TITLE_PATTERN.search(source)
            if not title_match:
                continue

            page_title = normalize_label(title_match.group(1))
            if not page_title:
                continue

            for heading in HEADING_PATTERN.findall(source):
                if normalize_label(heading) == page_title:
                    duplicates.append(
                        f"{template.relative_to(PROJECT_ROOT)}: {page_title}"
                    )

        self.assertEqual(
            duplicates,
            [],
            "These templates repeat the shared page title in their content: "
            + ", ".join(duplicates),
        )

    def test_landing_picture_explanation_was_removed(self):
        source = (
            TEMPLATE_ROOT / "admin" / "landing_team.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "Upload or replace the pictures shown in the public team section",
            source,
        )
        self.assertNotIn(
            "Personal profile pictures do not change the landing page",
            source,
        )


if __name__ == "__main__":
    unittest.main()
