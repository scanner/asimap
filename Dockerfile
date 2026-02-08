#########################
#
# Builder stage
#
FROM python:3.12.9 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ARG APP_HOME=/app
WORKDIR ${APP_HOME}
COPY pyproject.toml uv.lock README.md LICENSE /app/
COPY asimap /app/asimap

RUN uv sync --frozen --no-dev
RUN uv run python -m build

#########################
#
# includes the 'development' dependencies
#
FROM builder AS dev

LABEL org.opencontainers.image.source=https://github.com/scanner/asimap
LABEL org.opencontainers.image.description="Apricot Systematic IMAP Demon"
LABEL org.opencontainers.image.licenses=BSD-3-Clause

RUN apt update && apt install --assume-yes jove vim nmh && apt clean

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

ARG APP_HOME=/app

WORKDIR ${APP_HOME}
ENV PYTHONPATH=${APP_HOME}

RUN uv sync --frozen

# Puts the venv's python (and other executables) at the front of the
# PATH so invoking 'python' will activate the venv.
#
ENV PATH=/app/.venv/bin:/usr/bin/mh:$PATH

WORKDIR ${APP_HOME}

RUN addgroup --system --gid 900 app \
    && adduser --system --uid 900 --ingroup app app

USER app

CMD ["python", "/app/asimap/asimapd.py"]

#########################
#
# `prod` - The docker image for the production service
#
FROM python:3.12.9-slim AS prod

LABEL org.opencontainers.image.source=https://github.com/scanner/asimap
LABEL org.opencontainers.image.description="Apricot Systematic IMAP Demon"
LABEL org.opencontainers.image.licenses=BSD-3-Clause

RUN apt update && apt install --assume-yes nmh && apt clean

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# We only want the installable dist we created in the builder.
#
COPY --from=builder /app/dist /app/dist
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ARG VERSION
RUN uv venv /venv && \
    VIRTUAL_ENV=/venv uv pip install /app/dist/asimap-${VERSION}-py3-none-any.whl

# Puts the venv's python (and other executables) at the front of the
# PATH so invoking 'python' will activate the venv.
#
ENV PATH=/venv/bin:/usr/bin/mh:$PATH

ARG APP_HOME=/app
WORKDIR ${APP_HOME}

RUN addgroup --system --gid 900 app \
    && adduser --system --uid 900 --ingroup app app

USER app

# NOTE: All the configuration for asimapd, like where the password file is and
# where the SSL files are are passed via env vars.
#
CMD ["/venv/bin/asimapd"]
