# One image for both services; docker-compose.yml picks which module each one runs.
FROM python:3.12-slim

WORKDIR /app
COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY config config
COPY shared shared
COPY simulator simulator
COPY hmi hmi

# Created here and owned by the app user, so a new named volume mounted on it is writable.
RUN useradd --create-home app && mkdir data && chown app data
USER app

ENV PYTHONUNBUFFERED=1
