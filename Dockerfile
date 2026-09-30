FROM node:22-bookworm-slim AS frontend
WORKDIR /app
RUN npm install -g pnpm@11.19.0
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY tsconfig.json vite.config.ts dev.config.mjs dev.config.d.mts main.tsx work.tsx api.ts styles.css ./
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
# Build collection never connects to a database and never uses production secrets,
# so the secret and the database URL are placeholders.
#
# It must run as production: settings.py only selects the hashed manifest storage
# under APP_ENV=production, and verify_static reads staticfiles.json, so under the
# local storage backend it would pass without ever proving the manifest is built.
# TEST_SQLITE is deliberately absent because settings.py refuses SQLite in
# production; nothing here opens a connection.
#
# The environment is set on the command rather than with ENV, so the placeholder
# secret is not baked into the image: the container at runtime must not start in
# production mode holding a secret that is printed in this file.
#
# ALLOWED_HOSTS and CSRF_TRUSTED_ORIGINS are mandatory in production and are
# placeholders on the reserved .invalid TLD (RFC 2606), which cannot resolve to a
# real host. Keep this list in step with the production requirements in
# settings.py; ledger.test_production_config asserts it stays complete.
RUN APP_ENV=production \
    DJANGO_SECRET_KEY=ci-only-not-a-real-secret-0123456789-abcdefghijklmnopqrstuvwxyz \
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
    ALLOWED_HOSTS=ci-only.invalid \
    CSRF_TRUSTED_ORIGINS=https://ci-only.invalid \
    sh -c 'python manage.py collectstatic --noinput --verbosity 2 \
           && python manage.py verify_static' \
    && rm -f .local-secret \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["python", "serve.py"]
