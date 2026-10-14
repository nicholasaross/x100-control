# The bridge, for the Jetson (multi-arch: amd64 / arm64).
#   docker compose up -d --build                     local-only, the default
#   EXTRAS=aws in docker-compose.yml                 adds the optional Alexa link (alexa/README.md)
FROM python:3.12-slim
ARG EXTRAS=""
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /src
COPY pyproject.toml README.md ./
COPY x100 ./x100
COPY x100_bridge ./x100_bridge
COPY x100_listener ./x100_listener
RUN pip install --no-cache-dir ".${EXTRAS:+[$EXTRAS]}" && rm -rf /src
# docker-compose.yml runs this as the host user that owns config/ (and certs/ for Alexa).
USER 1000:1000
WORKDIR /config
ENTRYPOINT ["x100-bridge"]
