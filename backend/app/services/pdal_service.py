"""
Point cloud processing pipeline — pure Python implementation.

Replaces PDAL CLI + Python bindings with:
  laspy  — read/write LAS/LAZ (pure Python + lazrs Rust wheel, self-contained)
  pyproj — coordinate reprojection (manylinux wheel, bundles PROJ)
  numpy  — statistical noise filtering

This eliminates all native apt dependencies (no libpdal-dev, no libgdal-dev,
no libproj-dev, no libgeos-dev) so the Docker image stays small and portable.

Pipeline:
  1. Detect CRS from LAS/LAZ VLR headers (laspy)
  2. Statistical outlier filter (numpy z-score on elevation)
  3. Remove noise point classes 7 + 18
  4. Reproject to EPSG:4326 (pyproj Transformer)
  5. Write reprojected.laz (laspy + lazrs)
  6. Extract metadata: bbox, density, RGB/intensity flags
  7. Run PotreeConverter for LOD octree (optional subprocess)
"""

from __future__ import annotations

import asyncio
import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import laspy
import numpy as np
import structlog
from pyproj import CRS, Transformer

from app.services.pnts_writer import generate_pnts_tileset
from app.services.multi_lod_writer import generate_multi_lod_tileset

log = structlog.get_logger()


@dataclass
class PointCloudMetadata:
    source_crs: Optional[str] = None
    output_crs: str = "EPSG:4326"
    point_count: int = 0
    bbox_3d: dict = field(default_factory=dict)
    center_lon: float = 0.0
    center_lat: float = 0.0
    center_elevation: float = 0.0
    elevation_min: float = 0.0
    elevation_max: float = 0.0
    density_pts_per_m2: float = 0.0
    has_rgb: bool = False
    has_intensity: bool = False
    las_version: str = "1.2"
    lod_levels: int = 0


ProgressCallback = Callable[[float, str], None]


class PDAlPipeline:
    """Pure-Python point cloud processor. 'PDAL' in the name is legacy."""

    def __init__(
        self,
        input_path: Path,
        output_dir: Path,
        potree_converter_path: str = "/usr/local/bin/PotreeConverter",
        threads: int = 4,
        source_crs_override: str | None = None,
    ) -> None:
        self.input_path = input_path
        self.output_dir = output_dir
        self.potree_path = potree_converter_path
        self.threads = threads
        self.source_crs_override = source_crs_override
        self._callbacks: list[ProgressCallback] = []

    def add_progress_callback(self, cb: ProgressCallback) -> None:
        self._callbacks.append(cb)

    def _emit(self, pct: float, step: str) -> None:
        for cb in self._callbacks:
            try:
                cb(pct, step)
            except Exception:
                pass
        log.info("pipeline_progress", pct=round(pct, 1), step=step)

    # ── Step 1: CRS Detection ────────────────────────────────────────────────

    def detect_crs(self) -> Optional[str]:
        """Read CRS from LAS/LAZ VLR headers using laspy."""
        try:
            with laspy.open(str(self.input_path)) as reader:
                las = reader.read()

            for vlr in las.vlrs:
                # OGC WKT (record_id 2112)
                if vlr.record_id == 2112:
                    wkt = vlr.record_data.decode("utf-8", errors="ignore").rstrip("\x00")
                    if wkt.strip():
                        crs = CRS.from_wkt(wkt)
                        epsg = crs.to_epsg()
                        result = f"EPSG:{epsg}" if epsg else crs.to_string()
                        log.info("crs_from_wkt_vlr", crs=result)
                        return result

                # GeoTIFF keys (record_id 34735)
                if vlr.record_id == 34735:
                    try:
                        crs = CRS.from_user_input(vlr.record_data)
                        epsg = crs.to_epsg()
                        result = f"EPSG:{epsg}" if epsg else crs.to_string()
                        log.info("crs_from_geotiff_vlr", crs=result)
                        return result
                    except Exception:
                        pass

        except Exception as exc:
            log.warning("crs_detection_failed", error=str(exc))

        # Heuristic: if coordinates look like VN-2000 projected meters, guess EPSG
        # This runs as last resort when VLR headers don't have CRS info
        try:
            with laspy.open(str(self.input_path)) as f:
                las = f.read()
            x_mean = float(np.asarray(las.x).mean())
            y_mean = float(np.asarray(las.y).mean())
            # VN-2000 TM-3: Easting 400k-700k, Northing 900k-2600k
            if 400_000 < x_mean < 700_000 and 900_000 < y_mean < 2_600_000:
                # Distinguish CM 105 vs CM 108 by Easting
                crs = "EPSG:3405" if x_mean < 650_000 else "EPSG:3406"
                log.warning("crs_heuristic_vn2000", x_mean=x_mean, y_mean=y_mean, crs=crs)
                return crs
        except Exception:
            pass

        log.warning("crs_not_detected", hint="file will be processed without reprojection")
        return None

    # ── Step 2-5: Filter + Reproject (pure Python) ──────────────────────────

    def _process_points(self, output_path: Path, source_crs: Optional[str]) -> None:
        """Load → filter → reproject → write using laspy + numpy + pyproj."""
        log.info("reading_las", path=str(self.input_path))
        with laspy.open(str(self.input_path)) as reader:
            las = reader.read()

        x = np.asarray(las.x, dtype=np.float64)
        y = np.asarray(las.y, dtype=np.float64)
        z = np.asarray(las.z, dtype=np.float64)
        n_before = len(x)
        log.info("points_loaded", count=n_before)

        # --- Noise filter: statistical outlier on Z ---
        if len(z) > 0:
            z_mean, z_std = float(z.mean()), float(z.std())
            mask: np.ndarray = np.abs(z - z_mean) < 2.2 * z_std
        else:
            mask = np.ones(len(z), dtype=bool)

        # --- Class filter: remove noise (7) and high noise (18) ---
        try:
            cls = np.asarray(las.classification, dtype=np.uint8)
            mask &= (cls != 7) & (cls != 18)
        except Exception:
            pass  # no classification dimension — skip

        x, y, z = x[mask], y[mask], z[mask]
        log.info("points_after_filter", kept=len(x), removed=n_before - len(x))

        # --- Reproject to EPSG:4326 ---
        # VN-2000 TOWGS84 fix: pyproj's EPSG:34xx codes ignore the 7-parameter
        # datum shift (TOWGS84) → ~710m offset. Use explicit PROJ4 strings instead.
        _VN2000_TOWGS84 = (
            "-191.90441429,-39.30318279,-111.45032835,"
            "0.009288360000000001,-0.01975479,0.00427372,0.252906278"
        )
        _VN2000_PROJ4 = {
            # Central meridian 102° (Tây Bắc)
            "EPSG:3299": f"+proj=tmerc +lat_0=0 +lon_0=102 +k=0.9999 +x_0=500000 +y_0=0 +ellps=WGS84 +towgs84={_VN2000_TOWGS84} +units=m +no_defs",
            # Central meridian 105° (Bắc, Bắc Trung Bộ — Thanh Hoá, Nghệ An…)
            "EPSG:3405": f"+proj=tmerc +lat_0=0 +lon_0=105 +k=0.9999 +x_0=500000 +y_0=0 +ellps=WGS84 +towgs84={_VN2000_TOWGS84} +units=m +no_defs",
            # Central meridian 108° (Nam Trung Bộ, Nam Bộ)
            "EPSG:3406": f"+proj=tmerc +lat_0=0 +lon_0=108 +k=0.9999 +x_0=500000 +y_0=0 +ellps=WGS84 +towgs84={_VN2000_TOWGS84} +units=m +no_defs",
            # Aliases sometimes used in Vietnamese software
            "EPSG:3857":  None,  # Web Mercator — keep as-is (not VN2000)
        }

        target_crs = "EPSG:4326"
        if source_crs and source_crs.upper().replace(" ", "") not in ("EPSG:4326", "4326"):
            try:
                crs_upper = source_crs.upper().strip()

                # WKT/PROJ4 string passed directly → use as-is
                if source_crs.strip().startswith(("+proj=", "PROJCS[", "GEOGCS[")):
                    from pyproj import CRS as _CRS
                    src = _CRS.from_user_input(source_crs)
                    transformer = Transformer.from_crs(src, target_crs, always_xy=True)

                # Known VN-2000 EPSG code → override with TOWGS84-correct PROJ4
                elif crs_upper in _VN2000_PROJ4 and _VN2000_PROJ4[crs_upper]:
                    transformer = Transformer.from_proj(
                        _VN2000_PROJ4[crs_upper], target_crs, always_xy=True
                    )
                    log.info("vn2000_towgs84_applied", epsg=crs_upper)

                else:
                    transformer = Transformer.from_crs(
                        source_crs, target_crs, always_xy=True
                    )

                x, y = transformer.transform(x, y)
                log.info("reprojection_done", from_crs=source_crs, to_crs=target_crs)
            except Exception as exc:
                log.error("reprojection_failed", error=str(exc))

        # --- Write output LAS/LAZ ---
        # CRITICAL: set scale 1e-7° (~1cm horizontal) so WGS-84 degree values
        # are stored with full precision. Default laspy scale 0.01° ≈ 1km —
        # catastrophically coarse for small areas (1km corridor → lat collapses
        # to single integer value → lat_range = 0 → pnts shows thin vertical line).
        out_header = laspy.LasHeader(
            version=las.header.version,
            point_format=las.header.point_format,
        )
        out_header.offsets = np.array([float(x.mean()), float(y.mean()), float(z.mean())])
        out_header.scales  = np.array([1e-7, 1e-7, 1e-3])  # ~1cm horizontal, 1mm vertical
        out_las = laspy.LasData(header=out_header)
        out_las.x = x
        out_las.y = y
        out_las.z = z

        # Copy all extra point dimensions (RGB, intensity, etc.)
        for dim_name in las.point_format.dimension_names:
            if dim_name.lower() in ("x", "y", "z"):
                continue
            try:
                out_las[dim_name] = np.asarray(las[dim_name])[mask]
            except Exception:
                pass

        output_path.parent.mkdir(parents=True, exist_ok=True)
        out_las.write(str(output_path))
        log.info("las_written", path=str(output_path), size_mb=output_path.stat().st_size / 1e6)

    # ── Step 6: Metadata ─────────────────────────────────────────────────────

    def _extract_metadata(
        self, processed_path: Path, source_crs: Optional[str]
    ) -> PointCloudMetadata:
        meta = PointCloudMetadata(source_crs=source_crs)
        try:
            with laspy.open(str(processed_path)) as reader:
                las = reader.read()

            meta.las_version = f"{las.header.version.major}.{las.header.version.minor}"
            meta.point_count = len(las.points)
            meta.has_rgb = "red" in las.point_format.dimension_names
            meta.has_intensity = "intensity" in las.point_format.dimension_names

            x = np.asarray(las.x, dtype=np.float64)
            y = np.asarray(las.y, dtype=np.float64)
            z = np.asarray(las.z, dtype=np.float64)

            meta.bbox_3d = {
                "xmin": float(x.min()), "xmax": float(x.max()),
                "ymin": float(y.min()), "ymax": float(y.max()),
                "zmin": float(z.min()), "zmax": float(z.max()),
            }
            meta.center_lon = float((x.min() + x.max()) / 2)
            meta.center_lat = float((y.min() + y.max()) / 2)
            meta.center_elevation = float(z.mean())
            meta.elevation_min = float(z.min())
            meta.elevation_max = float(z.max())

            # Estimate pts/m² from geographic coords
            dx_deg = meta.bbox_3d["xmax"] - meta.bbox_3d["xmin"]
            dy_deg = meta.bbox_3d["ymax"] - meta.bbox_3d["ymin"]
            lat_rad = math.radians(meta.center_lat)
            area_m2 = (dx_deg * 111_000 * math.cos(lat_rad)) * (dy_deg * 111_000)
            if area_m2 > 0:
                meta.density_pts_per_m2 = meta.point_count / area_m2

        except Exception as exc:
            log.error("metadata_extraction_failed", error=str(exc))

        return meta

    # ── Step 7: 3D Tiles LOD generation via py3dtiles ────────────────────────

    def _run_py3dtiles(self, processed_path: Path, potree_out: Path) -> int:
        """
        Generate Cesium 3D Tiles with octree LOD using py3dtiles.

        Output: tileset.json + points/r*.pnts (8-way octree levels).
        Cesium auto-loads only the tiles visible at current zoom — provides
        the smooth zooming experience of desktop point cloud viewers.

        Performance: ~30-90 seconds for 8M points on 4 cores.
        """
        from py3dtiles.convert import convert
        from pyproj import CRS as _CRS

        # Clean previous output (py3dtiles requires empty directory or overwrite)
        if potree_out.exists():
            shutil.rmtree(potree_out)
        potree_out.mkdir(parents=True, exist_ok=True)

        # Downsample if huge — py3dtiles single-thread on >5M points takes >5min.
        # 2M points still gives ~300 pts/m² density (more than enough for viz).
        input_for_tiles = processed_path
        try:
            with laspy.open(str(processed_path)) as f:
                n_pts = f.header.point_count

            if n_pts > 2_000_000:
                log.info("downsampling_for_tiles", from_pts=n_pts, to_pts=2_000_000)
                with laspy.open(str(processed_path)) as f:
                    las = f.read()

                idx = np.random.default_rng(42).choice(n_pts, 2_000_000, replace=False)
                idx.sort()

                ds_header = laspy.LasHeader(
                    version=las.header.version,
                    point_format=las.header.point_format,
                )
                ds_header.offsets = las.header.offsets
                ds_header.scales  = las.header.scales
                ds_las = laspy.LasData(header=ds_header)
                ds_las.x = np.asarray(las.x)[idx]
                ds_las.y = np.asarray(las.y)[idx]
                ds_las.z = np.asarray(las.z)[idx]
                for dim in las.point_format.dimension_names:
                    if dim.lower() in ("x", "y", "z"):
                        continue
                    try:
                        ds_las[dim] = np.asarray(las[dim])[idx]
                    except Exception:
                        pass

                downsampled = self.output_dir / "downsampled.laz"
                ds_las.write(str(downsampled))
                input_for_tiles = downsampled
                log.info("downsampled_for_tiles",
                         size_mb=round(downsampled.stat().st_size / 1e6, 1))
        except Exception as exc:
            log.warning("downsample_skipped", error=str(exc))

        log.info("py3dtiles_start", path=str(input_for_tiles), out=str(potree_out))

        # Input is already in EPSG:4326 (reprojected by pdal pipeline)
        # Output must be ECEF (EPSG:4978) for Cesium geographic placement
        # With Celery --pool=solo, the worker is the main process (not daemon),
        # so py3dtiles CAN spawn child processes. Use multiprocessing for speed.
        # Fallback to single-threaded if process pool fails.
        import os
        n_cores = min(os.cpu_count() or 2, 4)
        try:
            convert(
                files=[str(input_for_tiles)],
                outfolder=str(potree_out),
                overwrite=True,
                jobs=n_cores,
                use_process_pool=True,
                cache_size=400,
                crs_in=_CRS.from_epsg(4326),
                crs_out=_CRS.from_epsg(4978),
                pyproj_always_xy=True,
                rgb=True,
                verbose=True,   # print tile generation progress
            )
        except AssertionError as exc:
            if "daemonic" in str(exc):
                log.warning("daemon_fallback_to_single_thread")
                convert(
                    files=[str(input_for_tiles)],
                    outfolder=str(potree_out),
                    overwrite=True,
                    jobs=1,
                    use_process_pool=False,
                    cache_size=800,
                    crs_in=_CRS.from_epsg(4326),
                    crs_out=_CRS.from_epsg(4978),
                    pyproj_always_xy=True,
                    rgb=True,
                    verbose=True,
                )
            else:
                raise

        # Count generated tiles (each .pnts = one octree node)
        tiles = list(potree_out.rglob("*.pnts"))
        # Estimate LOD levels from deepest tile name (e.g. r012.pnts = 3 levels deep)
        max_depth = 0
        for t in tiles:
            name = t.stem  # "r", "r0", "r12", "r034", etc.
            if name.startswith("r"):
                max_depth = max(max_depth, len(name) - 1)
        lod_levels = max_depth + 1
        log.info("py3dtiles_done", n_tiles=len(tiles), lod_levels=lod_levels)
        return lod_levels

    # ── Full Async Pipeline ──────────────────────────────────────────────────

    async def run(self) -> PointCloudMetadata:
        loop = asyncio.get_event_loop()

        self._emit(2.0, "Detecting CRS")
        if self.source_crs_override:
            source_crs = self.source_crs_override
            log.info("crs_override_used", crs=source_crs)
        else:
            source_crs = await loop.run_in_executor(None, self.detect_crs)

        self._emit(5.0, "Preparing workspace")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        reprojected_path = self.output_dir / "reprojected.laz"

        self._emit(10.0, "Filtering noise + reprojecting")
        try:
            await loop.run_in_executor(
                None, self._process_points, reprojected_path, source_crs
            )
        except Exception as exc:
            log.warning("processing_fallback", error=str(exc))
            shutil.copy2(str(self.input_path), str(reprojected_path))

        self._emit(70.0, "Extracting metadata")
        metadata = await loop.run_in_executor(
            None, self._extract_metadata, reprojected_path, source_crs
        )

        self._emit(80.0, "Generating multi-LOD 3D Tiles")
        ok = await loop.run_in_executor(
            None, generate_multi_lod_tileset, reprojected_path, self.output_dir
        )
        if ok:
            # Count actual generated levels (r=L0, r0..r7=L1, r0X..=L2)
            potree_dir = self.output_dir / "potree"
            tiles = list(potree_dir.glob("*.pnts"))
            metadata.lod_levels = max(len(t.stem) for t in tiles)
            log.info("multi_lod_used", n_tiles=len(tiles))
        else:
            # Final fallback: single .pnts tile
            self._emit(90.0, "Generating single-tile pnts (fallback)")
            ok = await loop.run_in_executor(
                None, generate_pnts_tileset, reprojected_path, self.output_dir
            )
            metadata.lod_levels = 1 if ok else 0

        metadata.output_crs = "EPSG:4326"
        self._emit(100.0, "Done")
        log.info("pipeline_complete",
                 points=metadata.point_count, lod=metadata.lod_levels,
                 density=round(metadata.density_pts_per_m2, 2))
        return metadata
