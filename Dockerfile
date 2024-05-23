#########################
#
# Builder stage
#
FROM python:3.12 as builder

ARG APP_HOME=/app
WORKDIR ${APP_HOME}
COPY requirements/build.txt /app/requirements/build.txt
COPY README.md LICENSE Makefile Make.rules pyproject.toml /app/
RUN python -m venv --copies /venv
RUN . /venv/bin/activate && \
    pip install --upgrade pip && \
    pip install --upgrade setuptools && \
    pip install -r /app/requirements/build.txt

COPY asimap /app/asimap
RUN . /venv/bin/activate && python -m build

#########################
#
# includes the 'development' requirements
#
FROM builder as dev

LABEL org.opencontainers.image.source=https://github.com/scanner/asimap
LABEL org.opencontainers.image.description="Apricot Systematic IMAP Demon"
LABEL org.opencontainers.image.licenses=BSD-3-Clause

RUN apt update && apt install --assume-yes jove vim && apt clean

ENV PYTHONUNBUFFERED 1
ENV PYTHONDONTWRITEBYTECODE 1

ARG APP_HOME=/app
ARG VERSION

WORKDIR ${APP_HOME}
ENV PYTHONPATH ${APP_HOME}

COPY README.md LICENSE Makefile Make.rules pyproject.toml /app/
COPY requirements/development.txt /app/requirements/development.txt
COPY asimap /app/asimap
RUN . /venv/bin/activate && pip install -r requirements/development.txt

# Puts the venv's python (and other executables) at the front of the
# PATH so invoking 'python' will activate the venv.
#
ENV PATH /venv/bin:$PATH

WORKDIR ${APP_HOME}

CMD ["python", "/app/asimap/bin/asimapd"]

#########################
#
# `app` - The docker image for the django app web service
#
FROM python:3.12-slim as prod

LABEL org.opencontainers.image.source=https://github.com/scanner/asimap
LABEL org.opencontainers.image.description="Apricot Systematic IMAP Demon"
LABEL org.opencontainers.image.licenses=BSD-3-Clause

ARG APP_HOME=/app
ARG VERSION

ENV PYTHONUNBUFFERED 1
ENV PYTHONDONTWRITEBYTECODE 1

# We only want the installable dist we created in the builder.
#
COPY --from=builder /app/dist /app/dist

RUN python -m venv --copies /venv
RUN . /venv/bin/activate && \
    pip install /app/dist/asimap-${VERSION}-py3-none-any.whl

# Puts the venv's python (and other executables) at the front of the
# PATH so invoking 'python' will activate the venv.
#
ENV PATH /app/venv/bin:$PATH

WORKDIR ${APP_HOME}

RUN addgroup --system --gid 900 app \
    && adduser --system --uid 900 --ingroup app app

USER app

# NOTE: All the configuration for asimapd, like where the password file is and
# where the SSL files are are passed via env vars.
#
CMD ["/app/venv/bin/asimapd"]
