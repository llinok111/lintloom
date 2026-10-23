"""能力目录解析测试：验证 CLl 文本兜底能正确处理「表格截断续行」。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.catalog import Catalog, _parse_dialects_text, _parse_rules_text

# 真实 `sqlfluff dialects` 输出片段（4.4.0，80 列终端下抓取）
DIALECT_SAMPLE = """==== sqlfluff - dialects ====
ansi:                 ANSI dialect [inherits from 'nothing']
bigquery:     Google BigQuery dialect [inherits from 'ansi']
materializ:   Materialize dialect [inherits from 'postgres']
e
postgres:          PostgreSQL dialect [inherits from 'ansi']
"""

RULE_SAMPLE = """==== sqlfluff - rules ====
AL01: [aliasing.table] Implicit/explicit aliasing of table.
      groups: all, aliasing aliases: L011
LT01: [layout.spacing] Inappropriate Spacing.
      groups: all, core, layout aliases: L001, L005
"""


class TestDialectTextParsing(unittest.TestCase):
    def test_merges_truncated_name(self):
        dialects = _parse_dialects_text(DIALECT_SAMPLE)
        labels = [d["label"] for d in dialects]
        self.assertEqual(labels, ["ansi", "bigquery", "materialize", "postgres"])

    def test_inherits_parsed(self):
        dialects = {d["label"]: d for d in _parse_dialects_text(DIALECT_SAMPLE)}
        self.assertEqual(dialects["ansi"]["inherits_from"], "nothing")
        self.assertEqual(dialects["bigquery"]["inherits_from"], "ansi")

    def test_description_cleaned(self):
        dialects = {d["label"]: d for d in _parse_dialects_text(DIALECT_SAMPLE)}
        self.assertEqual(dialects["bigquery"]["name"], "Google BigQuery")


class TestRuleTextParsing(unittest.TestCase):
    def test_codes_and_names(self):
        rules = {r["code"]: r for r in _parse_rules_text(RULE_SAMPLE)}
        self.assertEqual(set(rules), {"AL01", "LT01"})
        self.assertEqual(rules["LT01"]["name"], "layout.spacing")

    def test_groups_and_aliases(self):
        rules = {r["code"]: r for r in _parse_rules_text(RULE_SAMPLE)}
        self.assertIn("core", rules["LT01"]["groups"])
        self.assertIn("L001", rules["LT01"]["aliases"])


class TestCatalogRefs(unittest.TestCase):
    def test_rule_refs_include_alias_and_group(self):
        cat = Catalog(
            ok=True,
            rules=[{"code": "LT01", "name": "layout.spacing", "groups": ["all", "core"], "aliases": ["L001"]}],
            dialects=[{"label": "ansi"}],
        )
        refs = cat.rule_refs
        for expected in ("LT01", "layout.spacing", "core", "all", "L001"):
            self.assertIn(expected, refs)
        self.assertEqual(cat.dialect_labels, {"ansi"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
