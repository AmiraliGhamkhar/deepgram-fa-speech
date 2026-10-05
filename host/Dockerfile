# Minimal image for the Medical STT host service.
#
# The Deepgram API key is supplied at *runtime* only -- never via a build
# argument or a baked-in layer. No secret is ever COPYed in, so the image can
# be published to a registry without exposing a credential.
#
# Sized for 50 concurrent streaming sessions: the host only mints short-lived
# tokens, so it needs no audio stack, no Deepgram SDK and no database. Peak
# memory is dominated by the HTTP connection pool (default 100 keepalive
# connections), which is why the default pool is sized for the target.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY host/requirements.txt ./host/requirements.txt
RUN pip install --no-cache-dir -r host/requirements.txt \
    && useradd --system --no-create-home --create-home --uid 10001 stt

COPY host/ ./host/

# Run as an unprivileged user; the service needs no write access.
USER stt

EXPOSE 8443

# Liveness/readiness. /readyz reflects whether the Deepgram connection pool
# is initialized, so an orchestrator can delay traffic until the host is
# genuinely able to serve.
#
# The probe protocol MUST match the served protocol: the documented run
# passes HOST_TLS_CERTFILE/HOST_TLS_KEYFILE to uvicorn, which then serves
# HTTPS on that same port -- an http:// probe would fail forever and mask
# real outages with a permanent 'unhealthy'. When the TLS variables are
# set the probe switches to https with an unverified context, which is
# safe here: it talks to loopback inside the container, and there is
# nothing to impersonate.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,ssl,urllib.request; scheme='https' if (os.getenv('HOST_TLS_CERTFILE') and os.getenv('HOST_TLS_KEYFILE')) else 'http'; ctx=ssl._create_unverified_context() if scheme=='https' else None; urllib.request.urlopen(scheme+'://127.0.0.1:'+os.getenv('HOST_PORT','8443')+'/readyz',timeout=4,context=ctx)"

# TLS cert/key are mounted at runtime:
#   -v /etc/letsencrypt:/etc/letsencrypt:ro
#   -v /secure/host/clients.txt:/srv/host/clients.txt:ro
#
# Graceful shutdown: uvicorn gets time to finish in-flight token requests
# before SIGTERM takes effect (see HOST_GRACEFUL_SHUTDOWN).
STOPSIGNAL SIGTERM
CMD ["python", "-m", "host.app"]