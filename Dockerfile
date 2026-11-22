FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --system --uid 1000 --home-dir /app agent \
    && mkdir -p /data /config \
    && chown agent /data /config

USER agent
VOLUME ["/data", "/config"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)"

CMD ["tvdb-agent"]
