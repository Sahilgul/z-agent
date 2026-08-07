# Backend image. Build context = REPO ROOT:
#   docker build -f backend/Dockerfile -t collegium-backend:0.1.0 .
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY packages/contracts /app/packages/contracts
COPY backend /app/backend

RUN pip install --no-cache-dir /app/packages/contracts \
 && pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic>=2.9,<3" \
      "pydantic-settings>=2.5" "sqlalchemy>=2.0" "alembic>=1.13" "httpx>=0.27" \
      "redis>=5,<7" "structlog>=24" "bcrypt>=4.2" "pyjwt>=2.9" \
      "apscheduler>=3.10,<4" "python-dotenv>=1.0" "docker>=7" \
      "fakeredis>=2.26" "psycopg[binary]>=3.2"

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
