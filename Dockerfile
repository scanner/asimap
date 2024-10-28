#########################
#
# Builder stage
#
FROM python:3.12.7 AS builder

ARG APP_HOME=/app
WORKDIR ${APP_HOME}
COPY requirements/build.txt requirements/production.txt /app/requirements/
COPY README.md LICENSE Makefile Make.rules pyproject.toml /app/
RUN python -m venv --copies /venv
RUN . /venv/bin/activate && \
    pip install --upgrade pip && \
    pip install --upgrade setuptools && \
    pip install -r /app/requirements/build.txt && \
    pip install -r /app/requirements/production.txt

COPY asimap /app/asimap
RUN . /venv/bin/activate && python -m build

#########################
#
# includes the 'development' requirements
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

COPY README.md LICENSE Makefile Make.rules pyproject.toml /app/
COPY requirements/development.txt /app/requirements/development.txt
COPY asimap /app/asimap
RUN . /venv/bin/activate && pip install -r requirements/development.txt

# Puts the venv's python (and other executables) at the front of the
# PATH so invoking 'python' will activate the venv.
#
ENV PATH=/venv/bin:/usr/bin/mh:$PATH

WORKDIR ${APP_HOME}

RUN addgroup --system --gid 900 app \
    && adduser --system --uid 900 --ingroup app app

USER app

CMD ["python", "/app/asimap/asimapd.py"]

#########################
#
# `app` - The docker image for the django app web service
#
FROM python:3.12.7-slim AS prod

LABEL org.opencontainers.image.source=https://github.com/scanner/asimap
LABEL org.opencontainers.image.description="Apricot Systematic IMAP Demon"
LABEL org.opencontainers.image.licenses=BSD-3-Clause

RUN apt update && apt install --assume-yes nmh && apt clean

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# We only want the installable dist we created in the builder.
#
COPY --from=builder /app/dist /app/dist
COPY --from=builder /venv /venv

ARG VERSION
RUN . /venv/bin/activate && \
    pip install /app/dist/asimap-${VERSION}-py3-none-any.whl

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
