# asimap -- Docker Compose Deployment

This directory is a complete deployment of `asimapd` with Docker Compose, for
production, staging or integration hosts. Copy it to the host, fill in `.env`
and `secrets.env`, and start it. Nothing here is used by local development,
which uses `docker-compose.yml` at the repo root.

The stack runs one container from the pre-built image on GHCR
(`ghcr.io/scanner/asimap`). It serves IMAPS on port 993 and, optionally,
POP3S on 995.

---

## Directory layout

```
deployment/compose/
  docker-compose.yml    # the asimap service
  dot-env.example       # non-secret config template -- copy to .env
  secrets.env.example   # secret config template -- copy to secrets.env
  README.md             # this file
```

---

## 1. Prepare the mail store

`asimapd` runs as the unprivileged `app` user, UID 900, and everything it
reads or writes lives in one host directory mounted at `/opt/asimap`:

```
<OPT_ASIMAP_DIR>/
  asimapd_passwords.txt   # accounts: username:hash:maildir, one per line
  ssl/ssl_crt.pem         # TLS certificate (full chain)
  ssl/ssl_key.pem         # TLS private key
  <maildir>/...           # each account's MH mail directory
  traces/                 # protocol traces, only when TRACE is set
```

Create it and give it to UID 900:

```sh
sudo install -d -o 900 -g 900 -m 0700 /srv/asimap /srv/asimap/ssl
```

When `as_email_service` manages the accounts, `OPT_ASIMAP_DIR` is its mail
directory tree: it writes the password file and the mail directories there,
and delivers new mail through the delivery spool inside each mail directory.
Otherwise, create accounts with `asimapd_set_password` (see the top-level
README).

---

## 2. Install the TLS certificate

Copy the certificate chain and key into `ssl/`, readable only by UID 900:

```sh
sudo install -o 900 -g 900 -m 0400 fullchain.pem /srv/asimap/ssl/ssl_crt.pem
sudo install -o 900 -g 900 -m 0400 privkey.pem   /srv/asimap/ssl/ssl_key.pem
```

`asimapd` reads the certificate at startup, so restart it after replacing
the files: `docker compose restart asimap`. With certbot, a deploy hook in
`/etc/letsencrypt/renewal-hooks/deploy/` that runs both steps keeps the
served certificate current across renewals.

---

## 3. Configure .env and secrets.env

```sh
cp dot-env.example .env
cp secrets.env.example secrets.env
chmod 0600 secrets.env
$EDITOR .env secrets.env
```

Set at minimum `VERSION` and `OPT_ASIMAP_DIR` in `.env`. `secrets.env` holds
`SENTRY_DSN`; leave it commented out to run without Sentry.

`asimapd` takes its settings from plain environment variables, so the secret
reaches the container as a second `env_file` rather than a mounted secret
file. Keeping it out of `.env` leaves `.env` safe to read.

`DEBUG`, `TRACE` and `ENABLE_POP3` are on when set to any non-empty value,
including `0` and `false`. Comment them out to turn them off.

To serve POP3S, set `ENABLE_POP3=1` in `.env` and uncomment the `995` port
in `docker-compose.yml`.

---

## 4. Start the stack

```sh
docker compose pull
docker compose up -d
docker compose logs -f asimap
```

---

## 5. Smoke test

Check the certificate being served and that the server answers:

```sh
openssl s_client -connect imap.example.com:993 -servername imap.example.com \
    </dev/null 2>/dev/null | openssl x509 -noout -subject -enddate
```

Then log in with a mail client, or with `openssl s_client` and
`a1 LOGIN <user> <password>`, and `a2 SELECT INBOX`.

---

## 6. Updating and rollback

Edit `.env` to set the version to run, then pull and recreate:

```sh
# In .env: VERSION=2.7.1
docker compose pull
docker compose up -d
```

Each account's `asimap.db`, in its mail directory, is migrated forward
automatically the first time a release opens it, and there are no reverse
migrations. Rolling back is the same steps with the previous version, as long
as no release in between added a database migration; otherwise restore the
`asimap.db` files from a backup taken before the upgrade.

---

## Debugging

The images include [py-spy](https://github.com/benfred/py-spy). It needs the
`SYS_PTRACE` capability; add it to the service while investigating:

```yaml
services:
  asimap:
    cap_add:
      - SYS_PTRACE
```

Both images include the `nmh` command line tools; the `-dev` images (e.g.
`VERSION=2.7.0-dev`) add editors. See the top-level README for using py-spy.
