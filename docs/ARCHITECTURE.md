# LiDAR3D — Hệ thống Web GIS 3D
## Architecture Guide & Deploy Manual

---

## 📐 Kiến trúc tổng quan

```
┌─────────────────────────────────────────────────────────────────────┐
│                          INTERNET                                    │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                    ┌───────────▼────────────┐
                    │   Nginx Reverse Proxy   │
                    │  (port 80/443)          │
                    └────┬──────────┬────────┘
                         │          │
          ┌──────────────▼──┐   ┌───▼───────────────┐
          │  React Frontend  │   │   FastAPI Backend  │
          │  (CesiumJS +     │   │   (port 8000)      │
          │   Potree)        │   │   4 Uvicorn workers│
          └──────────────────┘   └────┬──────────┬───┘
                                      │          │
                          ┌───────────▼──┐  ┌────▼──────────┐
                          │  PostgreSQL   │  │     Redis      │
                          │  + PostGIS   │  │  (cache/queue) │
                          └──────────────┘  └────────────────┘
                                                    │
                                      ┌─────────────▼─────────────┐
                                      │   Celery Worker(s)         │
                                      │   ┌─────────────────────┐  │
                                      │   │  PDAL Pipeline       │  │
                                      │   │  1. Detect CRS       │  │
                                      │   │  2. Reproject 4326   │  │
                                      │   │  3. Filter noise     │  │
                                      │   │  4. Extract meta     │  │
                                      │   │  5. PotreeConverter  │  │
                                      │   └─────────────────────┘  │
                                      └───────────────────────────-─┘
```

---

## 📁 Cấu trúc thư mục

```
lidar3d/
├── docker-compose.yml
├── .env.example
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py                  ← FastAPI app factory
│       ├── core/
│       │   ├── config.py            ← Pydantic Settings
│       │   └── security.py          ← JWT + Auth dependencies
│       ├── db/
│       │   └── session.py           ← Async SQLAlchemy engine
│       ├── models/
│       │   └── models.py            ← ORM models (PostGIS geometry)
│       ├── api/
│       │   ├── auth.py              ← /auth/login, /register, /me
│       │   ├── datasets.py          ← /datasets CRUD + pagination
│       │   ├── upload.py            ← Chunked upload (4GB+)
│       │   └── websocket.py         ← Real-time progress via WS
│       ├── services/
│       │   └── pdal_service.py      ← PDAL + PotreeConverter pipeline
│       └── workers/
│           ├── celery_app.py        ← Celery config + beat schedule
│           └── tasks.py             ← Background processing tasks
├── frontend/
│   ├── Dockerfile
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── main.tsx                 ← React entry + routing
│       ├── App.tsx                  ← Main layout (sidebar + viewer)
│       ├── index.css
│       ├── components/
│       │   ├── CesiumViewer.tsx     ← 3D viewer (CesiumJS + Potree)
│       │   └── ChunkedUploader.tsx  ← Drag-drop chunked upload
│       ├── pages/
│       │   └── LoginPage.tsx
│       ├── stores/
│       │   ├── authStore.ts         ← Zustand auth (persisted)
│       │   └── processingStore.ts   ← Active processing tracker
│       └── services/
│           └── api.ts               ← Typed API client
└── infra/
    ├── nginx/nginx.conf
    └── scripts/init_db.sql
```

---

## 🗄️ Database Schema

```sql
-- Users
users (id UUID PK, email, username, hashed_password, role, is_active, ...)

-- Datasets (chứa metadata point cloud)
datasets (
  id UUID PK,
  owner_id FK → users,
  name, description, status (pending/uploading/processing/completed/failed),
  original_filename, file_size_bytes, file_hash,
  
  -- PostGIS geometry
  bbox_geom GEOMETRY(POLYGON, 4326),  ← bounding box 2D
  bbox_3d JSONB,                       ← {xmin,ymin,zmin,xmax,ymax,zmax}
  center_lon, center_lat, center_elevation,
  
  -- Point cloud metadata
  point_count BIGINT,
  source_crs VARCHAR,      ← e.g. "EPSG:32648"
  output_crs VARCHAR,      ← "EPSG:4326"
  elevation_min/max FLOAT,
  density_pts_per_m2 FLOAT,
  has_rgb BOOL, has_intensity BOOL,
  las_version VARCHAR,
  
  -- Potree output
  potree_path VARCHAR,     ← relative path for /tiles/ serving
  lod_levels INT,
  
  -- Processing timing
  processing_started_at, processing_completed_at,
  error_message TEXT
)

-- Upload sessions (chunked upload tracking)
upload_sessions (
  id UUID PK,
  user_id FK, dataset_id FK,
  filename, total_size_bytes, chunk_size_bytes,
  total_chunks, uploaded_chunks,
  chunk_bitmask TEXT,       ← JSON array of received chunk indices
  temp_dir VARCHAR,
  expires_at TIMESTAMPTZ,
  is_complete BOOL
)

-- Processing jobs (Celery task tracking)
processing_jobs (
  id UUID PK,
  dataset_id FK,
  celery_task_id VARCHAR,
  status (queued/running/completed/failed/retrying/cancelled),
  job_type VARCHAR,
  progress_pct FLOAT,
  current_step VARCHAR,
  retry_count, max_retries INT,
  error_message, error_traceback TEXT,
  started_at, completed_at TIMESTAMPTZ
)
```

---

## 🔌 API Design

### Authentication
```
POST /api/v1/auth/register    ← tạo tài khoản
POST /api/v1/auth/login       ← nhận access_token + refresh_token
POST /api/v1/auth/refresh     ← làm mới token
GET  /api/v1/auth/me          ← thông tin user hiện tại
```

### Datasets
```
GET    /api/v1/datasets              ← list (pagination, filter, search)
GET    /api/v1/datasets/{id}         ← chi tiết + jobs
PATCH  /api/v1/datasets/{id}         ← cập nhật tên/visibility
DELETE /api/v1/datasets/{id}         ← xóa dataset + files
GET    /api/v1/datasets/{id}/job-status  ← trạng thái job mới nhất
```

### Upload (Chunked)
```
POST /api/v1/upload/init                     ← khởi tạo session
PUT  /api/v1/upload/{session_id}/chunk?index=N  ← upload từng chunk
POST /api/v1/upload/{session_id}/complete    ← merge + trigger processing
GET  /api/v1/upload/{session_id}/status      ← trạng thái upload
```

### WebSocket
```
WS   /ws/progress/{dataset_id}?token=<JWT>   ← real-time progress
```

### Static Tiles
```
GET  /tiles/{dataset_id}/potree/metadata.json   ← Potree metadata
GET  /tiles/{dataset_id}/potree/r/*             ← octree tiles
```

---

## 🚀 Phase 1: MVP - Deploy local

### Prerequisites
```bash
# Ubuntu 22.04
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
```

### Khởi động
```bash
git clone <repo> lidar3d && cd lidar3d

# Copy env
cp .env.example .env
# Chỉnh sửa .env:
#   CESIUM_ION_TOKEN=your_token_from_cesium.com/ion
#   SECRET_KEY=random_long_string
#   JWT_SECRET_KEY=random_long_string

# Build + start all services
docker compose up -d --build

# Tạo admin user
docker compose exec backend python -c "
import asyncio
from app.db.session import AsyncSessionFactory
from app.models.models import User, UserRole
from app.core.security import hash_password

async def create_admin():
    async with AsyncSessionFactory() as db:
        user = User(
            email='admin@lidar3d.vn',
            username='admin',
            hashed_password=hash_password('Admin@123'),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        print('Admin created')

asyncio.run(create_admin())
"

# Truy cập
echo "Frontend: http://localhost"
echo "API docs: http://localhost:8000/api/docs"
echo "Flower:   http://localhost:5555"
```

---

## 🔧 Phase 2: Optimization

### 1. Multi-worker Celery
```yaml
# docker-compose.override.yml
services:
  celery_worker:
    deploy:
      replicas: 3  # 3 worker containers
```

### 2. Redis cache cho tiles
```python
# Trong backend, cache tile responses
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from fastapi_cache.decorator import cache

@router.get("/tiles/{path:path}")
@cache(expire=86400)  # cache 24h
async def serve_tile(path: str):
    ...
```

### 3. Potree 3D Tiles optimization
```bash
# Dùng PotreeConverter 2.x với các flags:
PotreeConverter input.laz \
  -o ./output \
  --output-format BROTLI \    # smaller tiles
  --encoding BROTLI \
  --chunk-method LASZIP \
  --overwrite
```

### 4. Frontend lazy loading
```typescript
// Chỉ load tileset khi layer visible + user vào viewport
const tileset = await Cesium3DTileset.fromUrl(url, {
  preloadWhenHidden: false,      // không preload khi ẩn
  maximumScreenSpaceError: 16,   // thô khi xa
  dynamicScreenSpaceError: true, // tự động LOD
});
```

---

## 🏢 Phase 3: Enterprise Scaling

### Kubernetes deployment
```yaml
# k8s/backend-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: lidar3d-backend
spec:
  replicas: 4
  template:
    spec:
      containers:
      - name: backend
        image: lidar3d-backend:latest
        resources:
          requests: { cpu: "500m", memory: "1Gi" }
          limits:   { cpu: "2000m", memory: "4Gi" }
        env:
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: lidar3d-secrets
              key: database-url
```

### Celery auto-scaling
```bash
# KEDA (Kubernetes Event-Driven Autoscaling) dựa trên Redis queue length
kubectl apply -f k8s/keda-scaledobject.yaml
```

### Object Storage (S3/MinIO)
```python
# Thay file system bằng S3 cho tiles
import boto3
s3 = boto3.client("s3", endpoint_url="http://minio:9000")
s3.upload_file(local_path, "lidar3d-tiles", s3_key)
```

### CDN cho tiles
```nginx
# Cloudflare / CloudFront cache
# Cache-Control: public, max-age=31536000, immutable
# Tiles bất biến sau khi generate → cache mãi mãi
```

---

## 🔍 Monitoring

```bash
# Flower (Celery dashboard)
http://localhost:5555

# Grafana + Prometheus (thêm vào docker-compose)
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d

# Check logs
docker compose logs -f backend
docker compose logs -f celery_worker

# Celery task status
docker compose exec celery_worker celery -A app.workers.celery_app inspect active
docker compose exec celery_worker celery -A app.workers.celery_app inspect stats
```

---

## 📊 Processing Pipeline Detail

```
Input LAS/LAZ (4GB+)
        │
        ▼ Step 1 (2%)
   Detect CRS
   (laspy VLR headers → WKT → EPSG)
   Fallback: PDAL metadata
        │
        ▼ Step 2 (10%)
   Build PDAL Pipeline JSON:
   ├── readers.las (with override_srs)
   ├── filters.outlier (statistical, mean_k=12, mult=2.2)
   ├── filters.range (remove class 7,18 = noise)
   └── filters.reprojection (→ EPSG:4326)
        │
        ▼ Step 3 (15-55%)
   Execute PDAL → reprojected.laz
        │
        ▼ Step 4 (55%)
   Extract metadata with laspy:
   - point_count, bbox_3d
   - elevation_min/max
   - density (pts/m²)
   - has_rgb, has_intensity
        │
        ▼ Step 5 (60-98%)
   PotreeConverter:
   - Generates LOD octree
   - Output: Cesium 3D Tiles (tileset.json + .bin)
   - Served via /tiles/ static endpoint
        │
        ▼ Step 6 (100%)
   Save metadata → PostgreSQL
   Publish progress → Redis pub/sub → WebSocket → Browser
```

---

## 🌐 CesiumJS + Potree Integration

```typescript
// Potree output tương thích Cesium 3D Tiles
// PotreeConverter 2.x tự động generate tileset.json theo Cesium spec

const tileset = await Cesium3DTileset.fromUrl(
  "/tiles/{dataset_id}/potree/tileset.json",
  {
    maximumScreenSpaceError: 8,   // LOD quality (nhỏ = chi tiết hơn)
    maximumMemoryUsage: 512,       // MB GPU memory
    dynamicScreenSpaceError: true, // auto LOD theo distance
  }
);
viewer.scene.primitives.add(tileset);

// Fly to
viewer.camera.flyToBoundingSphere(tileset.boundingSphere, {
  offset: new HeadingPitchRange(0, toRadians(-45), radius * 2)
});
```

---

## 🛡️ Security Checklist

- [x] JWT HS256 với secret key riêng
- [x] bcrypt password hashing
- [x] Role-based access (admin/operator/viewer)
- [x] File type validation (.las/.laz only)
- [x] Upload size limit (10GB default)
- [x] Expired upload session cleanup
- [x] CORS whitelist
- [ ] Rate limiting (thêm slowapi)
- [ ] HTTPS/TLS (thêm Certbot/Let's Encrypt)
- [ ] Audit logging

---

## 📦 Cesium Ion Token

1. Đăng ký tại https://cesium.com/ion/
2. Tạo token tại: Account → Access Tokens → Create Token
3. Thêm vào `.env`: `CESIUM_ION_TOKEN=your_token`
4. Dùng cho: Cesium World Terrain + Bing Maps satellite imagery

---

## 🐞 Troubleshooting

### PDAL không tìm thấy CRS
```python
# Override manually trong PDAL pipeline
reader["override_srs"] = "EPSG:32648"  # VN-2000 / UTM zone 48N
```

### PotreeConverter không có trong Docker
```dockerfile
# Build từ source (trong Dockerfile)
RUN git clone https://github.com/potree/PotreeConverter.git && \
    cd PotreeConverter && mkdir build && cd build && \
    cmake .. && make -j$(nproc)
```

### WebSocket disconnect ngay lập tức
```python
# Kiểm tra JWT token trong query param
ws://localhost:8000/ws/progress/{id}?token=<access_token>
# Không phải refresh_token
```

### Upload chunk thất bại (413)
```nginx
# Trong nginx.conf
client_max_body_size 100M;  # Mỗi chunk tối đa 100MB
```
