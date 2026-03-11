#!/bin/bash
mkdir -p /app/backend/data
python3 /app/backend/seed.py
exec uvicorn backend.app:app --host 0.0.0.0 --port 8892
