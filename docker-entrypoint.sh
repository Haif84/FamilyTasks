#!/bin/sh
set -e
# Named volume is often root:root; appuser must own /app/data for SQLite.
mkdir -p /app/data
chown -R appuser:appuser /app/data
exec gosu appuser "$@"
