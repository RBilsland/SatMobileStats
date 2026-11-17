#!/usr/bin/env python3
"""Parse asterisk_stats_*.txt screen dumps into one structured CSV.

Keeps:
  - PiLOT: all PJSIP channel stats and all IAX2 trunk netstats
  - AWS EC2: all PJSIP channel stats, and IAX2/PiInTheSky* except *8424

Output is one wide row per report_generated timestamp.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_PATH = ROOT / "asterisk_stats.csv"

REPORT_RE = re.compile(
    r"Report Generated:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
)
NODE_RE = re.compile(r"\[\s*NODE:\s*(.+?)\s*\]\s*.*?Status:\s*(.+?)\s*$")
PJSIP_ROW_RE = re.compile(
    r"^\s*([0-9a-f]{8})\s+(\S+)\s+(\d{2}:\d{2}:\d{2})\s+(\S+)\s+"
    r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$"
)
PJSIP_INVALID_RE = re.compile(r"^\s*(PJSIP/\S+)\s+not valid\s*$", re.IGNORECASE)
IAX_ROW_RE = re.compile(
    r"^\s*(\(None\)|IAX2/\S+)\s+"
    r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+"
    r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+"
    r"(\S+)\s+(\S+)\s*$"
)
ACTIVE_IAX_RE = re.compile(r"(\d+)\s+active IAX channel", re.IGNORECASE)
FAILED_RE = re.compile(r"^Failed(?:\s*\((.+)\))?$", re.IGNORECASE)

LONG_FIELDNAMES = [
    "source_file",
    "session",
    "report_generated",
    "node",
    "node_status",
    "node_error",
    "active_iax_channels",
    "record_type",
    # PJSIP
    "pjsip_bridge_id",
    "pjsip_channel_id",
    "pjsip_uptime",
    "pjsip_uptime_s",
    "pjsip_codec",
    "pjsip_rx_count",
    "pjsip_rx_lost",
    "pjsip_rx_lost_pct",
    "pjsip_rx_jitter",
    "pjsip_tx_count",
    "pjsip_tx_lost",
    "pjsip_tx_lost_pct",
    "pjsip_tx_jitter",
    "pjsip_rtt",
    "pjsip_note",
    # IAX2
    "iax_channel",
    "iax_rtt",
    "iax_local_jitter",
    "iax_local_jb_delay",
    "iax_local_lost",
    "iax_local_lost_pct",
    "iax_local_dropped",
    "iax_local_ooo",
    "iax_local_kpkts",
    "iax_remote_jitter",
    "iax_remote_jb_delay",
    "iax_remote_lost",
    "iax_remote_lost_pct",
    "iax_remote_dropped",
    "iax_remote_ooo",
    "iax_remote_kpkts",
    "iax_first_msg",
    "iax_last_msg",
]

PJSIP_FIELDS = [
    "pjsip_bridge_id",
    "pjsip_channel_id",
    "pjsip_uptime",
    "pjsip_uptime_s",
    "pjsip_codec",
    "pjsip_rx_count",
    "pjsip_rx_lost",
    "pjsip_rx_lost_pct",
    "pjsip_rx_jitter",
    "pjsip_tx_count",
    "pjsip_tx_lost",
    "pjsip_tx_lost_pct",
    "pjsip_tx_jitter",
    "pjsip_rtt",
    "pjsip_note",
]

IAX_FIELDS = [
    "iax_channel",
    "iax_rtt",
    "iax_local_jitter",
    "iax_local_jb_delay",
    "iax_local_lost",
    "iax_local_lost_pct",
    "iax_local_dropped",
    "iax_local_ooo",
    "iax_local_kpkts",
    "iax_remote_jitter",
    "iax_remote_jb_delay",
    "iax_remote_lost",
    "iax_remote_lost_pct",
    "iax_remote_dropped",
    "iax_remote_ooo",
    "iax_remote_kpkts",
    "iax_first_msg",
    "iax_last_msg",
]

NODE_PREFIX = {
    "PiLOT": "pilot",
    "AWS EC2": "ec2",
}

WIDE_FIELDNAMES = (
    ["report_generated", "session", "source_file"]
    + [
        field
        for prefix in ("pilot", "ec2")
        for field in (
            f"{prefix}_status",
            f"{prefix}_error",
            f"{prefix}_active_iax_channels",
            *[f"{prefix}_{name}" for name in PJSIP_FIELDS],
            *[f"{prefix}_{name}" for name in IAX_FIELDS],
        )
    ]
)


def session_from_name(path: Path) -> str:
    name = path.stem  # asterisk_stats_261112_flight1
    parts = name.split("_")
    return parts[-1] if parts else name


def parse_count(value: str) -> str:
    """Expand Asterisk-style counts like 123K -> 123000. Keep original numeric text otherwise."""
    value = value.strip()
    if re.fullmatch(r"\d+[Kk]", value):
        return str(int(value[:-1]) * 1000)
    return value


def uptime_to_seconds(uptime: str) -> str:
    hours, minutes, seconds = (int(part) for part in uptime.split(":"))
    return str(hours * 3600 + minutes * 60 + seconds)


def empty_row(meta: dict[str, str]) -> dict[str, str]:
    row = {field: "" for field in LONG_FIELDNAMES}
    row.update(meta)
    return row


def prefer_iax(existing: dict[str, str] | None, candidate: dict[str, str]) -> dict[str, str]:
    """Prefer a real IAX channel over (None) when both appear in one report."""
    if existing is None:
        return candidate
    existing_channel = existing.get("iax_channel", "")
    candidate_channel = candidate.get("iax_channel", "")
    if existing_channel == "(None)" and candidate_channel != "(None)":
        return candidate
    if existing_channel != "(None)" and candidate_channel == "(None)":
        return existing
    return candidate


def widen_rows(long_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse filtered channel rows into one wide row per report_generated."""
    by_ts: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in long_rows:
        by_ts[row["report_generated"]].append(row)

    wide_rows: list[dict[str, str]] = []
    for report_generated in sorted(by_ts):
        items = by_ts[report_generated]
        wide = {field: "" for field in WIDE_FIELDNAMES}
        wide["report_generated"] = report_generated
        wide["session"] = items[0]["session"]
        wide["source_file"] = items[0]["source_file"]

        chosen: dict[tuple[str, str], dict[str, str]] = {}
        for row in items:
            prefix = NODE_PREFIX.get(row["node"])
            if not prefix:
                continue

            wide[f"{prefix}_status"] = row["node_status"] or wide[f"{prefix}_status"]
            wide[f"{prefix}_error"] = row["node_error"] or wide[f"{prefix}_error"]
            if row["active_iax_channels"]:
                wide[f"{prefix}_active_iax_channels"] = row["active_iax_channels"]

            record_type = row["record_type"]
            if record_type in {"pjsip", "pjsip_invalid"}:
                key = (prefix, "pjsip")
                # Prefer a full PJSIP stats row over an invalid placeholder.
                if key in chosen and chosen[key]["record_type"] == "pjsip" and record_type == "pjsip_invalid":
                    continue
                chosen[key] = row
            elif record_type == "iax":
                key = (prefix, "iax")
                chosen[key] = prefer_iax(chosen.get(key), row)

        for (prefix, kind), row in chosen.items():
            fields = PJSIP_FIELDS if kind == "pjsip" else IAX_FIELDS
            for field in fields:
                wide[f"{prefix}_{field}"] = row.get(field, "")

        wide_rows.append(wide)

    return wide_rows


def parse_status(raw_status: str) -> tuple[str, str]:
    raw_status = raw_status.strip()
    match = FAILED_RE.match(raw_status)
    if match:
        return "Failed", (match.group(1) or "").strip()
    if raw_status.lower().startswith("connected"):
        return "Connected", ""
    return raw_status, ""


def keep_row(row: dict[str, str]) -> bool:
    """Keep only the channel rows needed for analysis.

    PiLOT: all PJSIP + all IAX2 rows.
    AWS EC2: all PJSIP rows, plus IAX2/PiInTheSky* except channels ending in 8424.
    """
    node = row["node"]
    record_type = row["record_type"]

    if node == "PiLOT":
        return record_type in {"pjsip", "pjsip_invalid", "iax"}

    if node == "AWS EC2":
        if record_type in {"pjsip", "pjsip_invalid"}:
            return True
        if record_type == "iax":
            channel = row.get("iax_channel", "")
            return channel.startswith("IAX2/PiInTheSky") and not channel.endswith(
                "8424"
            )
        return False

    return False


def parse_file(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(errors="replace").splitlines()
    session = session_from_name(path)
    source = path.name
    rows: list[dict[str, str]] = []

    report_generated = ""
    node = ""
    node_status = ""
    node_error = ""
    active_iax = ""
    in_node = False

    def meta() -> dict[str, str]:
        return {
            "source_file": source,
            "session": session,
            "report_generated": report_generated,
            "node": node,
            "node_status": node_status,
            "node_error": node_error,
            "active_iax_channels": active_iax,
        }

    def start_node(new_node: str, raw_status: str) -> None:
        nonlocal node, node_status, node_error, active_iax, in_node
        node = new_node
        node_status, node_error = parse_status(raw_status)
        active_iax = ""
        in_node = True

    for line in lines:
        report_match = REPORT_RE.search(line)
        if report_match:
            report_generated = report_match.group(1)
            node = ""
            node_status = ""
            node_error = ""
            active_iax = ""
            in_node = False
            continue

        node_match = NODE_RE.search(line)
        if node_match:
            start_node(node_match.group(1).strip(), node_match.group(2).strip())
            continue

        if not in_node:
            continue

        active_match = ACTIVE_IAX_RE.search(line)
        if active_match:
            active_iax = active_match.group(1)
            # Retroactively fill active count on rows already emitted for this node.
            for prior in reversed(rows):
                if (
                    prior["source_file"] == source
                    and prior["report_generated"] == report_generated
                    and prior["node"] == node
                ):
                    prior["active_iax_channels"] = active_iax
                else:
                    break
            continue

        invalid_match = PJSIP_INVALID_RE.match(line)
        if invalid_match:
            row = empty_row(meta())
            row["record_type"] = "pjsip_invalid"
            row["pjsip_channel_id"] = invalid_match.group(1)
            row["pjsip_note"] = "not valid"
            if keep_row(row):
                rows.append(row)
            continue

        pjsip_match = PJSIP_ROW_RE.match(line)
        if pjsip_match:
            g = pjsip_match.groups()
            row = empty_row(meta())
            row.update(
                {
                    "record_type": "pjsip",
                    "pjsip_bridge_id": g[0],
                    "pjsip_channel_id": g[1],
                    "pjsip_uptime": g[2],
                    "pjsip_uptime_s": uptime_to_seconds(g[2]),
                    "pjsip_codec": g[3],
                    "pjsip_rx_count": parse_count(g[4]),
                    "pjsip_rx_lost": g[5],
                    "pjsip_rx_lost_pct": g[6],
                    "pjsip_rx_jitter": g[7],
                    "pjsip_tx_count": parse_count(g[8]),
                    "pjsip_tx_lost": g[9],
                    "pjsip_tx_lost_pct": g[10],
                    "pjsip_tx_jitter": g[11],
                    "pjsip_rtt": g[12],
                }
            )
            if keep_row(row):
                rows.append(row)
            continue

        iax_match = IAX_ROW_RE.match(line)
        if iax_match:
            g = iax_match.groups()
            row = empty_row(meta())
            row.update(
                {
                    "record_type": "iax",
                    "iax_channel": g[0],
                    "iax_rtt": g[1],
                    "iax_local_jitter": g[2],
                    "iax_local_jb_delay": g[3],
                    "iax_local_lost": g[4],
                    "iax_local_lost_pct": g[5],
                    "iax_local_dropped": g[6],
                    "iax_local_ooo": g[7],
                    "iax_local_kpkts": g[8],
                    "iax_remote_jitter": g[9],
                    "iax_remote_jb_delay": g[10],
                    "iax_remote_lost": g[11],
                    "iax_remote_lost_pct": g[12],
                    "iax_remote_dropped": g[13],
                    "iax_remote_ooo": g[14],
                    "iax_remote_kpkts": g[15],
                    "iax_first_msg": g[16],
                    "iax_last_msg": g[17],
                }
            )
            if keep_row(row):
                rows.append(row)
            continue

    return rows


def main() -> None:
    paths = sorted(ROOT.glob("asterisk_stats_*.txt"))
    if not paths:
        raise SystemExit(f"No asterisk_stats_*.txt files found in {ROOT}")

    long_rows: list[dict[str, str]] = []
    for path in paths:
        long_rows.extend(parse_file(path))

    wide_rows = widen_rows(long_rows)

    with OUT_PATH.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WIDE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(wide_rows)

    filled_pilot_pjsip = sum(1 for row in wide_rows if row["pilot_pjsip_channel_id"])
    filled_pilot_iax = sum(1 for row in wide_rows if row["pilot_iax_channel"])
    filled_ec2_pjsip = sum(1 for row in wide_rows if row["ec2_pjsip_channel_id"])
    filled_ec2_iax = sum(1 for row in wide_rows if row["ec2_iax_channel"])
    print(f"Wrote {len(wide_rows)} rows to {OUT_PATH}")
    print(
        "filled sections:",
        {
            "pilot_pjsip": filled_pilot_pjsip,
            "pilot_iax": filled_pilot_iax,
            "ec2_pjsip": filled_ec2_pjsip,
            "ec2_iax": filled_ec2_iax,
        },
    )


if __name__ == "__main__":
    main()
