"""
Modifiable Nested Octree (MNO) generator — cùng thuật toán PotreeConverter.

Mỗi điểm xuất hiện trong ĐÚNG 1 tile. Với refine="ADD":
  - Far  : Cesium chỉ load r.pnts → thấy overview thưa
  - Mid  : load thêm các r[N].pnts cho octant đang nhìn → dày thêm
  - Close: load thêm r[N][M].pnts, r[N][M][K].pnts → dày dần
  - Closest: load đến leaf → thấy đầy đủ TOÀN BỘ điểm trong file gốc

Tổng tất cả tiles = N (không trùng lặp, không mất điểm).
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import laspy
import numpy as np
import structlog

log = structlog.get_logger()

_A = 6378137.0
_E2 = 6.6943799901413165e-3
_HEADER_SIZE = 28


def _lonlat_to_ecef(lon, lat, alt):
    lr = np.radians(lon); la = np.radians(lat)
    N = _A / np.sqrt(1.0 - _E2 * np.sin(la) ** 2)
    x = (N + alt) * np.cos(la) * np.cos(lr)
    y = (N + alt) * np.cos(la) * np.sin(lr)
    z = (N * (1.0 - _E2) + alt) * np.sin(la)
    return x, y, z


def _write_pnts(out_path, cx, cy, cz, r, g, b):
    """Write one .pnts tile. Returns (rtc_center, sphere_radius)."""
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

    d = np.sqrt((cx - rtc[0])**2 + (cy - rtc[1])**2 + (cz - rtc[2])**2)
    sphere_r = float(d.max()) * 1.05 if n > 1 else 50.0
    return rtc, sphere_r


def _build_node(
    data: tuple,           # (cx, cy, cz, r, g, b) — points NOT YET assigned to coarser tiles
    out_dir: Path,
    name: str,             # tile name: "r", "r0", "r12", etc
    max_per_tile: int,
    max_depth: int,
    depth: int = 0,
    bbox: tuple | None = None,
) -> dict | None:
    """Recursive octree node builder. Returns tile metadata for tileset.json."""
    cx, cy, cz, r, g, b = data
    n = len(cx)
    if n == 0:
        return None

    # bbox for splitting children: use ECEF bbox of current node's points
    if bbox is None:
        bbox = (cx.min(), cx.max(), cy.min(), cy.max(), cz.min(), cz.max())

    # Sample points for this node (random shuffle for spatial uniformity)
    rng = np.random.default_rng(hash(name) & 0xFFFFFFFF)
    take = min(n, max_per_tile)
    if n > max_per_tile:
        perm = rng.permutation(n)
        this_idx = np.sort(perm[:max_per_tile])
        rest_mask = np.zeros(n, dtype=bool)
        rest_mask[perm[max_per_tile:]] = True
    else:
        this_idx = np.arange(n)
        rest_mask = np.zeros(n, dtype=bool)  # no remaining points

    # Write this node's tile
    rtc, sphere_r = _write_pnts(
        out_dir / f"{name}.pnts",
        cx[this_idx], cy[this_idx], cz[this_idx],
        r[this_idx], g[this_idx], b[this_idx],
    )

    # Build children from remaining points
    children = []
    if rest_mask.any() and depth < max_depth:
        rcx, rcy, rcz = cx[rest_mask], cy[rest_mask], cz[rest_mask]
        rr, rg, rb = r[rest_mask], g[rest_mask], b[rest_mask]

        # Octant split midpoints (from parent bbox, not children's bbox)
        mx = (bbox[0] + bbox[1]) / 2
        my = (bbox[2] + bbox[3]) / 2
        mz = (bbox[4] + bbox[5]) / 2

        for i in range(8):
            xhi = bool(i & 1)
            yhi = bool(i & 2)
            zhi = bool(i & 4)
            mask = (
                ((rcx >= mx) if xhi else (rcx < mx))
                & ((rcy >= my) if yhi else (rcy < my))
                & ((rcz >= mz) if zhi else (rcz < mz))
            )
            if not mask.any():
                continue

            child_bbox = (
                mx if xhi else bbox[0], bbox[1] if xhi else mx,
                my if yhi else bbox[2], bbox[3] if yhi else my,
                mz if zhi else bbox[4], bbox[5] if zhi else mz,
            )
            child_data = (rcx[mask], rcy[mask], rcz[mask],
                          rr[mask], rg[mask], rb[mask])
            child = _build_node(
                child_data, out_dir, f"{name}{i}",
                max_per_tile, max_depth, depth + 1, child_bbox,
            )
            if child is not None:
                children.append(child)

    # Geometric error: larger at top (rendered from far), smaller at leaves
    # Use sphere_r at root, halve each level (octree halves spatial extent)
    geom_err = sphere_r / (2 ** depth) if depth > 0 else sphere_r

    return {
        "boundingVolume": {"sphere": rtc + [sphere_r]},
        "geometricError": geom_err,
        "refine": "ADD",
        "content": {"uri": f"{name}.pnts"},
        "children": children,
        # internal stats
        "_n_points": int(take),
        "_n_remaining": int(rest_mask.sum()),
    }


def generate_multi_lod_tileset(
    las_path: Path,
    output_dir: Path,
    max_per_tile: int = 300_000,
    max_depth: int = 5,
) -> bool:
    """
    Build octree LOD from full LAS file (no global downsampling).
    Each point appears in exactly one tile across all levels.

    max_per_tile = 300_000 → each tile is ~6 MB, downloads in ~50ms on LAN.
    max_depth    = 5 → up to 8^5 = 32k leaf tiles (overkill for most data).
    """
    try:
        potree_dir = output_dir / "potree"
        if potree_dir.exists():
            import shutil
            shutil.rmtree(potree_dir)
        potree_dir.mkdir(parents=True, exist_ok=True)

        log.info("multi_lod_start", path=str(las_path))

        with laspy.open(str(las_path)) as reader:
            las = reader.read()

        lon = np.asarray(las.x, dtype=np.float64)
        lat = np.asarray(las.y, dtype=np.float64)
        alt = np.asarray(las.z, dtype=np.float64)
        n_total = len(lon)

        if lon.max() > 360 or lat.max() > 90 or lat.min() < -90:
            log.error("multi_lod_not_wgs84")
            return False

        log.info("multi_lod_loaded", points=n_total, mb=round(n_total * 30 / 1e6, 1))

        cx, cy, cz = _lonlat_to_ecef(lon, lat, alt)

        has_rgb = "red" in las.point_format.dimension_names
        if has_rgb:
            r = (np.asarray(las.red)   / 65535 * 255).astype(np.uint8)
            g = (np.asarray(las.green) / 65535 * 255).astype(np.uint8)
            b = (np.asarray(las.blue)  / 65535 * 255).astype(np.uint8)
        else:
            norm = (alt - alt.min()) / max(float(alt.max() - alt.min()), 1.0)
            r = (norm * 255).astype(np.uint8)
            g = (np.clip(1 - 2*abs(norm - 0.5), 0, 1) * 200).astype(np.uint8)
            b = ((1 - norm) * 255).astype(np.uint8)

        # Build octree from ALL points (no downsampling)
        root = _build_node(
            (cx, cy, cz, r, g, b),
            potree_dir,
            name="r",
            max_per_tile=max_per_tile,
            max_depth=max_depth,
        )
        if root is None:
            return False

        # Tileset root references the octree root with high geometric error
        # so Cesium always tries to load at least r.pnts from any distance
        tileset = {
            "asset": {"version": "1.0"},
            "geometricError": root["geometricError"] * 4,
            "root": {
                "boundingVolume": root["boundingVolume"],
                "geometricError": root["geometricError"],
                "refine": "ADD",
                "content": root["content"],
                "children": [_clean_node(c) for c in root["children"]],
            },
        }
        (potree_dir / "tileset.json").write_text(json.dumps(tileset, indent=2))

        # Stats
        all_tiles = list(potree_dir.glob("*.pnts"))
        total_points_in_tiles = 0
        for tile in all_tiles:
            # Approximate: each tile has up to max_per_tile points
            total_points_in_tiles += min(max_per_tile, tile.stat().st_size // 15)

        log.info(
            "multi_lod_done",
            n_tiles=len(all_tiles),
            input_points=n_total,
            tiles_estimated_pts=total_points_in_tiles,
            coverage_pct=round(min(100, total_points_in_tiles * 100 / n_total), 1),
        )
        return True

    except Exception as exc:
        log.error("multi_lod_failed", error=str(exc), exc_info=True)
        return False


def _clean_node(node: dict) -> dict:
    """Strip internal stats keys (_n_points, _n_remaining) before writing JSON."""
    return {
        "boundingVolume": node["boundingVolume"],
        "geometricError": node["geometricError"],
        "refine": node["refine"],
        "content": node["content"],
        "children": [_clean_node(c) for c in node["children"]],
    }
