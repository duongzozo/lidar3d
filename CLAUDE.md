# LiDAR3D Web GIS Platform — Project Context

> Production-ready 3D Web GIS for Vietnamese LiDAR point cloud data, built with FastAPI + CesiumJS + PostgreSQL/PostGIS + Celery/Redis. Generates Modifiable Nested Octree (MNO) LOD tiles to render multi-million-point clouds smoothly in the browser.

---

## 1. Tech Stack

### Backend (`./backend`)
- **Python 3.11** on `python:3.11-slim-bookworm`
- **FastAPI** + Uvicorn (4 workers) — REST API + WebSocket
- **SQLAlchemy 2.x async** + asyncpg — Postgres driver
- **Alembic** — schema migrations
- **Celery** + Redis — async task queue (LiDAR processing)
- **laspy[lazrs]** — pure-Python LAS/LAZ reader (no native laszip needed)
- **pyproj** — coordinate transformations
- **numpy** — point cloud math
- Custom octree LOD generator (see §6) — no PotreeConverter, no py3dtiles

### Frontend (`./frontend`)
- **React 18** + TypeScript + Vite
- **CesiumJS 1.117** — 3D globe rendering
- **TailwindCSS** — styling
- **Zustand** — auth + processing stores
- **React Query** — server state
- **Framer Motion** — animations

### Infrastructure (`./docker-compose.yml`)
- **PostgreSQL 16 + PostGIS** — spatial database
- **Redis 7** — Celery broker + result backend
- **Nginx** — reverse proxy + SPA hosting
- Named Docker volume `lidar_data` mounted at `/data` (shared across backend/celery_worker/beat/nginx)

---

## 2. Project Structure

```
lidar3d/
├── backend/
│   ├── Dockerfile                              # single-stage, no PotreeConverter
│   ├── requirements.txt
│   ├── alembic/                                # migrations
│   │   ├── env.py                              # excludes PostGIS extension tables
│   │   └── versions/
│   │       ├── 0001_initial.py
│   │       └── d2b0afa8e219_add_source_crs_override.py
│   ├── scripts/create_admin.py
│   └── app/
│       ├── main.py                             # FastAPI app, mounts /tiles
│       ├── core/{config.py, security.py}
│       ├── db/session.py                       # AsyncSessionFactory
│       ├── models/models.py                    # User, Dataset, UploadSession, ProcessingJob
│       ├── api/
│       │   ├── auth.py
│       │   ├── datasets.py                     # CRUD + reprocess endpoint
│       │   ├── upload.py                       # chunked upload
│       │   └── websocket.py                    # progress events
│       ├── services/
│       │   ├── pdal_service.py                 # pipeline orchestrator
│       │   ├── pnts_writer.py                  # single-tile fallback
│       │   └── multi_lod_writer.py             # MNO octree LOD (primary)
│       └── workers/
│           ├── celery_app.py
│           └── tasks.py                        # process_lidar_file
├── frontend/
│   ├── Dockerfile, nginx.conf, package.json
│   └── src/
│       ├── App.tsx                             # main layout + dataset list
│       ├── pages/LoginPage.tsx
│       ├── components/
│       │   ├── CesiumViewer.tsx                # 3D map + tileset loader
│       │   └── ChunkedUploader.tsx             # upload with CRS field
│       ├── services/api.ts
│       └── stores/{authStore.ts, processingStore.ts}
├── infra/
│   ├── nginx/nginx.conf                        # /api → backend, /tiles → backend
│   └── scripts/{deploy_ubuntu.sh, init_db.sql}
├── docker-compose.yml
├── Makefile
└── .env.example
```

---

## 3. Database Schema

**User**: id, email, hashed_password, role
**Dataset**: id, owner_id, name, status, **file_path** (path to original LAS on disk), file_size_bytes, file_hash, point_count, source_crs, center_lat, center_lon, elevation_min/max, bbox, **potree_path** (relative dir under OUTPUT_DIR), lod_levels, ...
**UploadSession**: id, dataset_id, filename, total_size_bytes, **source_crs_override** (user-supplied CRS), temp_dir, total_chunks, uploaded_chunks_bitmask (JSONB)
**ProcessingJob**: id, dataset_id, celery_task_id, job_type, status, progress_pct, current_step

**Important**: `ProcessingJob.dataset_id` FK does **not** have `ON DELETE CASCADE`. The delete endpoint must explicitly remove ProcessingJob records before deleting the Dataset.

**Credentials (default)**:
- DB user: `lidar3d` (NOT `lidar`)
- DB name: `lidar3d`
- DB password: `lidar3d_secret` (override via `POSTGRES_PASSWORD` env)
- Admin user (creation script): script at `backend/scripts/create_admin.py`

---

## 4. Processing Pipeline (`pdal_service.py`)

```
upload  →  /data/output/{dataset_id}/{filename}.las   (file_path stored in DB)
            │
            ▼
   pct=2   Detect CRS
            │ from VLR headers, then heuristic
            │ (Easting 400k-700k & Northing 900k-2600k → VN-2000)
            │ or override from upload form
            ▼
   pct=5   Prepare workspace
            ▼
   pct=10  Filter noise (classification=7) + reproject to EPSG:4326
            │ writes /data/output/{id}/reprojected.laz
            │ uses scale=1e-7° (~1cm) — CRITICAL for precision
            ▼
   pct=70  Extract metadata (bbox, density, point count)
            ▼
   pct=80  Generate multi-LOD 3D Tiles (multi_lod_writer)
            │ writes /data/output/{id}/potree/{r,r0..r7,r00..,...}.pnts
            │ writes /data/output/{id}/potree/tileset.json
            │ fallback: single-tile pnts_writer if multi_lod fails
            ▼
   pct=100 Done
```

---

## 5. Coordinate System Handling — VN-2000

### The 710m offset bug (critical)
PRJ files from Vietnamese LiDAR survey software contain a 7-parameter Helmert transformation in `TOWGS84[...]` that pyproj's `EPSG:3405` lookup ignores. Using just the EPSG code produces a **~710m offset** from the true location.

**Fix** (`pdal_service.py`):
```python
_VN2000_TOWGS84 = ("-191.90441429,-39.30318279,-111.45032835,"
                   "0.009288360000000001,-0.01975479,0.00427372,0.252906278")
_VN2000_PROJ4 = {
    "EPSG:3299": "+proj=tmerc +lat_0=0 +lon_0=102 +k=0.9999 +x_0=500000 +y_0=0 "
                 f"+ellps=WGS84 +towgs84={_VN2000_TOWGS84} +units=m +no_defs",
    "EPSG:3405": "+proj=tmerc +lat_0=0 +lon_0=105 ...",  # CM 105° (Bắc, Bắc Trung Bộ)
    "EPSG:3406": "+proj=tmerc +lat_0=0 +lon_0=108 ...",  # CM 108° (Nam Trung Bộ, Nam)
}
```

The reprojection code checks the source CRS string and substitutes the PROJ4 string with explicit TOWGS84 whenever an EPSG:34xx code is matched.

### Supported CRS input formats
The upload form's "source CRS" field accepts:
1. **EPSG code**: `EPSG:3405`, `EPSG:3406`, `EPSG:3299` → auto-overridden with TOWGS84-correct PROJ4
2. **PROJ4 string**: `+proj=tmerc ...` → used as-is
3. **WKT (PRJ file content)**: `PROJCS[...]` → parsed via `CRS.from_wkt()` — **most accurate**, recommended when available

### Coordinate precision in reprojected LAS
LAS stores coordinates as integers via `stored = round((value - offset) / scale)`. Default laspy scale `0.01` is fine for projected meters but **catastrophic for WGS-84 degrees**:

For a 1km × 1km area at 21°N:
- lat range ≈ 0.009° → with scale=0.01 → quantized to **0 integer units** → all latitudes collapse to one value → point cloud collapses to a thin vertical line

**Fix**: explicit header in `pdal_service.py`:
```python
out_header.offsets = np.array([float(x.mean()), float(y.mean()), float(z.mean())])
out_header.scales  = np.array([1e-7, 1e-7, 1e-3])  # ~1cm horizontal, 1mm vertical
```

---

## 6. Multi-LOD Octree (`multi_lod_writer.py`)

### Design: Modifiable Nested Octree (MNO)
Same algorithm as PotreeConverter but in pure Python. Each point appears in **exactly one tile** across all levels. With Cesium's `refine: "ADD"`, loading visible tiles cumulates to show all relevant points.

### Algorithm
```
1. Read ALL points from reprojected.laz (no global downsampling)
2. Convert WGS-84 lon/lat/alt → ECEF (Cesium's coordinate frame)
3. Recursive octree build:
   - For each node:
     a. Take up to max_per_tile=300k points randomly → write r{path}.pnts
     b. Remaining points: split into 8 octants by ECEF midpoints
     c. Recurse on non-empty octants until max_depth=5 or octant empty
4. Build tileset.json hierarchy with proper geometricError per node
```

### Output structure
```
potree/
├── tileset.json
├── r.pnts                  ← Level 0: root (overview)
├── r0.pnts ... r7.pnts     ← Level 1: 8 octants
├── r0X.pnts ... r7X.pnts   ← Level 2: sub-octants (only where dense)
└── (deeper if points remain)
```

### Tile naming convention
`r{i}{j}{k}...` where each digit `0-7` indicates the octant at that level (bit pattern: `x_high << 0 | y_high << 1 | z_high << 2`).

### Stats (real measurements)
| Input points | Tiles | Levels | Time | Coverage |
|---|---|---|---|---|
| 1M | 9 | 0+1 | 1.1s | 100% |
| 5M | 35 | 0+1+2 | 6.7s | 100% |
| 8.4M | ~50-70 | 0+1+2+3 | ~10-15s | 100% |

### Geometric error formula
`geom_err = sphere_r / (2 ** depth)` for `depth > 0`, else `sphere_r`. Cesium loads child tiles when their pixel error exceeds `maximumScreenSpaceError=8`, so smaller GE = loads only when zoomed closer.

### Why not PotreeConverter or py3dtiles?
- **PotreeConverter**: C++ build inside Docker fails repeatedly (brotli warnings, build env issues, GitHub deps fragile)
- **py3dtiles**: Pure Python but uses multiprocessing+ZMQ which hangs in Docker (workers spawn but `/dev/shm` IPC fails, "Submit next portion" loops infinitely)
- **Custom MNO**: ~150 lines of NumPy, works everywhere, ~10s for 8M points

---

## 7. Cesium Viewer Configuration (`CesiumViewer.tsx`)

### Critical tileset options for octree LOD
```typescript
const tileset = await Cesium3DTileset.fromUrl(tilesetUrl, {
  maximumScreenSpaceError: 8,         // lower = more detail
  cacheBytes: 1024 * 1024 * 1024,     // 1 GB cache for smooth pan
  maximumCacheOverflowBytes: 512 * 1024 * 1024,
  skipLevelOfDetail: true,            // skip intermediate LODs (faster)
  skipScreenSpaceErrorFactor: 16,
  baseScreenSpaceError: 1024,
  loadSiblings: true,                 // smoother pan/zoom
  dynamicScreenSpaceError: true,
  preloadWhenHidden: true,
});

tileset.pointCloudShading.attenuation = true;
tileset.pointCloudShading.geometricErrorScale = 1.0;
tileset.pointCloudShading.maximumAttenuation = 10;  // max point size in px
tileset.pointCloudShading.eyeDomeLighting = true;   // 3D depth perception
tileset.pointCloudShading.eyeDomeLightingStrength = 1.0;
```

### Imagery fallback (no Cesium Ion token)
- ArcGIS URL `{z}/{y}/{x}` differs from Cesium's default `{z}/{x}/{y}` → can't be used as-is
- OSM tiles `https://tile.openstreetmap.org/{z}/{x}/{y}.png` work without any token
- Logic: use OSM by default; only switch to Esri/Bing if a Cesium Ion token is set

### flyToBoundingSphere
For corridor data (e.g. 200m × 3000m), `bs.radius * 1.2` places camera inside the bounding sphere → only a slice visible. Use `Math.max(bs.radius * 5, 800)` with pitch `-55°` to ensure full overview.

---

## 8. URL Routing

All API calls use **relative URLs** (`VITE_API_URL=""` empty by default). Nginx proxies:
- `/api/v1/*` → backend container
- `/tiles/*`  → backend container (FastAPI StaticFiles mounted from `/data/output`)
- `/`         → frontend container (SPA fallback via named location `@spa_fallback` to avoid redirect loops)

**Common mistake**: hard-coding `http://localhost:8000` in the frontend bundle. Always rebuild after changing `VITE_API_URL`.

---

## 9. Key Files Reference

| File | Purpose |
|---|---|
| `backend/app/services/pdal_service.py` | Pipeline orchestrator. Contains VN-2000 PROJ4 map. Calls multi_lod_writer → falls back to pnts_writer. |
| `backend/app/services/multi_lod_writer.py` | MNO octree LOD generator. Pure Python. Primary tile generator. |
| `backend/app/services/pnts_writer.py` | Single-tile .pnts fallback. Uses `(HEADER_SIZE + json_len) % 8 == 0` for byte alignment. |
| `backend/app/api/datasets.py` | List/get/delete/reprocess endpoints. Smart input file resolution (falls back to reprojected.laz if original deleted). |
| `backend/app/api/upload.py` | Chunked upload with atomic JSON bitmask UPDATE (avoids race condition with concurrent chunks). Saves `file_path`. |
| `frontend/src/components/CesiumViewer.tsx` | Tileset loader with octree LOD options + point cloud shading. |
| `frontend/src/App.tsx` | Dataset list with ↺ Reprocess + 🗑 Delete buttons. CRS input per dataset. |
| `docker-compose.yml` | `--pool=solo` Celery (avoids daemon issues), `shm_size: 2gb` (multiprocessing safety net). |

---

## 10. Lessons Learned (Critical Gotchas)

1. **LAS scale for WGS-84**: Default `0.01` quantizes degrees to ~1km — set `1e-7` explicitly.
2. **VN-2000 needs TOWGS84**: pyproj's `EPSG:3405` ignores datum shift (710m offset) — must use PROJ4 with explicit `+towgs84=...`.
3. **`.pnts` byte alignment**: Header is 28 bytes (28%8=4) so JSON must be padded so `(28+json_len)%8==0`, not `json_len%8==0`.
4. **ECEF for Cesium**: Tile positions must be ECEF (EPSG:4978), not WGS-84 degrees. Convert before writing pnts.
5. **ForeignKey CASCADE**: Without `ondelete="CASCADE"`, deleting a Dataset with ProcessingJob children raises FK violation. Delete children first.
6. **Celery daemon + multiprocessing**: prefork pool runs tasks as daemon processes → can't spawn child processes → use `--pool=solo`.
7. **Docker `/dev/shm`**: Default 64MB is too small for ZMQ/multiprocessing IPC. Set `shm_size: 2gb` if libraries need it.
8. **Alembic + PostGIS**: Autogenerate picks up extension tables (`loader_lookuptables`, etc.) and tries to drop them. Use `include_object` filter in `env.py` to exclude `tiger`/`topology` schemas.
9. **Bind-mount vs image rebuild**: With `./backend:/app`, code changes don't need `docker compose build` — just `restart`. Only `requirements.txt` changes need rebuild.
10. **Original file persistence**: After processing, the original LAS file at `/data/output/{id}/{filename}.las` must NOT be deleted — needed for reprocess. If lost, fall back to `reprojected.laz` (skipping reprojection).

---

## 11. Common Operations

### Initial setup
```bash
docker compose up -d --build
docker compose exec backend alembic upgrade head
docker compose exec -it backend python scripts/create_admin.py
```

### Apply DB migrations manually (when alembic fights PostGIS)
```bash
docker compose exec postgres psql -U lidar3d -d lidar3d -c "
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS file_path TEXT;
ALTER TABLE upload_sessions ADD COLUMN IF NOT EXISTS source_crs_override VARCHAR(200);
"
docker compose exec backend alembic stamp head
```

### Tail logs
```bash
docker compose logs -f celery_worker        # processing progress
docker compose logs -f backend              # API requests
docker compose logs -f --tail=50 backend celery_worker
```

### Re-run pipeline with corrected CRS
Click **↺** icon on dataset → enter `EPSG:3405` (or `EPSG:3406`) → **Run**. Pipeline reprojects + regenerates LOD tiles.

### Verify multi-LOD tiles generated
```bash
docker compose exec backend ls /data/output/<dataset_id>/potree/
# Should show: tileset.json, r.pnts, r0.pnts..r7.pnts, r0X.pnts...
```

### Force rebuild (when image is stale)
```bash
docker compose build --no-cache backend celery_worker
docker compose up -d --force-recreate backend celery_worker
```

---

## 12. Open Items / Future Work

- **Authentication**: currently single-user via JWT, no UI for user management
- **WebSocket reconnect**: progress events stop if connection drops mid-processing
- **Tile pre-computation**: would benefit from generating tiles in parallel chunks (currently serial)
- **Quantized positions**: Cesium supports `POSITION_QUANTIZED` for ~50% smaller pnts files
- **EDL parameters**: eye-dome lighting tuning could improve depth perception
- **COPC support**: Cesium 1.110+ natively supports Cloud Optimized Point Cloud format
- **Streaming uploads**: chunks currently buffered to disk before merge; could stream directly

---

## 13. Performance Profile

| Stage | 8.4M points (test) |
|---|---|
| Upload (LAN, chunked 8MB) | ~5s |
| CRS detection + reproject | ~5s |
| Metadata extraction | ~1s |
| **Multi-LOD octree build** | **~10-15s** |
| Total pipeline | ~25s |
| Initial tile load (browser) | <500ms (r.pnts only) |
| Pan/zoom tile load | streamed, transparent |

---

*Last updated: 2026-05-27 — current production state*
