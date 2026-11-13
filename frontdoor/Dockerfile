# The front door on Fly.io: gives every visitor a Daytona sandbox of their own and proxies to it.
# Small on purpose: the demos run on Daytona, not here. It carries refund_demo/ only to upload it
# into new sandboxes, and the files frontdoor/snapshot.py hashes to name the snapshot it uses.
FROM python:3.12-slim

ARG UV_VERSION=0.13.0
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --only-group frontdoor --no-install-project
COPY frontdoor ./frontdoor
COPY refund_demo ./refund_demo

ENV PORT=8080 \
    UI_PORT=8081 \
    PYTHONUNBUFFERED=1
EXPOSE 8080 8081

CMD [".venv/bin/python", "-m", "frontdoor"]
