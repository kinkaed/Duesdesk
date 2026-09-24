FROM node:22-bookworm-slim AS frontend
WORKDIR /app
RUN npm install -g pnpm@11.19.0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY tsconfig.json vite.config.ts main.tsx work.tsx api.ts styles.css ./
RUN pnpm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIND_HOST=0.0.0.0 PORT=8000
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY backend/ ./
COPY --from=frontend /app/backend/static/app ./static/app
# Build collection never connects to a database and never uses production secrets.
RUN TEST_SQLITE=1 python manage.py collectstatic --noinput \
    && rm -f .local-secret \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "python manage.py deployment_check && python serve.py"]
