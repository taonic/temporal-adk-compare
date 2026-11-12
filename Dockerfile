# The whole demo in one container: the API and UI, the business systems, two
# ADK hosts, two Temporal Workers and a Temporal dev server, all started and
# supervised by refund_demo.api, exactly as `make demo` runs them locally.
FROM python:3.12-slim

ARG TEMPORAL_CLI_VERSION=1.8.3
ARG UV_VERSION=0.13.0
ARG TARGETARCH=amd64

# The Temporal CLI (for its dev server), fetched with the image's own Python.
RUN python -c "import io, sys, tarfile, urllib.request; \
url = 'https://github.com/temporalio/cli/releases/download/v{0}/temporal_cli_{0}_linux_{1}.tar.gz'.format(*sys.argv[1:]); \
tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(url).read())).extract('temporal', '/usr/local/bin', filter='data')" \
       "${TEMPORAL_CLI_VERSION}" "${TARGETARCH}" \
    && temporal --version \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY refund_demo ./refund_demo

# Listen on every interface inside the container. The dev server's Web UI is
# exposed too, so the "open in Temporal UI" links work.
ENV API_HOST=0.0.0.0 \
    TEMPORAL_UI_IP=0.0.0.0 \
    PYTHONUNBUFFERED=1
EXPOSE 8400 8233

CMD [".venv/bin/python", "-m", "refund_demo.api"]
