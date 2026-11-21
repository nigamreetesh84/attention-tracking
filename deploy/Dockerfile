# Server image, used by the app and worker services. Build context: repo root.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# WeasyPrint (PDF reports) needs Pango at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY shared ./shared
COPY server ./server
RUN pip install ".[server]"

RUN useradd --system --uid 10001 --no-create-home attentionguard
USER attentionguard

EXPOSE 8000
CMD ["python", "-m", "ag_server"]
