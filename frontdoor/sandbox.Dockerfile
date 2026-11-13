# A visitor's sandbox on Daytona: the demo's dependencies, not its code. The front door
# uploads refund_demo/ into /app when it creates a sandbox, so a code change needs only a
# redeploy of the front door, not a new snapshot (frontdoor/snapshot.py names the snapshot
# after a hash of this file and the lock file).
FROM python:3.12-slim

ARG TEMPORAL_CLI_VERSION=1.8.3
ARG UV_VERSION=0.13.0
ARG TARGETARCH=amd64

# The Temporal CLI (for its dev server), fetched with the image's own Python; curl for the
# front door's health checks.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
    && python -c "import io, sys, tarfile, urllib.request; \
url = 'https://github.com/temporalio/cli/releases/download/v{0}/temporal_cli_{0}_linux_{1}.tar.gz'.format(*sys.argv[1:]); \
tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(url).read())).extract('temporal', '/usr/local/bin', filter='data')" \
       "${TEMPORAL_CLI_VERSION}" "${TARGETARCH}" \
    && temporal --version \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
