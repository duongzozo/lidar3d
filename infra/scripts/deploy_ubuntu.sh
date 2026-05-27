#!/usr/bin/env bash
# =============================================================================
# LiDAR3D Web GIS — Ubuntu 22.04 LTS Production Deploy Script
# =============================================================================
# Usage:
#   chmod +x deploy_ubuntu.sh
#   sudo ./deploy_ubuntu.sh
# =============================================================================
set -euo pipefail
DEPLOY_DIR="/opt/lidar3d"
DOMAIN="${DOMAIN:-lidar3d.example.com}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@example.com}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root: sudo $0"

# ──────────────────────────────────────────────────────────────────────────────
# 1. System packages
# ──────────────────────────────────────────────────────────────────────────────
info "Installing system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
  curl wget git build-essential ca-certificates gnupg lsb-release \
  ufw fail2ban htop unzip jq

# Docker
if ! command -v docker &>/dev/null; then
  info "Installing Docker..."
  curl -fsSL https://get.docker.com | bash
  usermod -aG docker "$SUDO_USER" 2>/dev/null || true
fi

# Docker Compose v2
if ! docker compose version &>/dev/null; then
  info "Installing Docker Compose v2..."
  COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | jq -r '.tag_name')
  curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
    -o /usr/local/bin/docker-compose
  chmod +x /usr/local/bin/docker-compose
fi

# ──────────────────────────────────────────────────────────────────────────────
# 2. Firewall
# ──────────────────────────────────────────────────────────────────────────────
info "Configuring UFW firewall..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ──────────────────────────────────────────────────────────────────────────────
# 3. Deploy directory
# ──────────────────────────────────────────────────────────────────────────────
info "Setting up deploy directory: $DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"/{uploads,outputs,postgres_data,redis_data,logs}
chmod 755 "$DEPLOY_DIR"

# Copy project (assumes script is run from repo root)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cp -r "$PROJECT_ROOT"/* "$DEPLOY_DIR/" || warn "Manual copy needed: cp -r . $DEPLOY_DIR/"

# ──────────────────────────────────────────────────────────────────────────────
# 4. Environment file
# ──────────────────────────────────────────────────────────────────────────────
info "Generating .env..."
if [[ ! -f "$DEPLOY_DIR/.env" ]]; then
  SECRET_KEY=$(openssl rand -hex 32)
  DB_PASS=$(openssl rand -hex 16)
  cat > "$DEPLOY_DIR/.env" <<EOF
# === DATABASE ===
POSTGRES_DB=lidar3d
POSTGRES_USER=lidar
POSTGRES_PASSWORD=${DB_PASS}
DATABASE_URL=postgresql+asyncpg://lidar:${DB_PASS}@postgres:5432/lidar3d

# === REDIS ===
REDIS_URL=redis://redis:6379/0

# === JWT ===
JWT_SECRET_KEY=${SECRET_KEY}
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60
REFRESH_TOKEN_EXPIRE_DAYS=7

# === STORAGE ===
UPLOAD_DIR=/data/uploads
OUTPUT_DIR=/data/outputs
MAX_UPLOAD_SIZE_GB=10
CHUNK_SIZE_MB=64

# === PDAL / POTREE ===
PDAL_THREADS=4
POTREE_CONVERTER_PATH=/usr/local/bin/PotreeConverter

# === APP ===
ENVIRONMENT=production
LOG_LEVEL=INFO
CORS_ORIGINS=["https://${DOMAIN}"]

# === CESIUM ION ===
# Get your token at https://cesium.com/ion/tokens
VITE_CESIUM_ION_TOKEN=YOUR_TOKEN_HERE
VITE_API_BASE_URL=https://${DOMAIN}/api/v1
EOF
  info ".env created at $DEPLOY_DIR/.env"
  warn "Edit $DEPLOY_DIR/.env and set VITE_CESIUM_ION_TOKEN!"
else
  warn ".env already exists — skipping generation"
fi

# ──────────────────────────────────────────────────────────────────────────────
# 5. Nginx + TLS (Certbot) — optional
# ──────────────────────────────────────────────────────────────────────────────
setup_tls() {
  info "Setting up Nginx + Certbot TLS..."
  apt-get install -y nginx certbot python3-certbot-nginx
  
  cat > /etc/nginx/sites-available/lidar3d <<NGINX
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;

    client_max_body_size 10G;
    client_body_timeout 300s;

    # WebSocket
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_read_timeout 86400;
    }

    # API
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
    }

    # 3D Tiles (cache)
    location /tiles/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_cache_valid 200 1d;
        add_header Cache-Control "public, max-age=86400";
    }

    # Frontend SPA
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host \$host;
    }
}
NGINX

  ln -sf /etc/nginx/sites-available/lidar3d /etc/nginx/sites-enabled/
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx

  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$ADMIN_EMAIL" || \
    warn "Certbot failed — check DNS for ${DOMAIN}"
}

read -r -p "Set up Nginx + TLS with Certbot? [y/N] " setup_tls_input
[[ "${setup_tls_input,,}" == "y" ]] && setup_tls

# ──────────────────────────────────────────────────────────────────────────────
# 6. Systemd service for Docker Compose
# ──────────────────────────────────────────────────────────────────────────────
info "Creating systemd service..."
cat > /etc/systemd/system/lidar3d.service <<SERVICE
[Unit]
Description=LiDAR3D Web GIS Platform
Requires=docker.service
After=docker.service network.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${DEPLOY_DIR}
ExecStart=/usr/local/bin/docker-compose up -d --build
ExecStop=/usr/local/bin/docker-compose down
TimeoutStartSec=300
Restart=on-failure

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable lidar3d

# ──────────────────────────────────────────────────────────────────────────────
# 7. Log rotation
# ──────────────────────────────────────────────────────────────────────────────
cat > /etc/logrotate.d/lidar3d <<LR
${DEPLOY_DIR}/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    sharedscripts
}
LR

# ──────────────────────────────────────────────────────────────────────────────
# 8. Start
# ──────────────────────────────────────────────────────────────────────────────
info "Starting LiDAR3D stack..."
cd "$DEPLOY_DIR"
docker compose up -d --build

info "Waiting for services to be healthy..."
sleep 15

info "Running database migrations..."
docker compose exec -T backend alembic upgrade head || warn "Migration failed — may need manual run"

info "Creating admin user..."
docker compose exec -T backend python - <<PYEOF
import asyncio, os, sys
sys.path.insert(0, '/app')
os.environ.setdefault('DATABASE_URL', 'postgresql+asyncpg://lidar:pass@postgres:5432/lidar3d')

async def create_admin():
    from app.db.session import AsyncSessionLocal
    from app.models.models import User, UserRole
    from app.core.security import get_password_hash
    import uuid
    async with AsyncSessionLocal() as db:
        admin = User(
            id=uuid.uuid4(),
            email="${ADMIN_EMAIL}",
            username="admin",
            hashed_password=get_password_hash("changeme123!"),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin)
        await db.commit()
        print("Admin user created: ${ADMIN_EMAIL} / changeme123!")

asyncio.run(create_admin())
PYEOF

# ──────────────────────────────────────────────────────────────────────────────
echo ""
info "═══════════════════════════════════════════════════"
info " LiDAR3D deployed successfully!"
info "═══════════════════════════════════════════════════"
info " Frontend : http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP')"
info " API docs : http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP')/api/v1/docs"
info " Flower   : http://$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP'):5555"
info " Admin    : ${ADMIN_EMAIL} / changeme123!"
warn " IMPORTANT: Change admin password immediately!"
warn " IMPORTANT: Set VITE_CESIUM_ION_TOKEN in $DEPLOY_DIR/.env"
info "═══════════════════════════════════════════════════"
