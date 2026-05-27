# LiDAR3D Web GIS — Makefile
# Usage: make <target>

.PHONY: help up down build logs shell-backend shell-db migrate admin test lint clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Docker ────────────────────────────────────────────────────────────────────
up: ## Start all services
	docker compose up -d

up-build: ## Rebuild and start all services
	docker compose up -d --build

down: ## Stop all services
	docker compose down

down-v: ## Stop and remove volumes (DANGER: data loss)
	docker compose down -v

build: ## Build all images
	docker compose build

logs: ## Tail all logs
	docker compose logs -f

logs-backend: ## Tail backend logs
	docker compose logs -f backend

logs-worker: ## Tail celery worker logs
	docker compose logs -f celery_worker

restart-backend: ## Restart backend only
	docker compose restart backend

restart-worker: ## Restart celery worker
	docker compose restart celery_worker

# ── Database ──────────────────────────────────────────────────────────────────
migrate: ## Run Alembic migrations
	docker compose exec backend alembic upgrade head

migrate-down: ## Rollback last migration
	docker compose exec backend alembic downgrade -1

migrate-history: ## Show migration history
	docker compose exec backend alembic history

shell-db: ## Open psql shell
	docker compose exec postgres psql -U lidar -d lidar3d

# ── Backend ───────────────────────────────────────────────────────────────────
shell-backend: ## Open bash in backend container
	docker compose exec backend bash

admin: ## Create admin user (interactive)
	docker compose exec -it backend python scripts/create_admin.py

test: ## Run backend tests
	docker compose exec backend pytest tests/ -v --tb=short

lint: ## Run linting
	docker compose exec backend ruff check app/
	docker compose exec backend mypy app/ --ignore-missing-imports

# ── Frontend ──────────────────────────────────────────────────────────────────
shell-frontend: ## Open shell in frontend container
	docker compose exec frontend sh

frontend-build: ## Build frontend for production
	cd frontend && npm run build

frontend-dev: ## Start frontend dev server
	cd frontend && npm run dev

# ── Monitoring ────────────────────────────────────────────────────────────────
flower: ## Open Flower task monitor (requires xdg-open)
	xdg-open http://localhost:5555 2>/dev/null || echo "Open http://localhost:5555"

inspect-workers: ## Inspect active Celery workers
	docker compose exec celery_worker celery -A app.workers.celery_app inspect active

purge-queue: ## Purge all pending Celery tasks (DANGER)
	docker compose exec celery_worker celery -A app.workers.celery_app purge -f

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean-uploads: ## Remove temp upload chunks older than 24h
	find ./uploads/temp -type f -mtime +1 -delete 2>/dev/null || true

clean-docker: ## Remove dangling Docker images
	docker image prune -f

ps: ## Show running containers
	docker compose ps

health: ## Check service health
	@curl -sf http://localhost:8000/health | jq . || echo "Backend not healthy"
	@curl -sf http://localhost:3000 > /dev/null && echo "Frontend OK" || echo "Frontend not healthy"
