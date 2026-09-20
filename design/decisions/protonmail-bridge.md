# Proton Mail Bridge

**Read before editing:** `kubernetes/apps/paperless/protonmail-bridge/`, `kubernetes/apps/paperless/paperless/`, `kubernetes/apps/keycloak/keycloak/`

## Current state

Proton Mail is end-to-end encrypted, so Paperless can't read it over plain IMAP
directly. Proton Mail Bridge logs into the account, decrypts locally, and re-serves
the mailbox as a local IMAP/SMTP server Paperless consumes as ordinary IMAP. Lives in
the `paperless` namespace but as its own Flux Kustomization and Deployment — neither
depends on the other's Kustomization.

Image is `ghcr.io/videocurio/proton-mail-bridge`, **not** `shenxn/protonmail-bridge`
(that repo's publish workflow is broken despite active commits — check the *registry
tags*, not the commit log, before trusting an image repo). VideoCurio publishes a
source build of upstream to GHCR with immutable `vX.Y.Z` tags, pinned per the
repo-wide image policy. Login requires a **paid Proton plan** — free accounts cannot
use it.

Storage is `openebs-hostpath` and **deliberately excluded from backups** — `/root`
holds the gpg/`pass` keyring, the encrypted vault, and gluon's SQLite IMAP state
database; recovery is a re-login, and shipping an auth vault to Filen would be worse
than not having it. Cannot expand in place, sized (20Gi) up front. `strategy: Recreate`
+ `replicas: 1` are load-bearing — two bridges racing on the gluon database would
deadlock a RollingUpdate on the RWO volume.

Bridge serves IMAP over STARTTLS with a self-signed certificate carrying exactly
**one SAN, `IP:127.0.0.1`** (no DNS names), generated once and stored inside the
encrypted vault — there is no `cert.pem` file on disk for a sidecar to scrape. It also
accepts plaintext (`AUTH=PLAIN … STARTTLS`), but plaintext is not used here — STARTTLS
with the pinned certificate already works, and plaintext would put the password and
every message on the pod network in clear for no benefit.

**The single `IP:127.0.0.1` SAN forces every in-cluster consumer onto a loopback
relay.** Trusting the certificate is only half the job: the dialled address must be
literally `127.0.0.1`, which no Service name is. So each consumer runs its own socat
sidecar that listens on loopback and forwards to the bridge Service as a plain TCP
relay — terminating no TLS, so STARTTLS still negotiates end-to-end.

| Consumer | Sidecar listens on | Forwards to | How it is declared |
|---|---|---|---|
| Paperless (IMAP) | `127.0.0.1:1143` | `protonmail-bridge:143` | `bridge` container in the pod spec |
| Keycloak (SMTP) | `127.0.0.1:1025` | `protonmail-bridge:25` | `Keycloak.spec.unsupported.podTemplate` |

Paperless's half of this is forced by `ssl.create_default_context()` with
non-configurable `check_hostname = True` (`PAPERLESS_EMAIL_CERTIFICATE_LOCATION`
handles only trust). Its mail account is host `127.0.0.1`, port `1143`, STARTTLS.

Keycloak's sidecar **must be a native sidecar** (`initContainers` with
`restartPolicy: Always`). The operator applies `unsupported.podTemplate` to the
realm-import Job as well, where an ordinary socat container never exits and wedges the
Job at 1/2 forever. Keycloak's realm points at `127.0.0.1:1025`, `starttls: true`, and
trusts the certificate through `Keycloak.spec.truststores`.

The certificate is exported once during bootstrap (`cert export` in the bridge CLI)
and committed as the `app-sealed.yml` SealedSecret, mounted `optional: true` via
`type: custom` (app-template's `type: secret` schema has no `optional` field) as a
directory, not a `subPath` (a `subPath` mount never refreshes). Valid 20 years — close
to a one-time step — but regenerated (re-export, re-seal) if the vault is rebuilt.

**Login is interactive** (account password + 2FA) **and cannot be a Job** — a
one-time manual procedure (design/runbook.md → "Bootstrap Proton Mail Bridge"),
re-run only if the vault is lost.

Ports: the bridge binds only `127.0.0.1` (1143 IMAP / 1025 SMTP); the entrypoint runs
socat to republish those on the pod IP as `:143`/`:25`. The Service publishes both —
`:143` for Paperless, `:25` for Keycloak's realm mail.

**Proton only sends from an address it owns.** It rejects anything else at DATA, not
at MAIL FROM, with `554 5.0.0 Error: The sender or recipient address is not valid` —
so an SMTP connection test passes while every real mail silently fails. Adding
`blackcats.cc` as a Proton custom domain would allow a service `from:` address;
without it the account's own address is the only valid sender.

## Rules

- **Treat the bridge image tag as a liveness dependency, not just a CVE concern** —
  Proton deprecates old API clients, so a stale bridge eventually stops
  authenticating; if login starts failing, check the age of the pinned tag first.
- **An in-place refresh of the cert Secret is not enough — Paperless must be
  restarted too.** Paperless resolves the certificate path with
  `Path(os.environ[key]).resolve()`, which dereferences the kubelet's atomic-writer
  symlink chain and freezes the *timestamped* directory into
  `settings.EMAIL_CERTIFICATE_FILE` for the process's life. Updating the Secret makes
  the kubelet write a new timestamped directory and delete the old one, so the running
  process holds a path that no longer exists and every fetch dies with
  `FileNotFoundError`. Hence `reloader.stakater.com/auto: "true"` on the Paperless
  **controller** (`paperless/app/helmrelease.yml`, on `controllers.app.annotations`,
  not `pod.annotations`).
- **Diagnose a cert-refresh failure with `mailbox_login()`, never a system check** —
  `manage.py check`, `python -c`, `ls -lL` and an IMAP `NOOP` all re-resolve `..data`
  fresh and pass even when the real fetch path is broken.
- **`tls: bad record MAC` in the bridge log means "client rejected my certificate,"
  not stream corruption** — it is Gluon's server-side view of a client aborting the
  STARTTLS handshake after failing verification. Triage from inside the Paperless pod
  with no CA, the bridge CA, and the Service name in turn:
  ```bash
  python -c "import ssl,imaplib; c=ssl.create_default_context(cafile='/etc/ssl/protonmail/cert.pem'); \
  m=imaplib.IMAP4('127.0.0.1',1143); m.starttls(c); print(m.noop())"
  ```
  `CERTIFICATE_VERIFY_FAILED: self-signed certificate` means the cert Secret is
  missing or unmounted; `Hostname mismatch` means something is dialling a name instead
  of `127.0.0.1`.
- **`PAPERLESS_EMAIL_CERTIFICATE_LOCATION` must land in the same change as the cert
  SealedSecret, and be removed with it** — Paperless's `_email_certificate_validate`
  system check emits an `Error` (`Email cert <path> is not a file`) whenever the
  variable is set and the path is absent, aborting startup with `SystemCheckError`:
  the container never goes Ready, the Helm upgrade times out, and Flux rolls back.
- **Name the sealed output `app-sealed.yml`, never `app-secret.yml`** —
  `**/*secret.yml` is gitignored, so a sealed file matching that glob is silently
  never pushed and mail fetch fails with no error anywhere in git.
- **`optional: true` only governs the kubelet, not the application** — a pod starts
  fine with an absent optional Secret, but Paperless's own startup check
  (`checks.py`, not `settings.py`) still aborts if the env var points nowhere.
  Reproduce with:
  ```bash
  kubectl exec deploy/paperless-app -n paperless -c app -- env PAPERLESS_EMAIL_CERTIFICATE_LOCATION=/nope python manage.py check
  ```
- **Setting `PAPERLESS_EMAIL_CERTIFICATE_LOCATION` replaces Paperless's default trust
  store rather than adding to it** — a second mail account on a publicly-trusted IMAP
  server would stop verifying. Only the bridge is expected here.
- **`cert export` writes `key.pem` beside `cert.pem` — export to `/tmp` in the
  throwaway bootstrap pod, never to `/root`**, or the private key persists in
  plaintext on the state volume beside the vault that exists to keep it encrypted.
- **Set `PAPERLESS_EMAIL_ALLOW_INTERNAL_HOSTS` explicitly, even though the current
  default already allows it** — Paperless 3.x refuses a mail host resolving to a
  non-public IP, and `127.0.0.1` is about as non-public as it gets. Pinning it guards
  against the default tightening later, when the failure would look like a mail
  problem rather than a policy one.
- **Only one bridge instance may hold the vault, and the entrypoint starts one
  automatically** — log in by stopping the Deployment first, not exec'ing into the
  running pod (`pkill bridge` inside a live pod just kills the container out from
  under the exec session). Flux reverts a `kubectl scale` on its next reconcile, so
  the HelmRelease must be **suspended** for the duration of a login.
- **`CONTAINER_*` (socat's listen side) and `PROTON_BRIDGE_*` (the bridge's own) must
  stay different values** — equal values make socat forward to itself.
- **Readiness must probe the bridge's own listener (`netstat … 127.0.0.1:1143`), not
  socat's `:143`** — socat binds immediately and accepts connections whether or not
  the bridge is up, so a TCP probe on `:143` is green from the first second and proves
  nothing. There is no liveness probe: the entrypoint is `cat faketty | bridge --cli`,
  so a bridge exit ends the pipeline and restarts the container on its own — a
  liveness probe would only risk killing a long initial sync.
