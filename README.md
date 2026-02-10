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

* How to Build and Install

`make package` will build the installable package.
`make install` will install that built package

* How to Run

** `asimapd`

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
          [--pwfile=<pwfile>]

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
```

** Environment Variables

`ENABLE_MH_FILE_LOCKING` -- By default asimap does not use advisory file
locking on MH mailbox folders. Set this to `true` to re-enable file locking
for environments where external MH command-line clients (e.g., `inc`, `scan`,
`rmm`) are actively modifying the same mail store concurrently. Disabling file
locking prevents file descriptor exhaustion on systems with large numbers of
mailboxes.

** `asimapd_set_password`

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
