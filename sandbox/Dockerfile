# Test sandbox image (docs/sandbox-design.md). Build it with: orch sandbox build
# It holds the tools only. Source arrives read-only at /src, the venv at /venv; both are mounted at run time.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/
RUN apt-get update \
 && apt-get install -y --no-install-recommends git rsync \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 1000 -m builder \
 && mkdir /venv /uvcache /work \
 && chown 1000:1000 /venv /uvcache /work
COPY entry.sh /entry.sh
# a checkout with CRLF line endings must not break the shebang
RUN sed -i "s/\r$//" /entry.sh && chmod 755 /entry.sh
USER 1000:1000
ENTRYPOINT ["/entry.sh"]
