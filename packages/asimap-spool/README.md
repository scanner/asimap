# asimap-spool

Deliver messages to the [asimap](https://github.com/scanner/asimap) IMAP
server by dropping them in its spool directory. No dependencies outside the
standard library.

```python
from asimap_spool import Delivery, deliver

deliver(maildir, raw_message_bytes)  # to the inbox, unseen
deliver(
    maildir,
    raw_message_bytes,
    [Delivery("Lists/python", flags=("\\Seen",), create=True)],
    source="as_email_service",
)
```

## How it works

Each user's MH mail store has a `.asimap-spool/` directory at its root. asimap
does not treat it as a mail folder, and MH tools find no messages in it.

1. A producer writes `<uuid7>-incoming.json`, fsyncs it, and renames it to
   `<uuid7>-ready.json`.
2. asimap imports ready entries in id order, which is creation order, then
   removes them.
3. Entries asimap can not import, and incoming files a producer abandoned, are
   moved to `.asimap-spool/failed/`.

Delivery is at least once: a crash during import can deliver a message twice.

`deliver()` creates `.asimap-spool/` if it does not exist. If it can not
create the directory or write into it, it raises `SpoolUnavailableError` (an
`OSError`) and leaves nothing behind, so the caller can deliver the message
some other way:

```python
from asimap_spool import SpoolUnavailableError, deliver

try:
    deliver(maildir, raw_message_bytes)
except SpoolUnavailableError:
    ...  # fall back, e.g. write straight into the MH folder
```

## Entry format

`asimap_spool.schema()` returns the JSON Schema
(`spool-entry.schema.json`). Version 1:

| Field              | Required | Meaning                                                       |
|--------------------|----------|---------------------------------------------------------------|
| `version`          | yes      | `1`                                                           |
| `id`               | yes      | lowercase uuid7; matches the file name                        |
| `received_at`      | yes      | ISO 8601 with a timezone; becomes the internal date           |
| `source`           | no       | producer name, for logging                                    |
| `deliveries`       | no       | list of `{folder, flags, create}`; empty means the inbox      |
| `message_encoding` | yes      | `"base64"`                                                    |
| `message`          | yes      | the raw RFC 5322 message, base64 encoded                      |

A delivery's `flags` are IMAP flag names (`\Seen`, `\Flagged`, keywords). A
message without `\Seen` is unseen. If `create` is false and the folder does not
exist, asimap delivers to the inbox.
