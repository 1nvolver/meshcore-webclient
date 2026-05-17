# MeshCore Gateway — container image
#
# Build:  docker build -t meshcore-gateway .
# Run:    zie docker-compose.yml of de README

FROM python:3.12-slim

# Systeem-deps: alleen tini voor proper signal handling. We gebruiken geen
# build-deps want alle Python wheels zijn pure-Python of hebben binary wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# Niet-root user voor veiligheid. UID/GID 1000 = standaard pi/eerste user op Pi.
# Lid van dialout (groep voor seriële poorten op Debian-based systemen).
RUN groupadd -g 20 dialout 2>/dev/null || true \
 && useradd -m -u 1000 -g 1000 -G dialout app

WORKDIR /app

# Eerst alleen requirements voor betere layer-caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# App-bestanden
COPY gateway.py web.py bot.py db.py ./

# Persistente DB-locatie
RUN mkdir -p /data && chown -R app:app /data /app
ENV MESHCORE_DB=/data/meshcore.db \
    MESHCORE_WEB_HOST=0.0.0.0 \
    MESHCORE_WEB_PORT=8080 \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 8080
VOLUME ["/data"]

# Tini → nette signal-handling voor Ctrl-C / docker stop
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "gateway.py"]
