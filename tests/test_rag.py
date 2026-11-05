import unittest

from costcontrol.rag import build_documents, render_hits, search, tokenize


class RagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documents = build_documents()

    def test_index_is_not_empty(self):
        self.assertGreater(len(self.documents), 100)
        sources = {document.source for document in self.documents}
        self.assertIn("README.md", sources)
        self.assertIn("docs/cost-model.md", sources)
        self.assertIn("config_templates/discrete_manufacturing.yaml", sources)

    def test_tokenize_chinese_bigrams(self):
        tokens = tokenize("未分配制造费用")
        self.assertIn("未分", tokens)
        self.assertIn("制造", tokens)
        self.assertIn("费用", tokens)

    def test_query_unallocated(self):
        hits = search("6602 管理费用 5101 制造费用", k=3)
        self.assertTrue(hits)
        self.assertTrue(any("accounting" in hit.document.source for hit in hits))

    def test_query_sqlite(self):
        hits = search("SQLite 多期间怎么持久化", k=3)
        self.assertTrue(any("persistence" in hit.document.source for hit in hits))

    def test_query_equiv_units(self):
        hits = search("约当产量怎么算", k=3)
        self.assertTrue(any("cost-model" in hit.document.source for hit in hits))

    def test_render(self):
        text = render_hits("退料", search("退料", k=2))
        self.assertIn("RAG 检索", text)
        self.assertIn("score", text)

    def test_empty_query(self):
        with self.assertRaises(ValueError):
            search("   ")


if __name__ == "__main__":
    unittest.main()
