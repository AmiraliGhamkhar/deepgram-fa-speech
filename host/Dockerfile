# Minimal image for the Medical STT host service.
# The Deepgram API key is supplied at runtime only -- never via a build
# argument or a baked-in layer.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY host/requirements.txt ./host/requirements.txt
RUN pip install --no-cache-dir -r host/requirements.txt

COPY host/ ./host/

# Run as an unprivileged user; the service needs no write access.
RUN useradd --system --no-create-home --uid 10001 stt
USER stt

EXPOSE 8443

# TLS cert/key are mounted at runtime:
#   -v /etc/letsencrypt:/etc/letsencrypt:ro
CMD ["python", "-m", "host.app"]
