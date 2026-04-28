FROM python:3.12-slim

# System deps for pandas/numpy wheels (small, no build-essential needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY analysis.py daemon.py display.py journal.py notify.py trade.py ./

# Persistent dirs (mounted as volumes at runtime)
RUN mkdir -p logs trade_notes && \
    useradd -u 1000 -m trader && \
    chown -R trader:trader /app
USER trader

# tini handles signals cleanly so SIGTERM stops daemon gracefully
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "daemon.py"]
