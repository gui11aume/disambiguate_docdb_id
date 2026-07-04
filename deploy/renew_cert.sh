#!/usr/bin/env bash
# Monthly cert renewal. Certbot only acts when expiry is within 30 days,
# so running this more often is harmless.
set -euo pipefail

COMPOSE="docker compose -f /opt/docdb/docker-compose.yml"

$COMPOSE stop nginx
docker run --rm \
    -p 80:80 \
    -v /etc/letsencrypt:/etc/letsencrypt \
    certbot/certbot renew --quiet
$COMPOSE start nginx
