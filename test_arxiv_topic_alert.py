import unittest

from arxiv_topic_alert import filter_by_keywords


class FilterByKeywordsTests(unittest.TestCase):
    def test_returns_empty_list_when_entries_none(self):
        self.assertEqual(filter_by_keywords(None, None, None), [])

    def test_accepts_none_keyword_lists(self):
        entries = [{"title": "Quantum Networks", "summary": "A study"}]
        self.assertEqual(filter_by_keywords(entries, None, None), entries)

    def test_ignores_blank_keywords(self):
        entries = [
            {"title": "AI Research", "summary": "Learning representations"},
            {"title": "Biology", "summary": "Cell study"},
        ]
        filtered = filter_by_keywords(entries, ["AI", "  "], [None, "learning"])
        self.assertEqual(filtered, entries[:1])

    def test_accepts_single_keyword_string(self):
        entries = [{"title": "Graph Theory", "summary": "Spectral graph"}]
        self.assertEqual(filter_by_keywords(entries, "graph", None), entries)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
