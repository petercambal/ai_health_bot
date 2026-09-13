"""One-off import of historical workout log data from the "AI Trener.xlsx"
spreadsheet into health_records (typ='workout', one row per session - see
database.get_last_workout / log_workout_session for the same shape going forward).

Usage:
    uv run python scripts/import_workout_history.py path/to/AI_Trener.xlsx

Reads the xlsx directly via zipfile/ElementTree rather than adding openpyxl as a
project dependency just for this one-time script.

Sessions are grouped by (date, variant) as they appear in the sheet, with one fix-up:
a few rows in the source file have a spreadsheet fill-down bug where the date column
increments by one year per row within what is clearly a single same-day session (same
variant, one exercise per date, dates share day/month and run in consecutive years).
Those are collapsed back into one session dated by the first row's date - see
_fix_year_drift below.

Safe to re-run: skips any (date, variant) session that's already in the database.
"""

import asyncio
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from app import database

_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_LOG_SHEET = "xl/worksheets/sheet2.xml"
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")


def _col_letters(cell_ref: str) -> str:
    return "".join(ch for ch in cell_ref if ch.isalpha())


def _read_shared_strings(z: zipfile.ZipFile) -> list[str]:
    with z.open("xl/sharedStrings.xml") as f:
        tree = ET.parse(f)
    return ["".join(t.text or "" for t in si.findall(".//a:t", _NS)) for si in tree.getroot()]


def _read_log_rows(xlsx_path: Path) -> list[tuple[str, str, str, str, str, str]]:
    z = zipfile.ZipFile(xlsx_path)
    shared = _read_shared_strings(z)
    with z.open(_LOG_SHEET) as f:
        tree = ET.parse(f)

    rows: list[tuple[str, str, str, str, str, str]] = []
    for row in tree.getroot().findall(".//a:sheetData/a:row", _NS):
        cells: dict[str, str] = {}
        for c in row.findall("a:c", _NS):
            v = c.find("a:v", _NS)
            val = v.text if v is not None else ""
            if c.get("t") == "s" and val != "":
                val = shared[int(val)]
            cells[_col_letters(c.get("r"))] = val
        if not any(cells.get(k) for k in ("A", "B", "C", "D")):
            continue
        rows.append(
            (cells.get("A", ""), cells.get("B", ""), cells.get("C", ""), cells.get("D", ""), cells.get("E", ""), cells.get("F", ""))
        )

    return rows[1:]  # drop header row


def _parse_slovak_date(value: str) -> date:
    m = _DATE_RE.match(value.strip())
    if not m:
        raise ValueError(f"Unrecognized date format: {value!r}")
    day, month, year = (int(g) for g in m.groups())
    return date(year, month, day)


def _fix_year_drift(rows: list[tuple[str, str, str, str, str, str]]) -> list[tuple[str, str, str, str, str, str]]:
    """Collapses the fill-down bug described in the module docstring: a run of
    consecutive same-variant rows, each on its own date, where the dates share
    day/month and increase by exactly one year per row. Rewrites the date field of
    every row in such a run to the first row's date."""
    fixed = list(rows)
    i = 0
    while i < len(fixed):
        j = i + 1
        while (
            j < len(fixed)
            and fixed[j][1] == fixed[i][1]
            and fixed[j][0] != fixed[j - 1][0]
        ):
            try:
                d_prev = _parse_slovak_date(fixed[j - 1][0])
                d_cur = _parse_slovak_date(fixed[j][0])
            except ValueError:
                break
            if not (d_cur.day == d_prev.day and d_cur.month == d_prev.month and d_cur.year == d_prev.year + 1):
                break
            j += 1
        if j - i > 1:
            base_date = fixed[i][0]
            print(
                f"  Fixing year-drift: rows {i}-{j - 1} (variant={fixed[i][1]!r}) "
                f"all dated {base_date} instead of {[fixed[k][0] for k in range(i, j)]}"
            )
            for k in range(i, j):
                fixed[k] = (base_date, *fixed[k][1:])
            i = j
        else:
            i += 1
    return fixed


def _group_sessions(
    rows: list[tuple[str, str, str, str, str, str]],
) -> dict[tuple[date, str], list[dict]]:
    sessions: dict[tuple[date, str], list[dict]] = {}
    for date_str, variant, cvik, vykon, narocnost, poznamka in rows:
        if not (date_str and variant and cvik and vykon):
            continue
        day = _parse_slovak_date(date_str)
        entry = {"cvik": cvik.strip(), "vykon": vykon.strip()}
        if narocnost.strip():
            entry["narocnost"] = narocnost.strip()
        if poznamka.strip():
            entry["poznamka"] = poznamka.strip()
        sessions.setdefault((day, variant.strip()), []).append(entry)
    return sessions


async def _resolve_telegram_id(pool, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    rows = await pool.fetch("SELECT telegram_id FROM auth_user")
    if len(rows) != 1:
        raise SystemExit(
            f"Expected exactly one auth_user to auto-detect telegram_id, found {len(rows)}. "
            "Pass it explicitly: uv run python scripts/import_workout_history.py <xlsx> <telegram_id>"
        )
    return rows[0]["telegram_id"]


async def _import(xlsx_path: Path, telegram_id: int | None) -> None:
    rows = _read_log_rows(xlsx_path)
    print(f"Read {len(rows)} exercise rows from {xlsx_path.name}")

    rows = _fix_year_drift(rows)
    sessions = _group_sessions(rows)
    print(f"Grouped into {len(sessions)} sessions (1 row per day, as requested)\n")

    await database.connect()
    try:
        pool = database.get_pool()
        telegram_id = await _resolve_telegram_id(pool, telegram_id)
        inserted = 0
        skipped = 0
        for (day, variant), cviky in sorted(sessions.items()):
            existing = await pool.fetchrow(
                "SELECT 1 FROM health_records WHERE user_id = $1 AND typ = 'workout' "
                "AND ts::date = $2 AND data->>'variant' = $3",
                telegram_id,
                day,
                variant,
            )
            if existing:
                skipped += 1
                continue
            ts = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
            await database.insert_health_record(
                user_id=telegram_id,
                typ="workout",
                data={"variant": variant, "cviky": cviky},
                ts=ts,
            )
            inserted += 1
            print(f"  Inserted {day.isoformat()} {variant} ({len(cviky)} cvikov)")

        print(f"\nDone: {inserted} inserted, {skipped} already present.")
    finally:
        await database.disconnect()


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(
            "Usage: uv run python scripts/import_workout_history.py path/to/AI_Trener.xlsx [telegram_id]",
            file=sys.stderr,
        )
        sys.exit(1)

    path = Path(sys.argv[1]).expanduser()
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    arg_telegram_id = int(sys.argv[2]) if len(sys.argv) == 3 else None
    asyncio.run(_import(path, telegram_id=arg_telegram_id))
