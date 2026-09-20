FROM python:3.11-slim

# Runtime libs only — bluetoothd (BlueZ) runs on the HOST; the container
# talks to it over the system D-Bus socket mounted in docker-compose.yml.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        dbus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir .

# Non-interactive shell needs a TTY: run with `docker compose run --rm surplife`
ENTRYPOINT ["surplife"]
CMD ["--help"]