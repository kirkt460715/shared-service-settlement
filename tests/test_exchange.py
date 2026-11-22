"""检查交换样本可读取、引用可查，保留业务所需的数据状态。"""
import tempfile
from pathlib import Path
import unittest

from inspect_records import DATA, read_collection


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tables = {path.stem: read_collection(path.stem)[0] for path in DATA.glob("*.json")}

    def test_collections_have_data(self):
        self.assertTrue(self.tables)
        self.assertTrue(all(self.tables.values()))

    def test_reject_repeated_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "duplicated.json").write_text(
                '{"fields":["id"],"key":"id","rows":[{"id":"x"},{"id":"x"}]}', encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "记录键"):
                read_collection("duplicated", path)

    def test_foreign_keys(self):
        for source, column, table, key in REFERENCES:
            with self.subTest(source=source, column=column):
                keys = {row[key] for row in self.tables[table]}
                self.assertLessEqual({row[column] for row in self.tables[source]}, keys)

    def test_input_states_present(self):
        for table, field, value in INPUT_STATES:
            with self.subTest(table=table, field=field, value=value):
                self.assertIn(value, {row[field] for row in self.tables[table]})


REFERENCES = [('submitted_invoices', 'receipt', 'expenses', 'evidence_id'), ('allocations', 'fund', 'funds', 'fund'), ('allocations', 'cost', 'expenses', 'evidence_id')]
INPUT_STATES = [('expenses', 'reversal_for', 'E01'), ('allocations', 'transaction', 'reversed'), ('allocations', 'transaction', 'paid'), ('visits', 'status', 'cancel')]
