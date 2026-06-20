[![Build Status](https://drone.apricot.com/api/badges/scanner/asimap/status.svg?ref=refs/heads/main)](https://drone.apricot.com/scanner/asimap)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)

'asimap' is a python IMAP server that uses python's mailbox module to
provide the backing store. This lets us export things like an MH mail
store via IMAP. Actually the way it is coded right now it ONLY support
'mailbox.MH' style folders.

We go to various lengths to actually work alongside currently running
MH clients accessing the same files at the same time.

It uses a multiprocess model where there is a main server process
running as root and for any logged in user there is a sub-process
running as that user. If a user logs in via more than one mail client
all of their connections will be handled by the same
sub-process. Sub-processes stick around for a short while after a user
disconnects to avoid startup time if they connect again within, say, 30
minutes.

This is not a high performance server being pure python and it is not
a highly scaleable one requiring a process for every logged in user.

NOTE: Placeholder instructions

# How to Build and Install

`make package` will build the installable package.
`make install` will install that built package

# How to Run

## `asimapd`

Whether inside a docker container or from the command line `asimapd` tries to
assume reasonable defaults to run without needing to specify any command line
options.

Usually the most important command line option is `--pwfile` which contains the
location of the account/password file. The format of the password file is a
username, a password hash, and the root directory that contains the maildir for
that user.

The password hash uses the routines from Django so that a password saved from a
Django app can be used in `asimapd`. See
https://github.com/django/django/blob/main/django/contrib/auth/hashers.py for
details.

Command line argument for asimapd:

``` text
NOTE: For all command line options that can also be specified via an env. var:
      the command line option will override the env. var if set.

Usage:
  asimapd [--address=<i>] [--port=<p>] [--cert=<cert>] [--key=<key>]
          [--trace] [--trace-dir=<td>] [--debug] [--log-config=<lc>]
          [--pwfile=<pwfile>] [--enable-pop3] [--pop3-port=<pp>]

Options:
  --version
  -h, --help         Show this text and exit
  --address=<i>      The address to listen on. Defaults to '0.0.0.0'.
                     The env. var is `ADDRESS`.
  --port=<p>         Port to listen on. Defaults to: 993.
                     The env. var is `PORT`
  --cert=<cert>      SSL Certificate file. If not set defaults to
                     `/opt/asimap/ssl/cert.pem`. The env var is SSL_CERT
  --key=<key>        SSL Certificate key file. If not set defaults to
                     `/opt/asimap/ssl/key.pem`. The env var is SSL_KEY

  --trace            For debugging and generating protocol test data `trace`
                     can be enabled. When enabled messages will appear on the
                     `asimap.trace` logger where the `message` part of the log
                     message is a JSON dump of the message being sent or
                     received. This only happens for post-authentication IMAP
                     messages (so nothing about logging in is recorded.)
                     However the logs are copious! The default logger will dump
                     trace logs where `--trace-dir` specifies.

  --trace-dir=<td>   The directory trace log files are written to. Unless
                     overriden by specifying a custom log config! Since traces
                     use the logging system if you supply a custom log config
                     and turn tracing on that will override this. By default
                     trace logs will be written to `/opt/asimap/traces/`. By
                     default the traces will be written using a
                     RotatingFileHandler with a size of 20mb, and backup count
                     of 5 using the pythonjsonlogger.jsonlogger.JsonFormatter.

  --debug            Will set the default logging level to `DEBUG` thus
                     enabling all of the debug logging. The env var is `DEBUG`

  --log-config=<lc>  The log config file. This file may be either a JSON file
                     that follows the python logging configuration dictionary
                     schema or a file that coforms to the python logging
                     configuration file format. If no file is specified it will
                     check in /opt/asimap, /etc, /usr/local/etc, /opt/local/etc
                     for a file named `asimapd_log.cfg` or `asimapd_log.json`.
                     If no valid file can be found or loaded it will defaut to
                     logging to stdout. The env. var is `LOG_CONFIG`

  --pwfile=<pwfile>  The file that contains the users and their hashed passwords
                     The env. var is `PWFILE`. Defaults to `/opt/asimap/pwfile`

  --enable-pop3      Enable POP3S (POP3 over TLS) server. Disabled by default.
                     The env. var is `ENABLE_POP3`.

  --pop3-port=<pp>   Port for POP3S. Defaults to: 995.
                     The env. var is `POP3_PORT`
```

## POP3 Support

asimap includes an optional POP3S (POP3 over TLS) server. POP3 access is
restricted to the INBOX only — no other folders are visible. Messages are
snapshotted at session start; deletions via `DELE` are only committed on a
clean `QUIT`. Disconnecting without `QUIT` leaves all messages intact.

The POP3 server reuses the same per-user subprocess as IMAP connections, so
a user can have both IMAP and POP3 clients connected simultaneously.

To enable POP3, either pass `--enable-pop3` on the command line or set the
`ENABLE_POP3` environment variable. In a Docker deployment, expose port 995
and add the env var:

``` yaml
# docker-compose.yml
services:
  asimap:
    ports:
      - "993:993"
      - "995:995"
    env_file: .env
```

``` shell
# .env
ENABLE_POP3=1
```

## Environment Variables

Every command-line option also has a corresponding environment variable (noted
in the CLI help above). The table below covers all recognised variables. Any
variable that has a CLI equivalent can be overridden by the CLI flag at runtime.

### Example `.env`

```shell
##############################################################################
# Core server
##############################################################################

# Address to bind the IMAPS listener. Default: 0.0.0.0 (all interfaces)
ADDRESS=0.0.0.0

# IMAPS port. Default: 993
PORT=993

##############################################################################
# SSL/TLS
##############################################################################

# Path to the PEM-encoded TLS certificate. Default: /opt/asimap/ssl/cert.pem
SSL_CERT=/opt/asimap/ssl/cert.pem

# Path to the TLS private key. Default: /opt/asimap/ssl/key.pem
SSL_KEY=/opt/asimap/ssl/key.pem

##############################################################################
# Authentication
##############################################################################

# Password file (username:hash:maildir_root, one entry per line).
# Default: /opt/asimap/pwfile
# The hash format is compatible with Django's password hashers (PBKDF2,
# Argon2, BCrypt, scrypt), so accounts can be imported from a Django app.
PWFILE=/opt/asimap/pwfile

##############################################################################
# POP3S (optional — disabled by default)
##############################################################################

# Set to 1/true/yes to enable the POP3S server (INBOX only).
# ENABLE_POP3=1

# POP3S port. Default: 995
# POP3_PORT=995

##############################################################################
# Logging and debugging
##############################################################################

# Set to 1/true/yes to enable DEBUG-level logging.
# DEBUG=1

# Path to a Python logging config file (JSON dict schema or .cfg format).
# Searched in /opt/asimap, /etc, /usr/local/etc, /opt/local/etc if not set.
# LOG_CONFIG=/opt/asimap/asimapd_log.json

# Set to 1/true/yes to enable IMAP protocol tracing (post-auth only).
# Produces JSON log entries on the `asimap.trace` logger. Very verbose.
# TRACE=1

# Directory for rotating trace log files. Default: /opt/asimap/traces/
# TRACE_DIR=/opt/asimap/traces

##############################################################################
# Mail store
##############################################################################

# Set to true to enable advisory file locking on MH mailbox folders.
# Useful when external MH command-line clients (inc, scan, rmm) access the
# same mail store concurrently. Disabled by default because it can exhaust
# file descriptors on systems with 1000+ mailboxes.
# ENABLE_MH_FILE_LOCKING=true

##############################################################################
# Observability — Sentry (all optional; Sentry is disabled if DSN is unset)
##############################################################################

# Sentry DSN. Leave unset to disable Sentry entirely.
# SENTRY_DSN=https://xxx@oyyy.ingest.sentry.io/zzz

# Fraction (0.0–1.0) of IMAP command transactions to send to Sentry.
# 0 disables performance tracing (errors are always captured regardless).
# Default: 0
# SENTRY_TRACES_SAMPLE_RATE=0.01

# Fraction (0.0–1.0) for Sentry profiling. Requires a Sentry Business plan.
# Default: 0
# SENTRY_PROFILES_SAMPLE_RATE=0

# Set to 1/true/yes to enable per-asyncio-task spans in Sentry.
# Off by default: asimap spawns many short-lived tasks (e.g. per SEARCH
# sub-term), so task spans add measurable CPU overhead for zero useful data
# unless you are specifically debugging task scheduling.
# SENTRY_ASYNCIO_TASK_SPANS=0
```

## Performance Profiling

The Docker images include [py-spy](https://github.com/benfred/py-spy), a
sampling profiler that can attach to running processes and follow subprocesses.
This is useful for understanding CPU usage across asimap's multi-process
architecture.

### Dev container

The dev container is already configured with `SYS_PTRACE` capability. Start it
and open a root shell:

``` shell
make up
make profile
```

Inside the root shell, find the asimapd root process and start profiling:

``` shell
# Record a 60-second profile of the root process and all user subprocesses
py-spy record --subprocesses --format raw --duration 60 --rate 100 \
  --pid $(pgrep -f asimapd) --output /opt/asimap/traces/profile.txt

# Instant stack dump of all threads across all processes
py-spy dump --subprocesses --pid $(pgrep -f asimapd)

# Live top-like view
py-spy top --subprocesses --pid $(pgrep -f asimapd)
```

The `raw` format produces collapsed stack traces with sample counts, one line
per unique call stack. Output written to `/opt/asimap/traces/` is accessible
from the host via the mounted volume.

### Prod container

For production profiling, add `SYS_PTRACE` when starting the container:

``` shell
docker run --cap-add SYS_PTRACE ...
```

Or in docker-compose, add to the prod service temporarily:

``` yaml
cap_add:
  - SYS_PTRACE
```

Then exec in as root to run py-spy:

``` shell
docker exec -u root -ti asimap /bin/bash
```

## `asimapd_set_password`

``` text
A script to set passwords for asimap accounts (creates the account if it
does not exist.)

This is primarily used for setting up a test development environment. In the
Apricot Systematic typical deployment the password file is managed by the
`as_email_service`

If the `password` is not supplied an unuseable password is set effectively
disabling the account.

If the account does not already exist `maildir` must be specified (as it
indicates the users mail directory root)

NOTE: `maildir` is in relation to the root when asimapd is running.

Usage:
  set_password [--pwfile=<pwfile>] <username> [<password>] [<maildir>]

Options:
  --version
  -h, --help         Show this text and exit
  --pwfile=<pwfile>  The file that contains the users and their hashed passwords
                     The env. var is `PWFILE`. Defaults to `/opt/asimap/pwfile`
```
