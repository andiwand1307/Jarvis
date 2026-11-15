FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JARVIS_WEB_DIR=/app/web

WORKDIR /app
COPY pyproject.toml ./
COPY jarvis_core ./jarvis_core
RUN pip install --no-cache-dir . && rm -rf /root/.cache

COPY web ./web
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
