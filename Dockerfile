FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RADAR_DB_PATH=/data/radar.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行；数据目录交给同一个 uid，挂 PVC 时不会权限打架
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin radar \
 && mkdir -p /data \
 && chown -R radar:radar /data /app
USER radar

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"

CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8000"]
