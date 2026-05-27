"""
Multi-level LOD 3D Tiles generator — pure Python, no multiprocessing.

Strategy: octree spatial decomposition. Each level subsamples points so:
  Level 0 (root):     ~50k points covering full bbox        — visible from far
  Level 1 (8 tiles):  ~80k points each (8 octants)          — medium zoom
  Level 2 (≤64 tiles): ~50k points each (only dense ones)    — close zoom

Cesium 3D Tiles spec: at each level, refine="ADD" means children render
ON TOP of parent (cumulative detail). Result: smooth zoom from overview
to full density, just like desktop point cloud viewers.

Output structure matches py3dtiles / PotreeConverter:
  potree/
  ├── tileset.json
  ├── r.pnts        (level 0)
  ├── r0.pnts ... r7.pnts   (level 1, 8 octants)
  └── r0X.pnts ...         (level 2, only for dense octants)
"""
from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Optional

import laspy
import numpy as np
import structlog

log = structlog.get_logger()

_A = 6378137.0
_E2 = 6.6943799901413165e-3
_HEADER_SIZE = 28


def _lonlat_to_ecef(lon: np.ndarray, lat: np.ndarray, alt: np.ndarray):
    lr = np.radians(lon); la = np.radians(lat)
    N = _A / np.sqrt(1.0 - _E2 * np.sin(la) ** 2)
    x = (N + alt) * np.cos(la) * np.cos(lr)
    y = (N + alt) * np.cos(la) * np.sin(lr)
    z = (N * (1.0 - _E2) + alt) * np.sin(la)
    return x, y, z


def _write_pnts(
    out_path: Path,
    cx: np.ndarray, cy: np.ndarray, cz: np.ndarray,
    r: np.ndarray, g: np.ndarray, b: np.ndarray,
) -> tuple[list[float], float]:
    """Write single .pnts file. Returns (sphere_center_ecef, sphere_radius)."""
    rtc = [float(cx.mean()), float(cy.mean()), float(cz.mean())]
    pos = np.column_stack([
        (cx - rtc[0]).astype(np.float32),
        (cy - rtc[1]).astype(np.float32),
        (cz - rtc[2]).astype(np.float32),
    ])
    cols = np.column_stack([r.astype(np.uint8), g.astype(np.uint8), b.astype(np.uint8)])
    n = len(pos)

    pos_bytes = pos.tobytes()
    col_bytes = cols.tobytes()

    ft_json = json.dumps({
        "POINTS_LENGTH": n,
        "RTC_CENTER": rtc,
        "POSITION": {"byteOffset": 0},
        "RGB": {"byteOffset": len(pos_bytes)},
    }, separators=(",", ":"))
    raw = ft_json.encode("utf-8")
    needed = ((_HEADER_SIZE + len(raw) + 7) // 8) * 8 - _HEADER_SIZE
    ft_json_bytes = raw.ljust(needed, b" ")

    ft_bin_raw = pos_bytes + col_bytes
    pad = (8 - len(ft_bin_raw) % 8) % 8
    ft_bin_bytes = ft_bin_raw + b"\x00" * pad

    total_len = _HEADER_SIZE + len(ft_json_bytes) + len(ft_bin_bytes)

    with open(out_path, "wb") as f:
        f.write(b"pnts")
        f.write(struct.pack("<I", 1))
        f.write(struct.pack("<I", total_len))
        f.write(struct.pack("<I", len(ft_json_bytes)))
        f.write(struct.pack("<I", len(ft_bin_bytes)))
        f.write(struct.pack("<I", 0))
        f.write(struct.pack("<I", 0))
        f.write(ft_json_bytes)
        f.write(ft_bin_bytes)

    # Bounding sphere
    d = np.sqrt((cx - rtc[0])**2 + (cy - rtc[1])**2 + (cz - rtc[2])**2)
    return rtc, float(d.max()) * 1.05


def _octant_split(cx, cy, cz, r, g, b):
    """Split points into 8 octants based on bbox midpoints. Returns list of 8 arrays-tuples."""
    mx, my, mz = (cx.min() + cx.max()) / 2, (cy.min() + cy.max()) / 2, (cz.min() + cz.max()) / 2
    octants = []
    for i in range(8):
        xmask = (cx >= mx) if (i & 1) else (cx < mx)
        ymask = (cy >= my) if (i & 2) else (cy < my)
        zmask = (cz >= mz) if (i & 4) else (cz < mz)
        m = xmask & ymask & zmask
        if m.sum() == 0:
            octants.append(None)
        else:
            octants.append((cx[m], cy[m], cz[m], r[m], g[m], b[m]))
    return octants


def _sample(arrays: tuple, n_max: int, rng):
    """Random sample up to n_max points from tuple of arrays."""
    n = len(arrays[0])
    if n <= n_max:
        return arrays
    idx = rng.choice(n, n_max, replace=False)
    idx.sort()
    return tuple(a[idx] for a in arrays)


def generate_multi_lod_tileset(
    las_path: Path,
    output_dir: Path,
    max_total_points: int = 2_000_000,
) -> bool:
    """Generate Cesium 3D Tiles with proper octree LOD using pure Python."""
    try:
        potree_dir = output_dir / "potree"
        if potree_dir.exists():
            import shutil
            shutil.rmtree(potree_dir)
        potree_dir.mkdir(parents=True, exist_ok=True)

        log.info("multi_lod_start", path=str(las_path))

        # ── Read LAS/LAZ ────────────────────────────────────────────
        with laspy.open(str(las_path)) as reader:
            las = reader.read()

        lon = np.asarray(las.x, dtype=np.float64)
        lat = np.asarray(las.y, dtype=np.float64)
        alt = np.asarray(las.z, dtype=np.float64)
        n_total = len(lon)

        # Validate WGS-84
        if lon.max() > 360 or lat.max() > 90 or lat.min() < -90:
            log.error("multi_lod_not_wgs84")
            return False

        # Downsample globally if huge (input cap)
        rng = np.random.default_rng(42)
        if n_total > max_total_points:
            idx = rng.choice(n_total, max_total_points, replace=False)
            idx.sort()
            lon, lat, alt = lon[idx], lat[idx], alt[idx]
        else:
            idx = np.arange(n_total)

        # Convert to ECEF
        cx, cy, cz = _lonlat_to_ecef(lon, lat, alt)

        # Colors
        has_rgb = "red" in las.point_format.dimension_names
        if has_rgb:
            r = (np.asarray(las.red)[idx]   / 65535 * 255).astype(np.uint8)
            g = (np.asarray(las.green)[idx] / 65535 * 255).astype(np.uint8)
            b = (np.asarray(las.blue)[idx]  / 65535 * 255).astype(np.uint8)
        else:
            norm = (alt - alt.min()) / max(float(alt.max() - alt.min()), 1.0)
            r = (norm * 255).astype(np.uint8)
            g = (np.clip(1 - 2*abs(norm - 0.5), 0, 1) * 200).astype(np.uint8)
            b = ((1 - norm) * 255).astype(np.uint8)

        n_used = len(cx)
        log.info("multi_lod_loaded", points=n_used)

        # ── Build octree LOD ────────────────────────────────────────
        # Level 0: root with subsampled overview
        L0_PTS, L1_PTS, L2_PTS = 80_000, 100_000, 80_000

        root_data = _sample((cx, cy, cz, r, g, b), L0_PTS, rng)
        root_rtc, root_sphere_r = _write_pnts(potree_dir / "r.pnts", *root_data)
        log.info("multi_lod_l0_written", points=len(root_data[0]))

        # Level 1: 8 octants
        l1_tiles = []
        octants_l1 = _octant_split(cx, cy, cz, r, g, b)
        for i, oct_data in enumerate(octants_l1):
            if oct_data is None or len(oct_data[0]) < 1000:
                continue
            sampled = _sample(oct_data, L1_PTS, rng)
            tile_name = f"r{i}.pnts"
            rtc, sphere_r = _write_pnts(potree_dir / tile_name, *sampled)

            children = []
            # Level 2: subdivide if still dense
            if len(oct_data[0]) > L1_PTS * 1.5:
                sub_octants = _octant_split(*oct_data)
                for j, sub in enumerate(sub_octants):
                    if sub is None or len(sub[0]) < 500:
                        continue
                    sub_sampled = _sample(sub, L2_PTS, rng)
                    sub_name = f"r{i}{j}.pnts"
                    sub_rtc, sub_sphere_r = _write_pnts(
                        potree_dir / sub_name, *sub_sampled
                    )
                    children.append({
                        "boundingVolume": {"sphere": sub_rtc + [sub_sphere_r]},
                        "geometricError": 5.0,
                        "refine": "ADD",
                        "content": {"uri": sub_name},
                    })

            l1_tiles.append({
                "boundingVolume": {"sphere": rtc + [sphere_r]},
                "geometricError": 30.0,
                "refine": "ADD",
                "content": {"uri": tile_name},
                "children": children,
            })

        log.info("multi_lod_l1_written", n_tiles=len(l1_tiles))

        # ── Build tileset.json ──────────────────────────────────────
        tileset = {
            "asset": {"version": "1.0"},
            "geometricError": 1000.0,
            "root": {
                "boundingVolume": {"sphere": root_rtc + [root_sphere_r]},
                "geometricError": 100.0,
                "refine": "ADD",
                "content": {"uri": "r.pnts"},
                "children": l1_tiles,
            },
        }
        (potree_dir / "tileset.json").write_text(json.dumps(tileset, indent=2))

        n_tiles = len(list(potree_dir.glob("*.pnts")))
        log.info("multi_lod_done", n_tiles=n_tiles, l1_count=len(l1_tiles))
        return True

    except Exception as exc:
        log.error("multi_lod_failed", error=str(exc), exc_info=True)
        return False
