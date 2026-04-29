FROM python:3.12-slim

# System deps: tini for signal handling, node + claude-code CLI for daemon-triggered analysis
RUN apt-get update && apt-get install -y --no-install-recommends \
    tini curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY analysis.py backtest.py charting.py claude_agent.py daemon.py display.py journal.py notify.py risk.py trade.py whale_flow.py ./

# Persistent dirs (mounted as volumes at runtime). /home/trader/.claude holds the
# claude-code login token — backed by a named volume so it survives rebuilds.
RUN mkdir -p logs trade_notes && \
    useradd -u 1000 -m trader && \
    mkdir -p /home/trader/.claude && \
    chown -R trader:trader /app /home/trader
USER trader

# tini handles signals cleanly so SIGTERM stops daemon gracefully
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-u", "daemon.py"]
