FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir ".[datasets]"
ENV OPS_STATE_DIR=/app/.state PYTHONUNBUFFERED=1
# The supervisor stays in the foreground. Run CLI commands with docker exec.
CMD ["python", "-m", "ops_recovery.lab", "--role", "supervisor", "--directory", "/app/.state/lab"]
