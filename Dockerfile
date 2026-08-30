# MeshCore Gateway — container image
#
# Lokaal bouwen:  docker build -t meshcore-gateway .
# Draaien:        zie docker-compose.yml (dev) of portainer-stack.yml (prod)
#
# CI bouwt dit image en pusht 'm naar GHCR — zie .github/workflows/ci.yml.

FROM python:3.12-slim

# OCI-labels: vullen de "Package"-pagina op GHCR en koppelen image → repo.
# org.opencontainers.image.source is wat GHCR gebruikt om het package aan de
# repo te hangen (en de zichtbaarheid/README over te nemen).
LABEL org.opencontainers.image.title="MeshCore Gateway Web Client" \
      org.opencontainers.image.description="Web/CLI gateway voor een via USB aangesloten MeshCore companion-radio" \
      org.opencontainers.image.licenses="proprietary" \
      org.opencontainers.image.vendor="Flight 815 B.V."

# Systeem-deps: alleen tini voor proper signal handling. Geen build-deps nodig:
# alle Python-wheels zijn pure-Python of hebben prebuilt binary wheels.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# Niet-root user. UID/GID 1000 = eerste user op een Pi/Debian-host.
# LET OP: 'useradd -g 1000' faalt als groep 1000 niet bestaat — in de slim-base
# bestaat die niet, dus we maken 'm eerst expliciet aan. (Dit was een latente
# build-breker in de vorige Dockerfile.)
# Groep 'dialout' (GID 20) bestaat al in de Debian-base en is de groep waar
# /dev/ttyACM* / /dev/ttyUSB* op de meeste hosts van is. Zie ook de
# 'group_add'-noot in portainer-stack.yml voor hosts met een andere GID.
RUN groupadd -g 1000 app \
 && useradd -m -u 1000 -g 1000 -G dialout app

WORKDIR /app

# Eerst alleen requirements → betere layer-caching bij code-wijzigingen.
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# App-bestanden
COPY gateway.py web.py bot.py db.py ./
COPY templates/ ./templates/
COPY static/ ./static/

# Persistente DB-locatie
RUN mkdir -p /data && chown -R app:app /data /app

ENV MESHCORE_DB=/data/meshcore.db \
    MESHCORE_WEB_HOST=0.0.0.0 \
    MESHCORE_WEB_PORT=8080 \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 8080
VOLUME ["/data"]

# /healthz is bewust unauthenticated en raakt de DB niet — goedkope liveness-check.
# Geen curl/wget in de slim-image, dus via urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os,sys,urllib.request; \
u='http://127.0.0.1:%s/healthz' % os.environ.get('MESHCORE_WEB_PORT','8080'); \
sys.exit(0 if urllib.request.urlopen(u, timeout=3).status == 200 else 1)"

# Tini → nette signal-handling voor docker stop / Ctrl-C
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "gateway.py"]
