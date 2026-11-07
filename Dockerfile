# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HOST=0.0.0.0 \
    DATA_DIR=/data

WORKDIR /app

RUN groupadd --system --gid 10001 voxdesk \
 && useradd --system --uid 10001 --gid voxdesk --create-home voxdesk

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY public ./public
COPY run.py pyproject.toml ./

RUN mkdir -p /data && chown -R voxdesk:voxdesk /data /app
USER voxdesk

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/api/health', timeout=4)"

CMD ["python", "run.py"]
