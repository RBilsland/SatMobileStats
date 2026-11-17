#!/usr/bin/env python3
"""Generate track_points.js for the local route playback webpage."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from make_locations_kml import BRM_PATH, format_when, load_points

ROOT = Path(__file__).resolve().parent
OUT_JS = ROOT / "track_points.js"
AST_PATH = ROOT / "asterisk_stats.csv"
MERGED_PATH = ROOT / "merged.csv"

# Keep the agreed time window; do not drop points for distance from a centroid.
WINDOW_START = datetime(2026, 11, 12, 13, 29, 46, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 11, 12, 15, 49, 20, tzinfo=timezone.utc)
ASTERISK_GAP_S = 30.0
# Landing site = place the drone spends most time; mark timeline when away from it.
# Hysteresis: leave home beyond LEAVE radius (takeoff), only count landed inside ENTER.
LANDING_CELL_SCALE = 2000  # ~50 m cells
AWAY_LEAVE_RADIUS_M = 40.0
AWAY_ENTER_RADIUS_M = 12.0
AWAY_MIN_DURATION_S = 15.0
AWAY_MERGE_GAP_S = 30.0

# Light cleanup only — preserve full sweeping geometry.
MAX_SPEED_MPS = 25.0
SMOOTH_WINDOW_S = 5.0
SMOOTH_PASSES = 1
CHAIKIN_ITERATIONS = 2
RESAMPLE_INTERVAL_S = 1.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def remove_speed_outliers(
    points: list[tuple[datetime, float, float]], max_speed_mps: float
) -> list[tuple[datetime, float, float]]:
    if len(points) < 3:
        return points

    kept = [points[0]]
    for ts, lat, lon in points[1:]:
        prev_ts, prev_lat, prev_lon = kept[-1]
        dt = max((ts - prev_ts).total_seconds(), 1e-3)
        speed = haversine_m(prev_lat, prev_lon, lat, lon) / dt
        if speed <= max_speed_mps:
            kept.append((ts, lat, lon))
            continue
        # Skip spike; if the next plausible point arrives later, the path continues.
    if kept[-1] != points[-1]:
        # Always retain the true end point if reachable at a sane average speed.
        start_ts, start_lat, start_lon = kept[-1]
        end_ts, end_lat, end_lon = points[-1]
        dt = max((end_ts - start_ts).total_seconds(), 1e-3)
        if haversine_m(start_lat, start_lon, end_lat, end_lon) / dt <= max_speed_mps:
            kept.append(points[-1])
    return kept


def gaussian_smooth(
    points: list[tuple[datetime, float, float]], window_s: float
) -> list[tuple[datetime, float, float]]:
    if len(points) < 3 or window_s <= 0:
        return points

    sigma = window_s / 2.5
    smoothed: list[tuple[datetime, float, float]] = []
    for i, (ts, _, _) in enumerate(points):
        t0 = ts.timestamp()
        weight_sum = 0.0
        lat_sum = 0.0
        lon_sum = 0.0
        for j in range(i, -1, -1):
            dt = t0 - points[j][0].timestamp()
            if dt > window_s:
                break
            weight = math.exp(-0.5 * (dt / sigma) ** 2)
            weight_sum += weight
            lat_sum += weight * points[j][1]
            lon_sum += weight * points[j][2]
        for j in range(i + 1, len(points)):
            dt = points[j][0].timestamp() - t0
            if dt > window_s:
                break
            weight = math.exp(-0.5 * (dt / sigma) ** 2)
            weight_sum += weight
            lat_sum += weight * points[j][1]
            lon_sum += weight * points[j][2]
        smoothed.append((ts, lat_sum / weight_sum, lon_sum / weight_sum))
    return smoothed


def lerp_time(a: datetime, b: datetime, t: float) -> datetime:
    a_epoch = a.timestamp()
    b_epoch = b.timestamp()
    return datetime.fromtimestamp(a_epoch + (b_epoch - a_epoch) * t, tz=timezone.utc)


def chaikin_smooth(
    points: list[tuple[datetime, float, float]], iterations: int
) -> list[tuple[datetime, float, float]]:
    """Corner-cutting curve that turns a simplified spine into sweeping arcs."""
    if len(points) < 3 or iterations <= 0:
        return points

    current = points
    for _ in range(iterations):
        refined: list[tuple[datetime, float, float]] = [current[0]]
        for a, b in zip(current, current[1:]):
            q = (
                lerp_time(a[0], b[0], 0.25),
                0.75 * a[1] + 0.25 * b[1],
                0.75 * a[2] + 0.25 * b[2],
            )
            r = (
                lerp_time(a[0], b[0], 0.75),
                0.25 * a[1] + 0.75 * b[1],
                0.25 * a[2] + 0.75 * b[2],
            )
            refined.extend([q, r])
        refined.append(current[-1])
        current = refined
    return current


def resample_track(
    points: list[tuple[datetime, float, float]], interval_s: float
) -> list[tuple[datetime, float, float]]:
    if len(points) < 2 or interval_s <= 0:
        return points

    start = points[0][0].timestamp()
    end = points[-1][0].timestamp()
    if end <= start:
        return points

    resampled: list[tuple[datetime, float, float]] = []
    target = start
    i = 0
    while target <= end:
        while i + 1 < len(points) and points[i + 1][0].timestamp() < target:
            i += 1
        a_ts, a_lat, a_lon = points[i]
        if i + 1 >= len(points):
            resampled.append(points[-1])
            break
        b_ts, b_lat, b_lon = points[i + 1]
        span = max(b_ts.timestamp() - a_ts.timestamp(), 1e-6)
        t = (target - a_ts.timestamp()) / span
        ts = datetime.fromtimestamp(target, tz=timezone.utc)
        resampled.append((ts, a_lat + (b_lat - a_lat) * t, a_lon + (b_lon - a_lon) * t))
        target += interval_s

    if resampled[-1][0] != points[-1][0]:
        resampled.append(points[-1])
    return resampled


def smooth_drone_track(
    points: list[tuple[datetime, float, float]],
) -> list[tuple[datetime, float, float]]:
    cleaned = remove_speed_outliers(points, MAX_SPEED_MPS)
    calmed = cleaned
    for _ in range(SMOOTH_PASSES):
        calmed = gaussian_smooth(calmed, SMOOTH_WINDOW_S)
    sweeping = chaikin_smooth(calmed, CHAIKIN_ITERATIONS)
    return resample_track(sweeping, RESAMPLE_INTERVAL_S)


def _cell_key(lat: float, lon: float, scale: float) -> tuple[int, int]:
    return (
        round(lat * scale),
        round(lon * scale * math.cos(math.radians(lat))),
    )


def landing_site_centroid(
    points: list[tuple[datetime, float, float]], scale: float
) -> tuple[float, float]:
    """Use densest location cell as the landing site (where most time is spent)."""
    counts = Counter(_cell_key(lat, lon, scale) for _, lat, lon in points)
    top_cell = counts.most_common(1)[0][0]
    cell_points = [
        (lat, lon)
        for _, lat, lon in points
        if _cell_key(lat, lon, scale) == top_cell
    ]
    lat = sum(item[0] for item in cell_points) / len(cell_points)
    lon = sum(item[1] for item in cell_points) / len(cell_points)
    return lat, lon


def _radius_cross_time(
    a: tuple[datetime, float, float],
    b: tuple[datetime, float, float],
    home_lat: float,
    home_lon: float,
    radius_m: float,
) -> datetime:
    """Interpolate the time distance crosses radius_m between samples a and b."""
    da = haversine_m(home_lat, home_lon, a[1], a[2])
    db = haversine_m(home_lat, home_lon, b[1], b[2])
    if abs(db - da) < 1e-6:
        return b[0]
    frac = (radius_m - da) / (db - da)
    frac = min(max(frac, 0.0), 1.0)
    return lerp_time(a[0], b[0], frac)


def detect_away_segments(
    points: list[tuple[datetime, float, float]],
    home_lat: float,
    home_lon: float,
    leave_radius_m: float,
    enter_radius_m: float,
    min_duration_s: float,
    merge_gap_s: float,
) -> list[dict[str, object]]:
    """Timeline segments where the drone is away from the landing site.

    Uses hysteresis so takeoff starts once beyond leave_radius_m, but landing
    only ends the segment once inside enter_radius_m (avoids clearing on approach).
    """
    if not points:
        return []

    distances = [
        haversine_m(home_lat, home_lon, lat, lon) for _, lat, lon in points
    ]
    # Start at home if already inside the leave radius.
    is_away = distances[0] > leave_radius_m
    away_flags = [is_away]
    for dist in distances[1:]:
        if is_away:
            if dist <= enter_radius_m:
                is_away = False
        elif dist >= leave_radius_m:
            is_away = True
        away_flags.append(is_away)

    raw_segments: list[tuple[datetime, datetime]] = []
    index = 0
    while index < len(points):
        if not away_flags[index]:
            index += 1
            continue
        end = index
        while end < len(points) and away_flags[end]:
            end += 1

        if index > 0:
            start_ts = _radius_cross_time(
                points[index - 1],
                points[index],
                home_lat,
                home_lon,
                leave_radius_m,
            )
        else:
            start_ts = points[index][0]

        if end < len(points):
            end_ts = _radius_cross_time(
                points[end - 1],
                points[end],
                home_lat,
                home_lon,
                enter_radius_m,
            )
        else:
            end_ts = points[end - 1][0]

        if (end_ts - start_ts).total_seconds() >= min_duration_s:
            raw_segments.append((start_ts, end_ts))
        index = end

    if not raw_segments:
        return []

    merged: list[tuple[datetime, datetime]] = [raw_segments[0]]
    for start_ts, end_ts in raw_segments[1:]:
        prev_start, prev_end = merged[-1]
        if (start_ts - prev_end).total_seconds() <= merge_gap_s:
            merged[-1] = (prev_start, end_ts)
        else:
            merged.append((start_ts, end_ts))

    return [
        {
            "start": format_when(start_ts),
            "end": format_when(end_ts),
            "start_ts": int(start_ts.timestamp() * 1000),
            "end_ts": int(end_ts.timestamp() * 1000),
        }
        for start_ts, end_ts in merged
    ]


def load_signal_series(
    path: Path, start: datetime, end: datetime
) -> list[dict[str, float | int]]:
    """Load mobile/satellite signal samples from merged.csv for the dials."""
    if not path.exists():
        return []

    samples: list[dict[str, float | int]] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            raw_ts = (row.get("timestamp_utc") or "").strip()
            if not raw_ts:
                continue
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
            if ts < start or ts > end:
                continue

            sample: dict[str, float | int] = {"ts": int(ts.timestamp() * 1000)}
            lq_raw = (row.get("lq_signal_quality_pct") or "").strip()
            brm_raw = (row.get("brm_signal_strength_dbhz") or "").strip()
            if lq_raw:
                try:
                    sample["lq"] = float(lq_raw)
                except ValueError:
                    pass
            if brm_raw:
                try:
                    sample["brm"] = float(brm_raw)
                except ValueError:
                    pass
            if "lq" in sample or "brm" in sample:
                samples.append(sample)
    return samples


def load_asterisk_segments(
    path: Path, start: datetime, end: datetime, gap_s: float
) -> list[dict[str, object]]:
    if not path.exists():
        return []

    times: list[datetime] = []
    sessions: list[str] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            raw = (row.get("report_generated") or "").strip()
            if not raw:
                continue
            ts = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            if ts < start or ts > end:
                continue
            times.append(ts)
            sessions.append((row.get("session") or "").strip())

    if not times:
        return []

    segments: list[dict[str, object]] = []
    seg_start = times[0]
    seg_session = sessions[0]
    prev = times[0]
    for ts, session in zip(times[1:], sessions[1:]):
        if (ts - prev).total_seconds() > gap_s or (
            session and seg_session and session != seg_session
        ):
            segments.append(
                {
                    "start": format_when(seg_start),
                    "end": format_when(prev),
                    "start_ts": int(seg_start.timestamp() * 1000),
                    "end_ts": int(prev.timestamp() * 1000),
                    "session": seg_session,
                }
            )
            seg_start = ts
            seg_session = session
        prev = ts
        if session:
            seg_session = session

    segments.append(
        {
            "start": format_when(seg_start),
            "end": format_when(prev),
            "start_ts": int(seg_start.timestamp() * 1000),
            "end_ts": int(prev.timestamp() * 1000),
            "session": seg_session,
        }
    )
    return segments


def main() -> None:
    raw_points = [
        point
        for point in load_points(BRM_PATH)
        if WINDOW_START <= point[0] <= WINDOW_END
    ]
    if not raw_points:
        raise SystemExit("No latitude/longitude points found in the selected window")

    points = smooth_drone_track(raw_points)
    # Home from raw dwell; away from smoothed track so timeline matches the marker.
    home_lat, home_lon = landing_site_centroid(raw_points, LANDING_CELL_SCALE)
    away_segments = detect_away_segments(
        points,
        home_lat,
        home_lon,
        AWAY_LEAVE_RADIUS_M,
        AWAY_ENTER_RADIUS_M,
        AWAY_MIN_DURATION_S,
        AWAY_MERGE_GAP_S,
    )
    asterisk_segments = load_asterisk_segments(
        AST_PATH, WINDOW_START, WINDOW_END, ASTERISK_GAP_S
    )
    signal_series = load_signal_series(MERGED_PATH, WINDOW_START, WINDOW_END)

    payload = {
        "generated_from": BRM_PATH.name,
        "window_start": format_when(WINDOW_START),
        "window_end": format_when(WINDOW_END),
        "smoothing": {
            "max_speed_mps": MAX_SPEED_MPS,
            "smooth_window_s": SMOOTH_WINDOW_S,
            "smooth_passes": SMOOTH_PASSES,
            "chaikin_iterations": CHAIKIN_ITERATIONS,
            "resample_interval_s": RESAMPLE_INTERVAL_S,
            "raw_points": len(raw_points),
            "distance_filter": None,
        },
        "landing_site": {
            "lat": round(home_lat, 7),
            "lon": round(home_lon, 7),
            "leave_radius_m": AWAY_LEAVE_RADIUS_M,
            "enter_radius_m": AWAY_ENTER_RADIUS_M,
        },
        "away_segments": away_segments,
        "asterisk_segments": asterisk_segments,
        "signals": signal_series,
        "count": len(points),
        "start": format_when(points[0][0]),
        "end": format_when(points[-1][0]),
        "points": [
            {
                "t": format_when(ts),
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "ts": int(ts.timestamp() * 1000),
            }
            for ts, lat, lon in points
        ],
    }

    OUT_JS.write_text(
        "window.TRACK_DATA = "
        + json.dumps(payload, separators=(",", ":"))
        + ";\n"
    )
    print(
        f"Wrote {len(points)} smoothed points to {OUT_JS} "
        f"(from {len(raw_points)} raw points)"
    )
    print(f"Range: {payload['start']} -> {payload['end']}")
    print(f"Asterisk segments: {len(asterisk_segments)}")
    print(f"Signal samples: {len(signal_series)}")
    print(
        f"Landing site: {home_lat:.6f},{home_lon:.6f}; "
        f"away segments: {len(away_segments)}"
    )


if __name__ == "__main__":
    main()
