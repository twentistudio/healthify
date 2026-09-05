FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    postgresql-client \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements from backend subdirectory
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/ /app/

# Copy training folder (now accessible from root)
COPY training/ /app/training/

# Verify training directory exists (debugging)
RUN echo "=== Verifying directories ===" && \
    ls -la /app/ && \
    echo "=== Training directory ===" && \
    ls -la /app/training/ && \
    echo "=== Scripts directory ===" && \
    ls -la /app/training/scripts/ || echo "⚠️ Scripts directory not found"

# Create necessary directories
RUN mkdir -p /app/staticfiles /app/media /app/logs

# Collect static files (SECRET_KEY not needed - settings.py handles this)
RUN DJANGO_SECRET_KEY=temp-build-key python manage.py collectstatic --noinput || echo "⚠️ collectstatic failed"

EXPOSE $PORT

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/health/ || exit 1

# Start command - NO makemigrations in production!
CMD python manage.py migrate --noinput && \
    python manage.py createcachetable && \
    gunicorn backend_project.wsgi:application -c gunicorn_conf.py