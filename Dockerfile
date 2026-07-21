# zimapp as a ZimaOS app.
#
# Built on the ZimaOS host itself (`docker build -t zimapp:local`), because that
# needs no registry account and the image is only ever going to run there.
# ZimaOS installs prebuilt images only (§4.4.1) — "prebuilt" here means: present
# in the host's docker daemon, referenced by tag from the compose.

FROM python:3.12-alpine

# PyYAML is the only dependency. --no-cache keeps the image small and skips the
# wheel build (alpine has a matching musl wheel).
RUN pip install --no-cache-dir pyyaml==6.0.2

WORKDIR /app
COPY zimapp.py zimapp_core.py zimapp_web.py ./
COPY static/ ./static/
COPY blueprints/ ./blueprints/

# Do not run as root: the app fetches foreign URLs and renders their content.
RUN adduser -D -u 1000 zimapp && chown -R zimapp:zimapp /app
USER zimapp

EXPOSE 8790

# There is deliberately no ssh client in here. The port check falls back to the
# app grid API, which needs credentials but no SSH — `inspect` and `generate`
# (the single-image path) are the only things that stay unavailable.

# --bind 0.0.0.0, because otherwise the container only serves itself.
#
# An earlier comment here claimed "the protection is the host's port mapping, not
# the bind address inside". That is not true for the app as installed: the
# generated compose publishes the port on 0.0.0.0, so the whole LAN reaches
# /api/* — measured 2026-07-21, GET /api/defaults answers 200 from another
# machine. There is no login. This is accepted on purpose until the planned user
# management lands; whoever wants it closed sooner publishes the port on
# 127.0.0.1 and reaches it over SSH/Tailscale.
CMD ["python3", "zimapp.py", "serve", "--bind", "0.0.0.0", "--port", "8790"]
