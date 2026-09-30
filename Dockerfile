FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('WEB_PORT', '8090') + '/healthz')"]

# Runs as root by default so it can chown new ebooks to match the audiobook's owner.
# Set `user: "PUID:PGID"` in compose instead if your library is owned by a single user.
ENTRYPOINT ["abs-ebook-filler"]
CMD ["web"]
