# LiDAR3D — 3D Web GIS Platform

> Production-ready point cloud processing and visualization system built with **FastAPI**, **CesiumJS**, **Potree**, and **PDAL**.

---

## Features

| Category | Details |
|---|---|
| **Upload** | Chunked upload (4 GB+), concurrent chunks, SHA-256 integrity, session recovery |
| **Processing** | PDAL pipeline — noise filter → reproject EPSG:4326 → LOD octree via PotreeConverter |
| **Visualization** | CesiumJS + Potree 3D Tiles — satellite imagery, terrain, elevation color, 60 FPS |
| **Tools** | Distance & area measurement, fly-to camera, layer toggle, lazy loading |
| **Auth** | JWT (access + refresh tokens), role-based (admin / operator / viewer) |
| **Realtime** | WebSocket progress (Redis pub/sub) during upload & processing |
| **Scalability** | Celery workers (Redis queue), async FastAPI, PostGIS spatial index |

---

## Stack

```
Browser
  └─► Nginx (reverse proxy)
        ├─► React + Vite (frontend)   :3000
        ├─► FastAPI (backend)          :8000
        │     ├─► PostgreSQL + PostGIS :5432
        │     ├─► Redis                :6379
        │     └─► Celery Worker
        │           └─► PDAL + PotreeConverter
        └─► Flower (Celery monitor)   :5555
```

---

## Quick Start

### Prerequisites
- Docker + Docker Compose v2
- Cesium Ion token — free at [cesium.com/ion](https://cesium.com/ion/tokens)

### 1. Clone & configure

```bash
git clone <repo>
cd lidar3d
cp .env.example .env
# Edit .env:
#   VITE_CESIUM_ION_TOKEN=<your token>
#   POSTGRES_PASSWORD=<strong password>
#   JWT_SECRET_KEY=<32+ random chars>
```

### 2. Start

```bash
make up-build
# or
docker compose up -d --build
```

### 3. Run migrations

```bash
make migrate
```

### 4. Create admin user

```bash
make admin
```

### 5. Open

| URL | Service |
|---|---|
| http://localhost | Frontend |
| http://localhost/api/v1/docs | API Swagger |
| http://localhost:5555 | Flower (Celery monitor) |

---

## Project Structure

```
lidar3d/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI routers
│   │   │   ├── auth.py       # /auth — login, register, refresh
│   │   │   ├── datasets.py   # /datasets — CRUD, list, metadata
│   │   │   ├── upload.py     # /upload — chunked upload, complete
│   │   │   └── websocket.py  # /ws/progress/{id} — realtime
│   │   ├── core/
│   │   │   ├── config.py     # Pydantic Settings
│   │   │   └── security.py   # JWT, bcrypt, RBAC
│   │   ├── db/
│   │   │   └── session.py    # Async SQLAlchemy engine
│   │   ├── models/
│   │   │   └── models.py     # ORM: User, Dataset, UploadSession, ProcessingJob
│   │   ├── services/
│   │   │   └── pdal_service.py  # PDAL pipeline + PotreeConverter
│   │   ├── workers/
│   │   │   ├── celery_app.py    # Celery config
│   │   │   └── tasks.py         # process_lidar_file task
│   │   └── main.py           # FastAPI app factory
│   ├── alembic/              # DB migrations
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── CesiumViewer.tsx    # CesiumJS + Potree viewer
│   │   │   └── ChunkedUploader.tsx # Multi-chunk upload UI
│   │   ├── pages/
│   │   │   └── LoginPage.tsx
│   │   ├── services/
│   │   │   └── api.ts          # Typed API client
│   │   ├── stores/
│   │   │   ├── authStore.ts    # Zustand auth
│   │   │   └── processingStore.ts
│   │   └── App.tsx
│   ├── Dockerfile
│   ├── vite.config.ts
│   └── package.json
├── infra/
│   ├── nginx/nginx.conf
│   └── scripts/
│       ├── deploy_ubuntu.sh
│       └── init_db.sql
├── docs/ARCHITECTURE.md
├── docker-compose.yml
├── Makefile
└── .env.example
```

---

## API Reference

### Authentication

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/auth/register` | Register new user |
| POST | `/api/v1/auth/login` | Get JWT tokens |
| POST | `/api/v1/auth/refresh` | Refresh access token |
| GET  | `/api/v1/auth/me` | Current user profile |

### Datasets

| Method | Path | Description |
|---|---|---|
| GET  | `/api/v1/datasets` | List (paginated, filterable) |
| GET  | `/api/v1/datasets/{id}` | Dataset detail + metadata |
| PATCH | `/api/v1/datasets/{id}` | Update name/visibility |
| DELETE | `/api/v1/datasets/{id}` | Delete dataset + files |
| GET | `/api/v1/datasets/{id}/job-status` | Processing job status |

### Upload

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/upload/init` | Start chunked upload session |
| PUT  | `/api/v1/upload/{id}/chunk?index=N` | Upload a chunk |
| POST | `/api/v1/upload/{id}/complete` | Finalize + trigger processing |
| GET  | `/api/v1/upload/{id}/status` | Upload progress |

### WebSocket

```
WS /ws/progress/{dataset_id}?token=<JWT>
```

Events: `{ "type": "progress", "pct": 45, "step": "Reprojecting CRS" }`

---

## PDAL Pipeline

```
LAS/LAZ file
  │
  ├─ 1. CRS Detection     (laspy VLR headers → EPSG code)
  ├─ 2. Noise Filter      (Statistical outlier: mean_k=12, mult=2.2)
  ├─ 3. Class Filter      (Remove class 7=noise, 18=high_noise)
  ├─ 4. Reproject         (→ EPSG:4326 WGS84)
  └─ 5. PotreeConverter   (LOD octree → 3D Tiles for Cesium)
```

---

## Deployment (Ubuntu 22.04)

```bash
# Automated
cd infra/scripts
sudo DOMAIN=lidar3d.yourdomain.com ADMIN_EMAIL=you@email.com ./deploy_ubuntu.sh

# Manual steps
docker compose up -d --build
docker compose exec backend alembic upgrade head
# See Makefile for admin creation
```

---

## Environment Variables

See `.env.example` for full list. Key variables:

```env
DATABASE_URL=postgresql+asyncpg://lidar:pass@postgres:5432/lidar3d
REDIS_URL=redis://redis:6379/0
JWT_SECRET_KEY=<32+ random chars>
VITE_CESIUM_ION_TOKEN=<token from cesium.com/ion>
MAX_UPLOAD_SIZE_GB=10
PDAL_THREADS=4
```

---

## License

MIT
