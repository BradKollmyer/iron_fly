FROM python:3.12-slim
WORKDIR /app

# git is required to pip-install iron-fly-strategy from GitHub @v0.1.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY scripts ./scripts
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh scripts/download-replay.py

ENV DATA_DIR=/data
ENV REPLAY_CACHE=/app/replay
ENV DESK_MODE=backtest-public
ENV PYTHONPATH=/app/src
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "iron_fly"]
