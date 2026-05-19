# s3_store

S3-backed file storage for Frappe with **native `bench backup` / `bench restore` integration**.

Unlike other S3 attachment apps, files uploaded via `s3_store` are pulled into the standard `bench backup --with-files` tar (so it's self-contained) and pushed back to S3 after `bench restore` with a single command — or automatically. No separate scheduled DB pushes, no parallel backup infrastructure.

Compatible with **AWS S3, MinIO, and any S3-compatible service**.

---

## Table of contents

1. [Installation](#installation)
2. [Configuration](#configuration)
3. [How uploads work](#how-uploads-work)
4. [How serving works](#how-serving-works)
5. [Backup with files](#backup-with-files)
6. [Restore](#restore)
7. [Bulk migration of pre-existing local files](#bulk-migration-of-pre-existing-local-files)
8. [Settings reference](#settings-reference)
9. [Architecture notes](#architecture-notes)
10. [Testing](#testing)
11. [Troubleshooting](#troubleshooting)
12. [Limitations](#limitations)

---

## Installation

```bash
bench get-app https://github.com/Centura-AG/s3_store
bench --site <site> install-app s3_store
```

This pulls in a single Python dependency: `boto3`. No node modules, no native extensions.

---

## Configuration

Open **S3 Store Settings** (singleton, System Manager only) in Desk.

**Minimum required for AWS S3**:

| Field                 | Example                |
| --------------------- | ---------------------- |
| Enabled               | ✓                      |
| Region                | `eu-central-1`         |
| AWS Access Key ID     | `AKIAIOSFODNN7EXAMPLE` |
| AWS Secret Access Key | `wJalrXUt…`            |
| Bucket                | `my-frappe-uploads`    |

**For MinIO / S3-compatible providers**, also set:

| Field        | Example                     |
| ------------ | --------------------------- |
| Endpoint URL | `https://minio.example.com` |

Saving the settings runs `head_bucket` against the configured S3 endpoint — if credentials, region, or bucket name are wrong, the save fails with a clear error.

---

## How uploads work

When `Enabled = 1`, every new File doc routes through the `write_file` hook:

1. The file's bytes are uploaded to S3 under a generated key:
   `{key_prefix}/{YYYY/MM/DD}/{attached_to_doctype}/{token8}_{sanitized_name}`
2. The File doc's `file_url` is rewritten to one of:
   - **Presigned URL mode** (default): `/api/method/s3_store.s3_store.api.serve?key=<encoded>`
   - **Public ACL mode**: a direct S3 URL (bucket must allow public reads)
3. No local copy is kept.

The `token8` is a random 4-byte hex prefix — uploads of the same `file.pdf` from different forms get distinct keys, so they can't overwrite each other in S3 or on disk during backup staging.

**Ignoring specific doctypes** — list one DocType per line under _Advanced → Ignored Doctypes_ to keep their attachments on the local filesystem (useful for `User` profile pictures, for example).

---

## How serving works

### Presigned URL mode (default)

`/api/method/s3_store.s3_store.api.serve?key=<encoded>` resolves the key to a File doc, checks `has_permission` if the file is private, then 302-redirects to a short-lived presigned URL. Expiry is configurable (`signed_url_expiry`, default 3600s).

Works with **private buckets** — credentials never leave the server.

### Public ACL mode

Files are written with `ACL: public-read` and the `file_url` is the direct S3 URL. Browsers fetch them straight from S3, bypassing the app. Use this if you need maximum performance for public-facing content and you're comfortable making the bucket world-readable.

Private files always use the presigned-URL path regardless of mode.

---

## Backup with files

```bash
bench --site <site> backup --with-files
```

Behind the scenes, `s3_store` monkey-patches `BackupGenerator.backup_files` so that — **only when this site has `S3 Store Settings.enabled = 1` and `include_in_native_backup = 1`** — every S3-hosted file is downloaded into `public/files/` or `private/files/` _before_ `tar` runs, and removed _after_. The patch is a no-op on sites without the app installed.

The resulting tar is self-contained: you can move it to a different host, a developer laptop, or a fresh bucket and restore without losing any uploads.

### Disk-space pre-flight

Before staging, `s3_store` totals `file_size` across all S3-backed File records and requires ~110% free on the disk hosting the site. If insufficient, the backup aborts with:

```
Insufficient disk space to stage S3 files for backup:
need <N> bytes, have <M> bytes free under <site_path>.
Free up space or disable include_in_native_backup in S3 Store Settings.
```

### Encrypted backups

`bench backup --with-files --backup-encryption-key <key>` works unchanged. Encryption runs _after_ `backup_files()` in Frappe core, so the staged S3 contents end up inside the encrypted blob.

### Concurrency

A file lock at `sites/<site>/.s3_store_backup.lock` prevents two backups from staging on top of each other. The second attempt fails fast with: `"Another s3_store backup is already running for this site."`

### Stale-tar shortcut

`BackupGenerator.get_recent_backup` normally returns recent `*-files.tar` if one exists within the `--older-than` window. That would silently skip new S3 uploads — so `s3_store` neutralises the **files-portion only** when S3 is enabled, forcing a fresh stage. The DB shortcut is preserved.

---

## Restore

```bash
bench --site <site> restore <db.sql.gz> \
    --with-public-files <files.tar> \
    --with-private-files <private-files.tar>
```

After this, the DB has File records whose `file_url` already points at S3 (`?key=…`), and the bytes sit on disk under their staging names (`{token8}_{sanitized_name}`).

To push them back to S3:

```bash
bench --site <site> s3-store-push-local
```

For each staged file, this uploads to the **exact existing key** referenced by the File doc — no URL rewriting, no fresh keys. Local copies are deleted after a successful push.

### Auto-pilot

Set **Auto-Push Local Files after Migrate** in S3 Store Settings, and the push runs automatically as an `after_migrate` hook — i.e. at the end of every `bench migrate`, which conventionally follows `bench restore`. Cautious users leave this off and run the command explicitly.

---

## Bulk migration of pre-existing local files

Use this once, after first installing on a site that already has local attachments:

**Via the UI**: open **S3 Migration Log → New**, save. The migration runs as a background job; progress is published to the realtime channel and reflected on the log doc (`migrated`, `failed`, `errors`).

**Via API**:

```bash
bench --site <site> execute s3_store.s3_store.api.start_migration
```

For every File row with a `/files/...` or `/private/files/...` URL, `s3_store`:

1. Mints a fresh S3 key
2. Uploads the bytes
3. Rewrites `file_url` to the serve endpoint (or public URL)
4. Deletes the local copy (subject to `delete_local_after_push`)

Errors are collected per-file — one bad file doesn't abort the run. The log doc shows status, counts, and the per-file error list.

---

## Settings reference

| Field                      | Default         | Description                                                                                                                               |
| -------------------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`                  | `0`             | Master switch. When off, files go to the local filesystem as usual.                                                                       |
| `endpoint_url`             | —               | S3 endpoint. Blank for AWS; set for MinIO etc.                                                                                            |
| `region`                   | `us-east-1`     | AWS region.                                                                                                                               |
| `aws_access_key_id`        | —               | Required when enabled.                                                                                                                    |
| `aws_secret_access_key`    | —               | Required when enabled (stored encrypted via Frappe's password field).                                                                     |
| `bucket`                   | —               | Required when enabled.                                                                                                                    |
| `key_prefix`               | —               | Optional path prefix (e.g. site name). Objects are stored at `{prefix}/{YYYY/MM/DD}/{doctype}/{token}_{name}`.                            |
| `public_file_mode`         | `Presigned URL` | `Presigned URL`: app generates a time-limited redirect (private bucket OK). `Public ACL`: direct S3 URL (bucket must allow public reads). |
| `signed_url_expiry`        | `3600`          | Presigned URL expiry in seconds.                                                                                                          |
| `delete_from_s3`           | `1`             | Delete the S3 object when a File doc is trashed.                                                                                          |
| `include_in_native_backup` | `1`             | Stage S3 files locally during `bench backup --with-files`.                                                                                |
| `auto_push_after_migrate`  | `0`             | After `bench migrate` (typically right after `bench restore`), push local files back to S3.                                               |
| `delete_local_after_push`  | `1`             | Remove the local copy after uploading to S3.                                                                                              |
| `ignored_doctypes`         | —               | Newline-separated DocType names whose attachments stay on the local filesystem.                                                           |

---

## Architecture notes

### Why monkey-patch `BackupGenerator`?

Frappe core exposes **no** `before_backup`, `after_backup`, or `after_restore` hooks. `BackupGenerator.backup_files()` shells out `tar cf` against `public/files` and `private/files` ([backups.py:349](https://github.com/frappe/frappe/blob/develop/frappe/utils/backups.py#L349)); `extract_files()` shells out `tar xvf --strip 2` ([installer.py:757](https://github.com/frappe/frappe/blob/develop/frappe/installer.py#L757)). Neither calls into app code.

`scheduled_backup()` instantiates `BackupGenerator` directly with no injection point, so a custom command wouldn't capture cron-triggered backups. The only way to make `bench backup --with-files` "just work" is to wrap `BackupGenerator.backup_files` from inside the app's `__init__.py`, which Frappe imports during `frappe.init()` — well before any backup runs.

The patch:

- Saves the original method via `_ORIGINAL_BACKUP_FILES` so future patches can chain.
- Is idempotent (`_PATCHED` flag).
- Falls back to the original when `_s3_enabled_for_current_site()` returns False — so sites without the app installed, sites with `enabled=0`, and sites in mid-bootstrap (`DoesNotExistError`/`OperationalError`) are unaffected.

### Staging path

Backup staging writes each S3 file to `public/files/{key_basename}` or `private/files/{key_basename}`. The basename comes from `make_key()` and carries an 8-char hex token, so two uploads with the same user-visible `file_name` always stage to distinct paths. Both `backup._stage_s3_files()` and `migration._push_existing_key()` agree on this layout — the same function (`backup.local_staging_path`) is the single source of truth.

### Serve lookup

`api.serve(key)` does an **exact-match** `frappe.db.get_value("File", {"file_url": <encoded URL>}, "name")` — not a LIKE scan. This:

- Lets MySQL use any index on `file_url`.
- Avoids LIKE-wildcard interpolation issues if a key contained `%` or `_`.
- Is bounded O(1) instead of O(N) in the File table size.

### Module layout

```
s3_store/
├── __init__.py                 # installs backup patch as a side effect
├── hooks.py                    # write_file, delete_file_data_content,
│                               # after_migrate, commands
└── s3_store/
    ├── s3_client.py            # pure-function boto3 wrapper
    ├── file_handler.py         # write_file + delete hook implementations
    ├── api.py                  # serve(key), start_migration
    ├── migration.py            # bulk migrator + post-restore push
    ├── backup.py               # monkey-patch + staging logic
    ├── commands.py             # bench s3-store-push-local
    └── doctype/
        ├── s3_store_settings/
        └── s3_migration_log/
```

`s3_client.py` is pure functions taking `settings` as the last argument — trivially mockable, stateless at the module level.

---

## Testing

```bash
bench --site <site> run-tests --app s3_store
```

The suite covers:

- Settings validation (creds + connection check)
- `write_file` and delete-on-trash hook
- `serve` redirect, missing-key, permission-error paths
- Bulk migration: rewrite URL, delete local, error collection, log doc lifecycle
- Post-restore push: re-upload to existing key, error when staged file is missing
- Backup staging: cleanup on success, cleanup on exception, mixed-mode skip
- Patched `BackupGenerator` is a no-op when S3 disabled; `get_recent_backup` neutralises file tars
- URL parsing across virtual-host AWS, path-style MinIO, and the presigned serve endpoint

All boto3 calls are mocked — the suite never hits the network.

### End-to-end smoke test against MinIO

```bash
# Start MinIO locally
docker run -p 9000:9000 -p 9001:9001 \
    -e MINIO_ROOT_USER=admin -e MINIO_ROOT_PASSWORD=adminadmin \
    minio/minio server /data --console-address ":9001"

# Configure s3_store with endpoint_url=http://localhost:9000, create bucket
bench --site dev.s3.test install-app s3_store
# (set credentials + bucket via Desk)

# Upload some files via the UI, then:
bench --site dev.s3.test backup --with-files

# Wipe and restore
bench drop-site dev.s3.test
bench --site dev.s3.test restore <db> \
    --with-public-files <files.tar> --with-private-files <priv-files.tar>
bench --site dev.s3.test s3-store-push-local
```

After the push, the local `public/files/` and `private/files/` directories should contain only Frappe's own non-S3 files; the bucket should hold every uploaded attachment under its original key.

---

## Troubleshooting

**`S3 connection check failed: An error occurred (403) when calling the HeadBucket operation`**
Wrong credentials, wrong region, or the IAM user/policy doesn't grant `s3:ListBucket` on the configured bucket. The same policy needs `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `arn:aws:s3:::<bucket>/*`.

**`Another s3_store backup is already running for this site.`**
A previous backup crashed without releasing the lock. Check for hung tar processes; delete `sites/<site>/.s3_store_backup.lock` if confirmed stale.

**`Insufficient disk space to stage S3 files for backup`**
Total of all S3 file sizes for the site exceeds 90% of free disk space. Either free up space or set `include_in_native_backup = 0` in S3 Store Settings (you'll lose the self-contained-tar property).

**Backup tar is missing some files**
Check the Error Log for `"S3 Store backup staging"` — individual download failures don't abort the backup, they're logged and skipped. The File doc is preserved so you can investigate.

**`bench s3-store-push-local` reports `"staged file missing"`**
The File doc points at an S3 key, but no local copy was found at the expected staging path. Either (a) the file wasn't included in the backup tar (the source backup was made without `--with-files`), or (b) it was unpacked under an unexpected name (file_name was renamed in the source). The S3 object, if it exists, is unaffected.

---

## Limitations

- **`bench backup --exclude-doctypes File`** is incompatible: the DB dump won't reference the staged files. There's no clean way to detect this from the patch site.
- **External `file_url` (e.g. Google Drive thumbnails)** is ignored by both the backup-staging path and the post-restore push — those URLs aren't under our control.
- **Mixed-mode**: if a file exists _both_ locally and on S3, the local copy wins during backup staging. After restore, the file at the staging path is pushed up to S3 with the existing key. This is intentional — local-wins makes recovery scenarios predictable.
- **Patch ordering**: if another app monkey-patches `BackupGenerator.backup_files` _after_ `s3_store`, our patch is lost. Both patches must chain via the saved original to coexist; `s3_store` does so.
- **`File.file_url` index**: the `serve` endpoint uses exact-match lookups, so it benefits from an index on `file_url`. Frappe doesn't ship one by default; on sites with millions of File rows, consider adding `idx_file_file_url` via a custom DB patch.

---

## License

MIT
