FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FPL_WEB_HOST=0.0.0.0 \
    PORT=8000 \
    FPL_EXPOSE_API_DOCS=false \
    FPL_ALLOWED_RELEASE_HEALTH=shadow,production

WORKDIR /app

RUN groupadd --system fpl && useradd --system --gid fpl --home-dir /app fpl

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install ".[web]"

COPY api ./api
COPY scripts/__init__.py scripts/run_web_app.py ./scripts/
COPY web ./web

USER fpl
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import json,os,urllib.request; port=os.getenv('FPL_WEB_PORT',os.getenv('PORT','8000')); data=json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/ready', timeout=4)); assert data.get('ready') is True"]

CMD ["python", "scripts/run_web_app.py"]
