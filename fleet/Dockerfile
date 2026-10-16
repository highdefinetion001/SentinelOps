FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY fleet ./fleet
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /var/lib/configguard && chown 10001:10001 /var/lib/configguard
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "fleet.app:app", "--host", "0.0.0.0", "--port", "8080"]
