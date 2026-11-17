#!/usr/bin/env python3
"""Merge link-quality, brm-satellites, and asterisk stats onto a 5-second UTC grid."""

from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from make_track_viewer import WINDOW_END, WINDOW_START

ROOT = Path(__file__).resolve().parent
LQ_PATH = ROOT / "link-quality.csv"
BRM_PATH = ROOT / "brm-satellites.csv"
AST_PATH = ROOT / "asterisk_stats.csv"
OUT_PATH = ROOT / "merged.csv"

GRID_SECONDS = 5

LQ_FIELDS = [
    "state",
    "access_tech",
    "operator_name",
    "signal_quality_pct",
    "signal_quality_recent",
    "lte_rssi_dbm",
    "lte_rsrq_db",
    "lte_rsrp_dbm",
    "lte_snr_db",
    "nr5g_rsrq_db",
    "nr5g_rsrp_dbm",
    "nr5g_snr_db",
    "error",
]

BRM_FIELDS = [
    "triggers",
    "signal_strength_dbhz",
    "signal_quality",
    "bearer",
    "pointing_enabled",
    "pointing_elevation_deg",
    "pointing_azimuth_deg",
    "network_attached",
    "location_latitude",
    "location_longitude",
    "location_status",
    "location_fix",
    "satellites_count",
    "satellites",
    "temperature_brm_c",
    "temperature_fem_c",
    "errors",
]

LTE_METRIC_FIELDS = ("lte_rssi_dbm", "lte_rsrq_db", "lte_rsrp_dbm", "lte_snr_db")


def parse_ts(value: str) -> datetime:
    cleaned = value.replace("\x00", "").strip().replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned)


def format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def has_lte_metrics(row: dict[str, str]) -> bool:
    return any((row.get(field) or "").strip() for field in LTE_METRIC_FIELDS)


def load_link_quality(path: Path) -> list[tuple[datetime, dict[str, str]]]:
    by_ts: dict[str, tuple[dict[str, str], bool]] = {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts_raw = row["timestamp_utc"]
            candidate = {field: (row.get(field) or "") for field in LQ_FIELDS}
            candidate_has_lte = has_lte_metrics(candidate)

            if ts_raw not in by_ts:
                by_ts[ts_raw] = (candidate, candidate_has_lte)
                continue

            existing, existing_has_lte = by_ts[ts_raw]
            if candidate_has_lte and not existing_has_lte:
                chosen, dropped = candidate, existing
            elif existing_has_lte and not candidate_has_lte:
                chosen, dropped = existing, candidate
            else:
                # Prefer the later row in file order when both (or neither) have LTE.
                chosen, dropped = candidate, existing

            if not chosen.get("error") and dropped.get("error"):
                chosen = dict(chosen)
                chosen["error"] = dropped["error"]

            by_ts[ts_raw] = (chosen, has_lte_metrics(chosen))

    samples: list[tuple[datetime, dict[str, str]]] = []
    for ts_raw, (row, _) in by_ts.items():
        samples.append((parse_ts(ts_raw), row))
    samples.sort(key=lambda item: item[0])
    return samples


def load_brm(path: Path) -> list[tuple[datetime, dict[str, str]]]:
    raw_rows: list[tuple[datetime, dict[str, str]]] = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts_raw = row.get("timestamp") or ""
            if "\x00" in ts_raw or not ts_raw.replace("\x00", "").strip():
                continue
            try:
                ts = parse_ts(ts_raw)
            except ValueError:
                continue

            cleaned = {
                "triggers": (row.get("trigger") or "").strip(),
                "signal_strength_dbhz": (row.get("signal_strength_dbhz") or "").strip(),
                "signal_quality": (row.get("signal_quality") or "").strip(),
                "bearer": (row.get("bearer") or "").strip(),
                "pointing_enabled": (row.get("pointing_enabled") or "").strip(),
                "pointing_elevation_deg": (row.get("pointing_elevation_deg") or "").strip(),
                "pointing_azimuth_deg": (row.get("pointing_azimuth_deg") or "").strip(),
                "network_attached": (row.get("network_attached") or "").strip(),
                "location_latitude": (row.get("location_latitude") or "").strip(),
                "location_longitude": (row.get("location_longitude") or "").strip(),
                "location_status": (row.get("location_status") or "").strip(),
                "location_fix": (row.get("location_fix") or "").strip(),
                "satellites_count": (row.get("satellites_count") or "").strip(),
                "satellites": (row.get("satellites") or "").strip(),
                "temperature_brm_c": (row.get("temperature_brm_c") or "").strip(),
                "temperature_fem_c": (row.get("temperature_fem_c") or "").strip(),
                "errors": (row.get("errors") or "").strip(),
            }
            raw_rows.append((ts, cleaned))

    raw_rows.sort(key=lambda item: item[0])

    collapsed: list[tuple[datetime, dict[str, str]]] = []
    for ts, row in raw_rows:
        if not collapsed or collapsed[-1][0] != ts:
            collapsed.append((ts, dict(row)))
            continue

        merged = collapsed[-1][1]
        trigger = row["triggers"]
        if trigger:
            existing_triggers = merged["triggers"]
            if existing_triggers:
                parts = existing_triggers.split("|")
                if trigger not in parts:
                    merged["triggers"] = f"{existing_triggers}|{trigger}"
            else:
                merged["triggers"] = trigger

        for field in BRM_FIELDS:
            if field == "triggers":
                continue
            value = row.get(field) or ""
            if value:
                merged[field] = value

    return collapsed


def as_of(
    samples: list[tuple[datetime, dict[str, str]]], grid_ts: datetime, index: int
) -> tuple[int, dict[str, str] | None, float | None]:
    while index + 1 < len(samples) and samples[index + 1][0] <= grid_ts:
        index += 1
    if index < 0 or index >= len(samples) or samples[index][0] > grid_ts:
        return index, None, None
    age = (grid_ts - samples[index][0]).total_seconds()
    return index, samples[index][1], age


def age_str(age: float | None) -> str:
    if age is None:
        return ""
    if abs(age - round(age)) < 1e-9:
        return str(int(round(age)))
    return f"{age:.3f}".rstrip("0").rstrip(".")


def build_grid(start: datetime, end: datetime) -> list[datetime]:
    first = math.ceil(start.timestamp() / GRID_SECONDS) * GRID_SECONDS
    last = math.floor(end.timestamp() / GRID_SECONDS) * GRID_SECONDS

    ticks: list[datetime] = []
    epoch = int(first)
    last_epoch = int(last)
    while epoch <= last_epoch:
        ticks.append(datetime.fromtimestamp(epoch, tz=timezone.utc))
        epoch += GRID_SECONDS
    return ticks


def parse_asterisk_report_ts(value: str) -> datetime:
    """Parse asterisk report_generated as UTC (same convention as the other logs)."""
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


def round_to_grid(ts: datetime) -> datetime:
    """Snap a timestamp to the nearest GRID_SECONDS UTC tick."""
    epoch = ts.timestamp()
    rounded = round(epoch / GRID_SECONDS) * GRID_SECONDS
    return datetime.fromtimestamp(rounded, tz=timezone.utc)


def load_asterisk(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Load wide asterisk rows keyed by nearest merged timestamp_utc."""
    if not path.exists():
        return [], {}

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return [], {}

        # Keep all asterisk columns except the join key itself; expose original
        # report time as ast_report_generated.
        value_fields = [f for f in reader.fieldnames if f != "report_generated"]
        out_fields = ["ast_report_generated", "ast_age_s", *value_fields]

        by_grid_ts: dict[str, dict[str, str]] = {}
        for row in reader:
            report_ts = parse_asterisk_report_ts(row["report_generated"])
            grid_ts = round_to_grid(report_ts)
            grid_key = format_ts(grid_ts)
            age = (grid_ts - report_ts).total_seconds()
            payload = {
                "ast_report_generated": row["report_generated"],
                "ast_age_s": age_str(age),
            }
            for field in value_fields:
                payload[field] = row.get(field, "")
            by_grid_ts[grid_key] = payload

    return out_fields, by_grid_ts


def fill_single_asterisk_gaps(
    asterisk_by_ts: dict[str, dict[str, str]],
) -> int:
    """Fill exactly-one-tick gaps by duplicating the previous asterisk row.

    Only fills when both the preceding and following grid ticks already have
    asterisk data (gap of exactly GRID_SECONDS between filled neighbors is
    GRID_SECONDS*2). Longer gaps are left blank.
    """
    filled_keys = sorted(asterisk_by_ts)
    added = 0
    for prev_key, next_key in zip(filled_keys, filled_keys[1:]):
        prev_ts = parse_ts(prev_key)
        next_ts = parse_ts(next_key)
        gap_s = (next_ts - prev_ts).total_seconds()
        if gap_s != GRID_SECONDS * 2:
            continue
        mid_ts = prev_ts + timedelta(seconds=GRID_SECONDS)
        mid_key = format_ts(mid_ts)
        if mid_key in asterisk_by_ts:
            continue
        asterisk_by_ts[mid_key] = dict(asterisk_by_ts[prev_key])
        added += 1
    return added


def write_merged(
    path: Path,
    grid: list[datetime],
    lq_samples: list[tuple[datetime, dict[str, str]]],
    brm_samples: list[tuple[datetime, dict[str, str]]],
    asterisk_fields: list[str],
    asterisk_by_ts: dict[str, dict[str, str]],
) -> int:
    fieldnames = (
        ["timestamp_utc", "lq_age_s", "brm_age_s"]
        + [f"lq_{field}" for field in LQ_FIELDS]
        + [f"brm_{field}" for field in BRM_FIELDS]
        + asterisk_fields
    )

    lq_index = -1
    brm_index = -1
    asterisk_hits = 0

    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()

        for grid_ts in grid:
            lq_index, lq_row, lq_age = as_of(lq_samples, grid_ts, lq_index)
            brm_index, brm_row, brm_age = as_of(brm_samples, grid_ts, brm_index)

            ts_key = format_ts(grid_ts)
            out: dict[str, str] = {
                "timestamp_utc": ts_key,
                "lq_age_s": age_str(lq_age),
                "brm_age_s": age_str(brm_age),
            }
            for field in LQ_FIELDS:
                out[f"lq_{field}"] = (lq_row or {}).get(field, "")
            for field in BRM_FIELDS:
                out[f"brm_{field}"] = (brm_row or {}).get(field, "")

            ast_row = asterisk_by_ts.get(ts_key)
            if ast_row:
                asterisk_hits += 1
                for field in asterisk_fields:
                    out[field] = ast_row.get(field, "")
            else:
                for field in asterisk_fields:
                    out[field] = ""

            writer.writerow(out)

    return asterisk_hits


def main() -> None:
    lq_samples = load_link_quality(LQ_PATH)
    brm_samples = load_brm(BRM_PATH)
    if not lq_samples or not brm_samples:
        raise SystemExit("Expected samples in both input files")

    asterisk_fields, asterisk_by_ts = load_asterisk(AST_PATH)
    asterisk_reports = len(asterisk_by_ts)
    asterisk_gap_fills = fill_single_asterisk_gaps(asterisk_by_ts)

    # Match the route viewer window (make_track_viewer.WINDOW_*).
    overlap_start = max(lq_samples[0][0], brm_samples[0][0], WINDOW_START)
    overlap_end = min(lq_samples[-1][0], brm_samples[-1][0], WINDOW_END)
    grid = build_grid(overlap_start, overlap_end)
    asterisk_hits = write_merged(
        OUT_PATH,
        grid,
        lq_samples,
        brm_samples,
        asterisk_fields,
        asterisk_by_ts,
    )
    print(f"Wrote {len(grid)} rows to {OUT_PATH}")
    print(f"Overlap: {overlap_start.isoformat()} -> {overlap_end.isoformat()}")
    print(
        f"Asterisk joined on nearest {GRID_SECONDS}s tick: "
        f"{asterisk_reports} reports, {asterisk_gap_fills} single-gap fills, "
        f"{asterisk_hits} rows filled"
    )


if __name__ == "__main__":
    main()
