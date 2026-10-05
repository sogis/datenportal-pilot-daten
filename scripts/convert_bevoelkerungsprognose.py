#!/usr/bin/env python3
"""Convert three population scenarios to one verified, semicolon-separated CSV.

Uses Python 3.10+ and its standard library only. Example from the repository root:

    python3 scripts/convert_bevoelkerungsprognose.py \
        --tief /Users/stefan/Downloads/2023_tiefes_Szenario.csv \
        --mittel /Users/stefan/Downloads/2023_mittleres_Szenario.csv \
        --hoch /Users/stefan/Downloads/2023_hohes_Szenario.csv \
        --output ch.so.bevoelkerung.prognose/bevoelkerungsprognose_2023.csv \
        --prognoseversion 2023

The modelled value is validated but not exported. The JSON metadata records the
supplied version label without asserting a confirmed calculation date.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


SCENARIOS = ("tief", "mittel", "hoch")
INPUT_FIELDS = (
    "gemcode", "gemname", "bez", "bez_name", "ms", "ms_name",
    "handlungsraum_name", "agglo_name",
)
OUTPUT_FIELDS = (
    "gemeindecode", "gemeindename", "bezirkcode", "bezirkname",
    "ms_regioncode", "ms_regionname", "handlungsraum", "agglomeration",
    "jahr", "szenario", "bevoelkerung_untergrenze", "bevoelkerung_obergrenze",
)
DESCRIPTIONS = (
    "Gemeindecode aus gemcode; unveränderte Zeichenfolge.",
    "Gemeindename aus gemname.",
    "Bezirkscode aus bez; unveränderte Zeichenfolge.",
    "Bezirksname aus bez_name.",
    "MS-Regionscode aus ms; unveränderte Zeichenfolge.",
    "MS-Regionsname aus ms_name.",
    "Räumliche Einordnung aus handlungsraum_name.",
    "Agglomerationszugehörigkeit aus agglo_name; keine agglo bleibt erhalten.",
    "Jahr der gelieferten Bevölkerungsprognose.",
    "Szenario entsprechend dem zugeordneten Eingabeargument.",
    "Untere Grenze des gelieferten Streubereichs, ohne Rundung.",
    "Obere Grenze des gelieferten Streubereichs, ohne Rundung.",
)


class ConversionError(ValueError):
    """A source or output failed validation."""


@dataclass
class Source:
    path: Path
    scenario: str
    sha256: str
    years: tuple[int, ...]
    geography: dict[str, tuple[str, ...]]
    bounds: dict[tuple[str, int], tuple[int, int]]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConversionError(message)


def unsigned_integer(value: str, context: str) -> int:
    require(re.fullmatch(r"[0-9]+", value) is not None,
            f"{context}: nicht negative Ganzzahl erwartet, erhalten: {value!r}")
    return int(value)


def load_source(path: Path, scenario: str) -> Source:
    raw = path.read_bytes()
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""),
                           delimiter=";", strict=True))
    require(len(rows) >= 3, f"{path.name}: zwei Kopfzeilen und Daten erwartet")
    header, subheader = rows[:2]
    require(tuple(header[:8]) == INPUT_FIELDS,
            f"{path.name}: unerwartete Gemeinde-Spalten in der ersten Kopfzeile")
    require(len(header) >= 12 and (len(header) - 9) % 3 == 0,
            f"{path.name}: vollständige Jahresgruppen mit je drei Spalten erwartet")
    require(len(subheader) == len(header),
            f"{path.name}: Kopfzeilen haben unterschiedliche Spaltenzahlen")
    require(header[8] == "" and subheader[:9] == [""] * 9,
            f"{path.name}: unerwartete Beschriftung vor den Jahresgruppen")
    years = []
    for col in range(9, len(header), 3):
        require(re.fullmatch(r"[0-9]{4}", header[col]) is not None
                and header[col + 1:col + 3] == ["", ""],
                f"{path.name}: ungültige Jahresüberschrift ab Spalte {col + 1}")
        require(subheader[col:col + 3] == ["untere Grenze", "Wert", "obere Grenze"],
                f"{path.name}: ungültige zweite Kopfzeile ab Spalte {col + 1}")
        years.append(int(header[col]))
    require(years == list(range(years[0], years[-1] + 1)),
            f"{path.name}: Jahre müssen eindeutig, lückenlos und aufsteigend sein")
    geography = {}
    bounds = {}
    for line, row in enumerate(rows[2:], start=3):
        context = f"{path.name}, Zeile {line}"
        require(len(row) == len(header), f"{context}: falsche Spaltenzahl")
        require(row[8] == "", f"{context}: Abstandsspalte muss leer sein")
        require(all(value.strip() for value in row[:8]),
                f"{context}: fehlende Gemeindeangabe")
        unsigned_integer(row[0], f"{context}, gemcode")
        require(row[0] not in geography, f"{context}: doppelte Gemeinde {row[0]}")
        geography[row[0]] = tuple(row[:8])
        for index, year in enumerate(years):
            col = 9 + 3 * index
            lower, model, upper = (
                unsigned_integer(row[col + offset],
                                 f"{context}, Jahr {year}, {subheader[col + offset]}")
                for offset in range(3)
            )
            require(lower <= model <= upper,
                    f"{context}, Jahr {year}: Grenzen oder Modellwert falsch geordnet")
            bounds[(row[0], year)] = (lower, upper)
    return Source(path, scenario, hashlib.sha256(raw).hexdigest(),
                  tuple(years), geography, bounds)


def validate_sources(sources: list[Source]) -> None:
    reference = sources[0]
    for source in sources[1:]:
        require(source.years == reference.years,
                f"{source.path.name}: andere Jahre als {reference.path.name}")
        require(source.geography.keys() == reference.geography.keys(),
                f"{source.path.name}: abweichende Gemeindemenge")
        for code, geography in reference.geography.items():
            require(source.geography[code] == geography,
                    f"{source.path.name}: abweichende Gemeindeangaben für {code}")


def write_csv(path: Path, sources: list[Source]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", lineterminator="\n")
        writer.writerow(OUTPUT_FIELDS)
        reference = sources[0]
        for code in sorted(reference.geography, key=lambda value: (int(value), value)):
            for year in reference.years:
                for source in sources:
                    writer.writerow((*source.geography[code], year, source.scenario,
                                     *source.bounds[(code, year)]))


def verify_output(path: Path, sources: list[Source]) -> dict:
    """Re-read raw inputs independently; do not use the parsed bounds or writer."""
    expected = {}
    for source in sources:
        raw = source.path.read_bytes()
        require(hashlib.sha256(raw).hexdigest() == source.sha256,
                f"{source.path.name}: Quelldatei hat sich während der Verarbeitung geändert")
        records = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""),
                                  delimiter=";", strict=True))
        columns = {}
        active_year = None
        # Independent extraction: carry the year forward across the subheaders.
        for col, (year_label, value_label) in enumerate(zip(records[0], records[1])):
            if col < 9:
                continue
            if year_label:
                active_year = int(year_label)
            if value_label in ("untere Grenze", "obere Grenze"):
                columns.setdefault(active_year, {})[value_label] = col
        for record in records[2:]:
            for year, positions in columns.items():
                key = (record[0], year, source.scenario)
                require(key not in expected, f"Doppelter Quellschlüssel bei Kontrolle: {key}")
                expected[key] = (
                    *record[:8], year, source.scenario,
                    int(record[positions["untere Grenze"]]),
                    int(record[positions["obere Grenze"]]),
                )

    seen = set()
    previous_order = None
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter=";", strict=True)
        require(tuple(next(reader, [])) == OUTPUT_FIELDS,
                "Ausgabeprüfung: falsche Kopfzeile")
        for line, record in enumerate(reader, start=2):
            require(len(record) == len(OUTPUT_FIELDS),
                    f"Ausgabeprüfung, Zeile {line}: falsche Spaltenzahl")
            year = unsigned_integer(record[8], f"Ausgabeprüfung, Zeile {line}, Jahr")
            lower = unsigned_integer(record[10], f"Ausgabeprüfung, Zeile {line}, Untergrenze")
            upper = unsigned_integer(record[11], f"Ausgabeprüfung, Zeile {line}, Obergrenze")
            key = (record[0], year, record[9])
            require(key not in seen, f"Ausgabeprüfung: doppelter Schlüssel {key}")
            require(key in expected, f"Ausgabeprüfung: unerwarteter Schlüssel {key}")
            actual = (*record[:8], year, record[9], lower, upper)
            require(actual == expected[key], f"Ausgabeprüfung: Abweichung für {key}")
            order = (int(record[0]), record[0], year, SCENARIOS.index(record[9]))
            require(previous_order is None or previous_order < order,
                    f"Ausgabeprüfung, Zeile {line}: falsche Sortierung")
            previous_order = order
            seen.add(key)
    require(seen == expected.keys(),
            f"Ausgabeprüfung: {len(expected.keys() - seen)} Beobachtungen fehlen")
    return {
        "status": "bestanden",
        "gemeinden": len({key[0] for key in seen}),
        "jahre": len({key[1] for key in seen}),
        "szenarien": len({key[2] for key in seen}),
        "datenzeilen": len(seen),
        "gepruefte_grenzwerte": 2 * len(seen),
        "gepruefte_geografische_felder": 8 * len(seen),
        "fehlende_beobachtungen": 0,
        "doppelte_beobachtungen": 0,
        "alle_ausgabefelder_entsprechen_quellen": True,
        "sortierung_geprueft": True,
        "quellenstruktur_und_grenzen_geprueft": True,
    }


def metadata_for(output: Path, staged: Path, sources: list[Source],
                 version: str | None, validation: dict) -> dict:
    columns = []
    for name, description in zip(OUTPUT_FIELDS, DESCRIPTIONS):
        column = {"name": name, "datentyp": "integer" if name in
                  ("jahr", "bevoelkerung_untergrenze", "bevoelkerung_obergrenze")
                  else "string", "beschreibung": description}
        if name.startswith("bevoelkerung_"):
            column["einheit"] = "Personen"
        if name == "szenario":
            column["zulaessige_werte"] = list(SCENARIOS)
        columns.append(column)
    return {
        "titel": "Bevölkerungsprognose nach Gemeinde, Jahr und Szenario",
        "prognoseversion": version,
        "prognoseversion_hinweis": (
            "Übergebene Versionskennung; für diese Lieferung aus den Quelldateinamen "
            "übernommen. Kein fachlich bestätigter Berechnungsstand."
            if version else "Keine Versionskennung angegeben."
        ),
        "beobachtung": "Eine Zeile je Gemeindecode, Jahr und Szenario innerhalb dieser Version.",
        "primaerschluessel": ["gemeindecode", "jahr", "szenario"],
        "sortierung": ["gemeindecode (numerisch)", "jahr", "szenario (tief, mittel, hoch)"],
        "format": {"medientyp": "text/csv", "encoding": "UTF-8", "bom": False,
                   "trennzeichen": ";", "kopfzeilen": 1},
        "spalten": columns,
        "zeitraum": {"von": sources[0].years[0], "bis": sources[0].years[-1]},
        "modellwert_enthalten": False,
        "fachliche_hinweise": [
            "Definition der Bevölkerung, Stichtag, Gemeindestand, Szenarioannahmen und "
            "Berechnung des Streubereichs sind in den Quelldateien nicht dokumentiert.",
            "Alle gelieferten Jahre einschliesslich 2022 wurden übernommen; "
            "2022 wird nicht als gemessener Istbestand ausgewiesen.",
            "Die E-Mail sieht die Publikation des Streubereichs ohne modellierten Einzelwert vor.",
        ],
        "quellen": [{"szenario": source.scenario, "dateiname": source.path.name,
                     "sha256": source.sha256, "gemeindezeilen": len(source.geography)}
                    for source in sources],
        "ausgabe": {"dateiname": output.name,
                    "sha256": hashlib.sha256(staged.read_bytes()).hexdigest()},
        "pruefung": validation,
    }


def convert(paths: dict[str, Path], output: Path, version: str | None = None) -> dict:
    require(tuple(paths) == SCENARIOS, "Die Eingaben müssen tief, mittel und hoch zugeordnet sein")
    input_paths = {path.resolve() for path in paths.values()}
    require(len(input_paths) == 3, "Drei unterschiedliche Eingabedateien erwartet")
    metadata_path = output.with_suffix(".metadata.json")
    require(output.resolve() != metadata_path.resolve(),
            "CSV und Metadaten dürfen nicht denselben Ausgabepfad haben")
    require(output.resolve() not in input_paths and metadata_path.resolve() not in input_paths,
            "Ausgabepfade dürfen keine Eingabedatei überschreiben")
    sources = [load_source(paths[scenario], scenario) for scenario in SCENARIOS]
    validate_sources(sources)
    output.parent.mkdir(parents=True, exist_ok=True)
    staged_paths = []
    try:
        for suffix in (".csv.tmp", ".json.tmp"):
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".prognose-",
                                             suffix=suffix, delete=False) as handle:
                staged_paths.append(Path(handle.name))
        staged_csv, staged_metadata = staged_paths
        write_csv(staged_csv, sources)
        validation = verify_output(staged_csv, sources)
        metadata = metadata_for(output, staged_csv, sources, version, validation)
        staged_metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        require(json.loads(staged_metadata.read_text(encoding="utf-8")) == metadata,
                "Metadatenprüfung fehlgeschlagen")
        os.replace(staged_csv, output)
        os.replace(staged_metadata, metadata_path)
        return validation
    finally:
        for path in staged_paths:
            path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(
        "Konvertiert drei Bevölkerungsprognosen in eine vollständig geprüfte Long-CSV. "
        "Schreibt zusätzlich <output>.metadata.json (ersetzt die CSV-Endung)."
    ))
    for scenario in SCENARIOS:
        parser.add_argument(f"--{scenario}", required=True, type=Path,
                            help=f"Quelldatei für das Szenario {scenario}")
    parser.add_argument("--output", required=True, type=Path, help="Pfad der Ausgabe-CSV")
    parser.add_argument("--prognoseversion", help="Versionskennung nur für die Metadaten")
    args = parser.parse_args(argv)
    try:
        result = convert({scenario: getattr(args, scenario) for scenario in SCENARIOS},
                         args.output, args.prognoseversion)
    except (OSError, ValueError, UnicodeError, csv.Error) as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1
    print(f"Konvertierung und vollständiger Quellenabgleich bestanden: {args.output}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
