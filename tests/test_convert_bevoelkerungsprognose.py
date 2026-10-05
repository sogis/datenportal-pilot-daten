"""Synthetic error cases and independent read-back checks; standard library only."""

import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "convert_bevoelkerungsprognose.py"
SPEC = importlib.util.spec_from_file_location("convert_bevoelkerungsprognose", SCRIPT)
converter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = converter
SPEC.loader.exec_module(converter)


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {scenario: self.root / f"{scenario}.csv"
                      for scenario in ("tief", "mittel", "hoch")}
        self.output = self.root / "result.csv"
        self.header = ["gemcode", "gemname", "bez", "bez_name", "ms", "ms_name",
                       "handlungsraum_name", "agglo_name", "", "2022", "", "",
                       "2023", "", ""]
        self.subheader = [""] * 9 + ["untere Grenze", "Wert", "obere Grenze"] * 2
        self.records = {}
        for index, scenario in enumerate(self.paths):
            amount = index * 20
            rows = [
                ["0010", "Büsserach", "1110", "Thierstein", "25", "Laufental",
                 "ländlich", "keine agglo", "", str(amount), str(amount + 5),
                 str(amount + 10), str(amount + 1), str(amount + 6), str(amount + 11)],
                ["0002", 'Gäu; "Test"', "1101", "Gäu", "44", "Olten",
                 "urban", "AareLand", "", str(amount + 100), str(amount + 105),
                 str(amount + 110), str(amount + 101), str(amount + 106),
                 str(amount + 111)],
            ]
            if index == 1:
                rows.reverse()  # Alignment must use the code, not the row position.
            self.records[scenario] = [self.header.copy(), self.subheader.copy(), *rows]
        self.save_inputs()

    def save_inputs(self):
        for scenario, path in self.paths.items():
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                csv.writer(handle, delimiter=";").writerows(self.records[scenario])

    def convert(self):
        return converter.convert(self.paths, self.output, "2023")

    def assert_invalid(self, message):
        self.save_inputs()
        with self.assertRaisesRegex(converter.ConversionError, message):
            self.convert()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.output.with_suffix(".metadata.json").exists())

    def test_round_trip_counts_sorting_unicode_codes_and_metadata(self):
        before = {scenario: path.read_bytes() for scenario, path in self.paths.items()}
        report = self.convert()
        self.assertEqual(report["datenzeilen"], 12)
        self.assertEqual(report["gepruefte_grenzwerte"], 24)
        self.assertEqual(report["gepruefte_geografische_felder"], 96)
        self.assertEqual(report["gemeinden"], 2)
        self.assertEqual(report["jahre"], 2)
        self.assertEqual(report["szenarien"], 3)
        with self.output.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            self.assertEqual(reader.fieldnames, [
                "gemeindecode", "gemeindename", "bezirkcode", "bezirkname",
                "ms_regioncode", "ms_regionname", "handlungsraum", "agglomeration",
                "jahr", "szenario", "bevoelkerung_untergrenze", "bevoelkerung_obergrenze",
            ])
            output = list(reader)
        keys = [(row["gemeindecode"], row["jahr"], row["szenario"]) for row in output]
        self.assertEqual(keys[:3], [("0002", "2022", s) for s in ("tief", "mittel", "hoch")])
        self.assertEqual(output[0]["gemeindename"], 'Gäu; "Test"')
        observation = next(row for row in output if
                           (row["gemeindecode"], row["jahr"], row["szenario"])
                           == ("0010", "2022", "tief"))
        self.assertEqual(observation["bevoelkerung_untergrenze"], "0")
        self.assertEqual(observation["bevoelkerung_obergrenze"], "10")
        self.assertEqual(observation["gemeindename"], "Büsserach")
        self.assertNotIn("Wert", reader.fieldnames)
        self.assertNotIn("prognoseversion", reader.fieldnames)
        self.assertFalse(self.output.read_bytes().startswith(b"\xef\xbb\xbf"))
        metadata = json.loads(self.output.with_suffix(".metadata.json").read_text())
        self.assertEqual(metadata["prognoseversion"], "2023")
        self.assertIn("Kein fachlich bestätigter", metadata["prognoseversion_hinweis"])
        self.assertEqual(metadata["pruefung"], report)
        self.assertEqual(metadata["ausgabe"]["sha256"],
                         hashlib.sha256(self.output.read_bytes()).hexdigest())
        for source in metadata["quellen"]:
            self.assertEqual(source["sha256"],
                             hashlib.sha256(before[source["szenario"]]).hexdigest())
        self.assertEqual(before, {scenario: path.read_bytes()
                                  for scenario, path in self.paths.items()})
        self.assertEqual(list(self.root.glob(".prognose-*")), [])

    def test_invalid_first_header(self):
        self.records["mittel"][0][0] = "wrong"
        self.assert_invalid("erste.*Kopfzeile")

    def test_invalid_second_header(self):
        self.records["mittel"][1][9] = "Wert"
        self.assert_invalid("zweite Kopfzeile")

    def test_missing_number(self):
        self.records["mittel"][2][9] = ""
        self.assert_invalid("Ganzzahl")

    def test_duplicate_municipality(self):
        self.records["mittel"].append(self.records["mittel"][2].copy())
        self.assert_invalid("doppelte Gemeinde")

    def test_reversed_bounds(self):
        row = self.records["tief"][2]
        row[9], row[11] = row[11], row[9]
        self.assert_invalid("falsch geordnet")

    def test_model_outside_bounds(self):
        self.records["tief"][2][10] = "99"
        self.assert_invalid("falsch geordnet")

    def test_geography_mismatch(self):
        self.records["hoch"][2][3] = "Anderer Bezirk"
        self.assert_invalid("abweichende Gemeindeangaben")

    def test_missing_municipality(self):
        self.records["hoch"].pop()
        self.assert_invalid("abweichende Gemeindemenge")

    def test_year_mismatch(self):
        self.records["hoch"][0][9] = "2024"
        self.records["hoch"][0][12] = "2025"
        self.assert_invalid("andere Jahre")

    def test_duplicate_year(self):
        self.records["hoch"][0][12] = "2022"
        self.assert_invalid("Jahre müssen eindeutig")

    def test_wrong_row_width(self):
        self.records["hoch"][2].pop()
        self.assert_invalid("falsche Spaltenzahl")

    def test_negative_and_fractional_numbers(self):
        for invalid in ("-1", "1.5", "1,5"):
            with self.subTest(invalid=invalid):
                self.records["tief"][2][9] = invalid
                self.assert_invalid("Ganzzahl")

    def test_nonempty_separator(self):
        self.records["hoch"][2][8] = "unexpected"
        self.assert_invalid("Abstandsspalte")

    def test_invalid_source_preserves_previous_output(self):
        self.output.write_text("previous csv", encoding="utf-8")
        metadata_path = self.output.with_suffix(".metadata.json")
        metadata_path.write_text("previous metadata", encoding="utf-8")
        self.records["hoch"][2][10] = ""
        self.save_inputs()
        with self.assertRaises(converter.ConversionError):
            self.convert()
        self.assertEqual(self.output.read_text(), "previous csv")
        self.assertEqual(metadata_path.read_text(), "previous metadata")

    def test_output_cannot_overwrite_input(self):
        with self.assertRaisesRegex(converter.ConversionError, "Eingabedatei überschreiben"):
            converter.convert(self.paths, self.paths["tief"])

    def test_duplicate_input_paths(self):
        paths = dict(self.paths, mittel=self.paths["tief"])
        with self.assertRaisesRegex(converter.ConversionError, "unterschiedliche Eingabedateien"):
            converter.convert(paths, self.output)

    def test_verifier_rejects_changed_missing_duplicate_and_unsorted_output(self):
        self.convert()
        sources = [converter.load_source(self.paths[s], s) for s in self.paths]
        with self.output.open(encoding="utf-8", newline="") as handle:
            original = list(csv.reader(handle, delimiter=";"))
        for mutation, message in (("changed", "Abweichung"), ("missing", "fehlen"),
                                  ("duplicate", "doppelter Schlüssel"),
                                  ("unsorted", "Sortierung")):
            with self.subTest(mutation=mutation):
                rows = [row.copy() for row in original]
                if mutation == "changed":
                    rows[1][10] = str(int(rows[1][10]) + 1)
                elif mutation == "missing":
                    rows.pop()
                elif mutation == "duplicate":
                    rows.insert(2, rows[1].copy())
                else:
                    rows[1], rows[2] = rows[2], rows[1]
                with self.output.open("w", encoding="utf-8", newline="") as handle:
                    csv.writer(handle, delimiter=";").writerows(rows)
                with self.assertRaisesRegex(converter.ConversionError, message):
                    converter.verify_output(self.output, sources)

    def test_verifier_rejects_changed_source(self):
        self.convert()
        sources = [converter.load_source(self.paths[s], s) for s in self.paths]
        self.paths["tief"].write_bytes(self.paths["tief"].read_bytes() + b"\n")
        with self.assertRaisesRegex(converter.ConversionError, "Quelldatei hat sich"):
            converter.verify_output(self.output, sources)

    def test_cli_returns_error_status_and_readable_message(self):
        args = []
        for scenario, path in self.paths.items():
            args.extend((f"--{scenario}", str(path)))
        args.extend(("--output", str(self.output)))
        self.records["hoch"][2][9] = ""
        self.save_inputs()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = converter.main(args)
        self.assertEqual(status, 1)
        self.assertIn("Fehler:", stderr.getvalue())
        self.assertIn("hoch.csv", stderr.getvalue())
        self.assertIn("Zeile", stderr.getvalue())
        self.assertFalse(self.output.exists())

    def test_equal_bounds_and_unspecified_version(self):
        for scenario in self.records:
            self.records[scenario][2][9:12] = ["0", "0", "0"]
        self.save_inputs()
        converter.convert(self.paths, self.output)
        metadata = json.loads(self.output.with_suffix(".metadata.json").read_text())
        self.assertIsNone(metadata["prognoseversion"])


if __name__ == "__main__":
    unittest.main()
