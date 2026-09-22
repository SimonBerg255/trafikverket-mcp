# Container image for Kubernetes / any Docker host.
# NOTE: deliberately NOT at the repo root – Railway auto-detects a root Dockerfile and then runs the
# dashboard start command without a shell, so "$PORT" is never expanded. Railway uses Railpack instead.
#   docker build -f deploy/Dockerfile -t trafikverket-mcp .
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
EXPOSE 8000
# server.py reads PORT from the environment itself (default 8000) – no shell expansion needed.
ENTRYPOINT ["python", "server.py"]
