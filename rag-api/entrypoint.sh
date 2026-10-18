#!/bin/sh
set -e

echo "Starting Gunicorn..."
exec gunicorn src.main:app -c gunicorn.conf.py
