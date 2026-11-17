#!/usr/bin/env python3
"""Build a Google Earth KML track from BRM latitude/longitude samples."""

from __future__ import annotations

import csv
import html
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRM_PATH = ROOT / "brm-satellites.csv"
OUT_PATH = ROOT / "locations.kml"


def parse_ts(value: str) -> datetime:
    cleaned = value.replace("\x00", "").strip().replace("Z", "+00:00")
    return datetime.fromisoformat(cleaned).astimezone(timezone.utc)


def format_when(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_points(path: Path) -> list[tuple[datetime, float, float]]:
    points: list[tuple[datetime, float, float]] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            ts_raw = (row.get("timestamp") or "").replace("\x00", "").strip()
            lat_raw = (row.get("location_latitude") or "").strip()
            lon_raw = (row.get("location_longitude") or "").strip()
            if not ts_raw or not lat_raw or not lon_raw:
                continue
            try:
                ts = parse_ts(ts_raw)
                lat = float(lat_raw)
                lon = float(lon_raw)
            except ValueError:
                continue
            points.append((ts, lat, lon))

    points.sort(key=lambda item: item[0])

    # Drop consecutive duplicates to keep the KML lighter without changing the path.
    deduped: list[tuple[datetime, float, float]] = []
    for ts, lat, lon in points:
        if deduped and deduped[-1][1] == lat and deduped[-1][2] == lon:
            # Keep the latest timestamp at this position so the track still advances.
            deduped[-1] = (ts, lat, lon)
            continue
        deduped.append((ts, lat, lon))
    return deduped


def write_kml(path: Path, points: list[tuple[datetime, float, float]]) -> None:
    if not points:
        raise SystemExit("No latitude/longitude points found")

    start_ts, start_lat, start_lon = points[0]
    end_ts, end_lat, end_lon = points[-1]

    when_lines = "\n".join(f"        <when>{format_when(ts)}</when>" for ts, _, _ in points)
    coord_lines = "\n".join(
        f"        <gx:coord>{lon} {lat} 0</gx:coord>" for _, lat, lon in points
    )
    line_coords = "\n".join(f"          {lon},{lat},0" for _, lat, lon in points)

    name = "BRM location track"
    description = (
        f"{len(points)} positions from {format_when(start_ts)} to {format_when(end_ts)}"
    )

    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"
     xmlns:gx="http://www.google.com/kml/ext/2.2">
  <Document>
    <name>{html.escape(name)}</name>
    <description>{html.escape(description)}</description>

    <Style id="trackStyle">
      <LineStyle>
        <color>ff00a0ff</color>
        <width>3</width>
      </LineStyle>
      <IconStyle>
        <scale>0.8</scale>
      </IconStyle>
    </Style>
    <Style id="startStyle">
      <IconStyle>
        <color>ff00ff00</color>
        <scale>1.1</scale>
      </IconStyle>
      <LabelStyle>
        <scale>0.9</scale>
      </LabelStyle>
    </Style>
    <Style id="endStyle">
      <IconStyle>
        <color>ff0000ff</color>
        <scale>1.1</scale>
      </IconStyle>
      <LabelStyle>
        <scale>0.9</scale>
      </LabelStyle>
    </Style>

    <Placemark>
      <name>Path</name>
      <description>{html.escape(description)}</description>
      <styleUrl>#trackStyle</styleUrl>
      <LineString>
        <tessellate>1</tessellate>
        <altitudeMode>clampToGround</altitudeMode>
        <coordinates>
{line_coords}
        </coordinates>
      </LineString>
    </Placemark>

    <Placemark>
      <name>Timed track</name>
      <description>Use Google Earth time slider to replay movement.</description>
      <styleUrl>#trackStyle</styleUrl>
      <gx:Track>
        <altitudeMode>clampToGround</altitudeMode>
{when_lines}
{coord_lines}
      </gx:Track>
    </Placemark>

    <Placemark>
      <name>Start</name>
      <description>{html.escape(format_when(start_ts))}</description>
      <TimeStamp><when>{format_when(start_ts)}</when></TimeStamp>
      <styleUrl>#startStyle</styleUrl>
      <Point>
        <coordinates>{start_lon},{start_lat},0</coordinates>
      </Point>
    </Placemark>

    <Placemark>
      <name>End</name>
      <description>{html.escape(format_when(end_ts))}</description>
      <TimeStamp><when>{format_when(end_ts)}</when></TimeStamp>
      <styleUrl>#endStyle</styleUrl>
      <Point>
        <coordinates>{end_lon},{end_lat},0</coordinates>
      </Point>
    </Placemark>
  </Document>
</kml>
"""
    path.write_text(content)


def main() -> None:
    points = load_points(BRM_PATH)
    write_kml(OUT_PATH, points)
    print(f"Wrote {len(points)} positions to {OUT_PATH}")
    print(
        f"Range: {format_when(points[0][0])} -> {format_when(points[-1][0])} "
        f"({points[0][1]:.6f},{points[0][2]:.6f} -> {points[-1][1]:.6f},{points[-1][2]:.6f})"
    )


if __name__ == "__main__":
    main()
