"""
Cesium 3D Tiles .pnts generator — pure Python fallback.

Key spec requirement (3D Tiles 1.0):
  "The JSON header must be padded with spaces (0x20) so that the start of the
   Feature Table Binary section is aligned to an 8-byte boundary."

  .pnts tile header = 28 bytes  (28 % 8 = 4)
  → json_length must satisfy: (28 + json_length) % 8 == 0
  → json_length % 8 == 4  (NOT 0!)

If json is padded to a multiple of 8 the binary starts at a 4-byte boundary,
float32 POSITION values are misread, and all points appear spread across the globe.
"""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import laspy
import numpy as np
import structlog

log = structlog.get_logger()

_A  = 6378137.0
_E2 = 6.6943799901413165e-3


def _lonlat_to_ecef(lon_deg: np.ndarray, lat_deg: np.ndarray, alt_m: np.ndarray):
    lon = np.radians(lon_deg)
    lat = np.radians(lat_deg)
    N = _A / np.sqrt(1.0 - _E2 * np.sin(lat) ** 2)
    x = (N + alt_m) * np.cos(lat) * np.cos(lon)
    y = (N + alt_m) * np.cos(lat) * np.sin(lon)
    z = (N * (1.0 - _E2) + alt_m) * np.sin(lat)
    return x, y, z


def _pad_to_alignment(data: bytes, offset_from_file_start: int, alignment: int = 8) -> bytes:
    """Pad `data` with spaces/zeros so that (offset + len(padded)) % alignment == 0."""
    current = offset_from_file_start + len(data)
    remainder = current % alignment
    if remainder != 0:
        data = data + b" " * (alignment - remainder)
    return data


def generate_pnts_tileset(
    las_path: Path,
    output_dir: Path,
    source_crs: str | None = None,
    max_points: int = 800_000,  # ~50MB pnts file, manageable for Cesium
) -> bool:
    """Read WGS-84 LAZ, write Cesium 3D Tiles .pnts + tileset.json. Returns True on success."""
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        potree_dir = output_dir / "potree"
        potree_dir.mkdir(exist_ok=True)

        log.info("pnts_start", path=str(las_path))

        with laspy.open(str(las_path)) as reader:
            las = reader.read()

        lon = np.asarray(las.x, dtype=np.float64)
        lat = np.asarray(las.y, dtype=np.float64)
        alt = np.asarray(las.z, dtype=np.float64)

        # ── Validate: must be WGS-84 degrees ────────────────────────────
        if lon.max() > 360 or lon.min() < -360 or lat.max() > 90 or lat.min() < -90:
            log.error("pnts_not_wgs84",
                      lon_range=(float(lon.min()), float(lon.max())),
                      lat_range=(float(lat.min()), float(lat.max())))
            return False

        n_total = len(lon)

        # ── Downsample ──────────────────────────────────────────────────
        if n_total > max_points:
            idx = np.random.default_rng(42).choice(n_total, max_points, replace=False)
            idx.sort()
            lon, lat, alt = lon[idx], lat[idx], alt[idx]
        else:
            idx = np.arange(n_total)

        # ── WGS-84 → ECEF ───────────────────────────────────────────────
        cx, cy, cz = _lonlat_to_ecef(lon, lat, alt)

        # RTC center = mean ECEF
        rtc = np.array([cx.mean(), cy.mean(), cz.mean()], dtype=np.float64)

        # Positions relative to RTC center (float32, metres)
        pos = np.column_stack([
            (cx - rtc[0]).astype(np.float32),
            (cy - rtc[1]).astype(np.float32),
            (cz - rtc[2]).astype(np.float32),
        ])

        # ── Colors ──────────────────────────────────────────────────────
        has_rgb = "red" in las.point_format.dimension_names
        if has_rgb:
            r = (np.asarray(las.red)[idx]  / 65535.0 * 255).astype(np.uint8)
            g = (np.asarray(las.green)[idx] / 65535.0 * 255).astype(np.uint8)
            b = (np.asarray(las.blue)[idx]  / 65535.0 * 255).astype(np.uint8)
        else:
            norm = (alt - alt.min()) / max(float(alt.max() - alt.min()), 1.0)
            r = (norm * 255).astype(np.uint8)
            g = (np.clip(1 - 2 * abs(norm - 0.5), 0, 1) * 200).astype(np.uint8)
            b = ((1 - norm) * 255).astype(np.uint8)
        colors = np.column_stack([r, g, b])

        n_pts     = len(pos)
        pos_bytes = pos.tobytes()        # n*12 bytes
        col_bytes = colors.tobytes()     # n*3  bytes

        # ── Feature table JSON ──────────────────────────────────────────
        ft_json_str = json.dumps({
            "POINTS_LENGTH": n_pts,
            "RTC_CENTER":    [float(rtc[0]), float(rtc[1]), float(rtc[2])],
            "POSITION":      {"byteOffset": 0},
            "RGB":           {"byteOffset": len(pos_bytes)},
        }, separators=(",", ":"))

        # CRITICAL: pad JSON so (HEADER_SIZE + json_len) % 8 == 0
        # Header = 28 bytes; 28 % 8 = 4 → json_len % 8 must equal 4
        HEADER_SIZE = 28
        ft_json_raw = ft_json_str.encode("utf-8")
        needed = ((HEADER_SIZE + len(ft_json_raw) + 7) // 8) * 8 - HEADER_SIZE
        ft_json_bytes = ft_json_raw.ljust(needed, b" ")

        # ── Feature table binary ────────────────────────────────────────
        ft_binary_raw = pos_bytes + col_bytes
        # Pad binary section to 8-byte boundary
        bin_pad = (8 - len(ft_binary_raw) % 8) % 8
        ft_binary_bytes = ft_binary_raw + b"\x00" * bin_pad

        total_len = HEADER_SIZE + len(ft_json_bytes) + len(ft_binary_bytes)

        # ── Write .pnts ─────────────────────────────────────────────────
        pnts_path = potree_dir / "pointcloud.pnts"
        with open(pnts_path, "wb") as f:
            f.write(b"pnts")
            f.write(struct.pack("<I", 1))                        # version
            f.write(struct.pack("<I", total_len))                # byteLength
            f.write(struct.pack("<I", len(ft_json_bytes)))       # featureTableJSONByteLength
            f.write(struct.pack("<I", len(ft_binary_bytes)))     # featureTableBinaryByteLength
            f.write(struct.pack("<I", 0))                        # batchTableJSONByteLength
            f.write(struct.pack("<I", 0))                        # batchTableBinaryByteLength
            f.write(ft_json_bytes)
            f.write(ft_binary_bytes)

        # ── Bounding sphere for tileset.json ────────────────────────────
        sphere_center = [float(rtc[0]), float(rtc[1]), float(rtc[2])]
        # Radius = actual max ECEF distance from center to any point
        dists = np.sqrt(
            (cx - rtc[0]) ** 2 + (cy - rtc[1]) ** 2 + (cz - rtc[2]) ** 2
        )
        sphere_r = float(dists.max()) * 1.1

        # Log actual geographic extent for debugging
        import math as _math
        lon_range_m = (float(lon.max()) - float(lon.min())) * 111320 * _math.cos(_math.radians(float(lat.mean())))
        lat_range_m = (float(lat.max()) - float(lat.min())) * 111320
        log.info("pnts_extent",
                 lon_range_m=round(lon_range_m),
                 lat_range_m=round(lat_range_m),
                 elev_range_m=round(float(alt.max() - alt.min()), 1),
                 sphere_r_m=round(sphere_r))

        tileset = {
            "asset": {"version": "1.0"},
            "geometricError": 500.0,
            "root": {
                "boundingVolume": {"sphere": sphere_center + [sphere_r]},
                "geometricError": 0.0,
                "refine": "ADD",
                "content": {"uri": "pointcloud.pnts"},
            },
        }
        (potree_dir / "tileset.json").write_text(json.dumps(tileset, indent=2))

        log.info("pnts_done",
                 n_pts=n_pts,
                 sphere_r_m=round(sphere_r, 1),
                 pnts_mb=round(pnts_path.stat().st_size / 1e6, 2))
        return True

    except Exception as exc:
        log.error("pnts_failed", error=str(exc), exc_info=True)
        return False
