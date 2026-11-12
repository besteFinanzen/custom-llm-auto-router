FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ROUTER_HOST=0.0.0.0 \
    PORT=4141 \
    ROUTER_STATE_DIR=/data

WORKDIR /app
RUN useradd --uid 10001 --create-home router && mkdir /data && chown router:router /data
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY --chown=router:router *.py *.html *.yaml ./

USER router
VOLUME ["/data"]
EXPOSE 4141
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '4141') + '/healthz', timeout=3)" || exit 1
CMD ["sh", "-c", "exec uvicorn gateway:app --host \"${ROUTER_HOST:-0.0.0.0}\" --port \"${PORT:-4141}\""]
