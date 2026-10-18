#!/bin/sh
# Shared by the runtime image and CI's pgvector PostgreSQL service.
set -eu
apt-get update
apt-get install -y --no-install-recommends postgresql-16-partman=5.5.0-1.pgdg12+1
rm -rf /var/lib/apt/lists/*
