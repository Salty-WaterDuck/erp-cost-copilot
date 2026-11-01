import io
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from uuid import uuid4

from costcontrol import load_dataset, run_cost
from costcontrol.adapters import AdapterError, load_mapping, read_export, write_export
from costcontrol.cli import main

ROOT = Path(__file__).resolve().parent.parent
OCT = ROOT / "data" / "demo_ningbo.json"
NOV = ROOT / "data" / "demo_ningbo_2026_11.json"


def make_dir() -> Path:
    path = ROOT / f".test-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


class AdapterRoundTripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = make_dir() / "export"
        cls.mapping = load_mapping()
        cls.ds = load_dataset(OCT)
        write_export(cls.ds, cls.dir, cls.mapping)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir.parent, ignore_errors=True)

    def test_all_files_written(self):
        names = {p.name for p in self.dir.iterdir()}
        for name in self.mapping.files.values():
            self.assertIn(name, names)
        self.assertIn("说明.txt", names)

    def test_headers_are_vendor_shaped(self):
        text = (self.dir / self.mapping.files["materials"]).read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("物料编码", text.splitlines()[0])
        self.assertIn("标准价", text.splitlines()[0])

    def test_value_mapping_written(self):
        text = (self.dir / self.mapping.files["materials"]).read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("外购", text)
        self.assertNotIn("RAW", text)

    def test_round_trip_dataset(self):
        loaded = read_export(self.dir, self.mapping)
        self.assertEqual(loaded.company, self.ds.company)
        self.assertEqual(loaded.period, self.ds.period)
        self.assertEqual(len(loaded.orders), 4)
        self.assertEqual(len(loaded.issues), 15)
        self.assertEqual(loaded.materials["RM-SS304"].mtype.value, "RAW")
        self.assertEqual(loaded.opening_balances, self.ds.opening_balances)

    def test_round_trip_cost_identical(self):
        loaded = read_export(self.dir, self.mapping)
        original = run_cost(self.ds)
        restored = run_cost(loaded)
        self.assertEqual(restored.sheet.total_cost, original.sheet.total_cost)
        self.assertEqual(restored.sheet.variance_total, original.sheet.variance_total)
        self.assertEqual(
            [o.order_no for o in restored.sheet.orders],
            [o.order_no for o in original.sheet.orders],
        )


class AdapterOpeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = make_dir() / "nov"
        cls.mapping = load_mapping()
        cls.ds = load_dataset(NOV)
        write_export(cls.ds, cls.dir, cls.mapping)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir.parent, ignore_errors=True)

    def test_wip_and_balances_round_trip(self):
        loaded = read_export(self.dir, self.mapping)
        self.assertEqual(loaded.opening_balances, self.ds.opening_balances)
        self.assertEqual(loaded.wip_opening, self.ds.wip_opening)
        self.assertEqual(
            run_cost(loaded).sheet.total_cost, run_cost(self.ds).sheet.total_cost
        )


class AdapterErrorTest(unittest.TestCase):
    def test_missing_directory(self):
        with self.assertRaises(AdapterError):
            read_export(ROOT / "no-such-export")

    def test_missing_file(self):
        empty = make_dir()
        try:
            with self.assertRaises(AdapterError):
                read_export(empty)
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class CliAdapterTest(unittest.TestCase):
    def setUp(self):
        self.dir = make_dir() / "export"

    def tearDown(self):
        shutil.rmtree(self.dir.parent, ignore_errors=True)

    def run_cli(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue() + err.getvalue()

    def test_export_then_read(self):
        code, text = self.run_cli("--export-csv", str(self.dir))
        self.assertEqual(code, 0)
        self.assertIn("已生成", text)

        code, text = self.run_cli("--csv", str(self.dir))
        self.assertEqual(code, 0)
        self.assertIn("60,054.14", text)

    def test_read_missing_export(self):
        code, text = self.run_cli("--csv", str(self.dir / "missing"))
        self.assertEqual(code, 2)
        self.assertIn("读取 ERP 导出失败", text)


if __name__ == "__main__":
    unittest.main()
