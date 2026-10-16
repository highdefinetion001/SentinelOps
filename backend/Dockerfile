FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY config/baseline.json config/policies.yaml config/servers.json config/server-templates.json ./config/
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
