FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Cloud Run injects $PORT; gunicorn binds to it.
# Single worker + 8 threads suits a I/O-bound pipeline on one instance.
# Timeout 540s gives the daily run plenty of headroom.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 540 main:app
