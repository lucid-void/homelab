# Proton Mail Bridge — decisions

Proton Mail is end-to-end encrypted, so there is no IMAP server to point
Paperless at: mail is only decryptable by a client holding the account's PGP
keys. Proton Mail Bridge is that client. It logs into the account, decrypts
locally, and re-serves the mailbox as a **local** IMAP/SMTP server. Paperless
then consumes ordinary IMAP.

Lives in the `paperless` namespace because it exists solely for Paperless, but
it is a separate Flux Kustomization and a separate Deployment: neither app
depends on the other's Kustomization, and either can be down without the other
noticing (Paperless just stops finding new mail).

## Image: `ghcr.io/videocurio/proton-mail-bridge`, not `shenxn/protonmail-bridge`

Proton ships no official container. `shenxn/protonmail-bridge` is the image
almost every guide names (~690 stars) and it is the **wrong** choice as of
2026-08: its CI has been red for over a year. `VERSION` in that repo tracks
upstream (v3.26.0 on 2026-08-27) but **both** publish workflows fail on every
bump, so the newest image actually pushed to Docker Hub is `3.19.0-build`,
dated **2025-04-02**. The repo looks maintained — commits land monthly — while
the registry has been frozen for ~16 months. Check the *registry tags*, never
the commit log, before trusting it again.

VideoCurio's image is a source build of the same upstream, published to GHCR
with immutable `vX.Y.Z` tags (`v3.24.2`, 2026-04). Pinned to the exact tag per
the repo-wide image policy; Renovate can follow the `v`-prefixed semver.

Bridge talks to Proton's API and Proton deprecates old clients, so a stale
bridge eventually stops authenticating. **Bridge version is a liveness
dependency, not just a CVE concern** — if login starts failing, check the age of
the pinned tag first.

Note: bridge login **requires a paid Proton plan**. Free accounts cannot use it.

## Storage: `openebs-hostpath`, and not backed up

`/root` holds the gpg/`pass` keyring, the encrypted vault, and gluon's IMAP
state database. Gluon is the IMAP server library Proton wrote for bridge v3 and
it is **SQLite** (`ProtonMail/gluon` pulls in `mattn/go-sqlite3`), with a
write-heavy message-literal cache beneath it. SQLite on NFS is the classic
silent-corruption setup, so this follows the Minecraft-world precedent: local
disk, node-pinned, acceptable because all three control planes are VMs on one
Proxmox host.

Deliberately **excluded from backups**. The volume is credentials plus a cache
of mail that already lives at Proton; recovery is a re-login, and shipping an
auth vault to Filen would be strictly worse than not having it. `openebs-hostpath`
cannot expand in place, so it is sized (20Gi) for the mailbox up front — growing
it means deleting the PVC and re-running the bootstrap.

`strategy: Recreate` and `replicas: 1` are both load-bearing: two bridges on one
vault race on the gluon database, and a RollingUpdate would deadlock on the RWO
volume while the old pod kept serving.

## The certificate is the whole problem

Everything awkward about this deployment comes from one upstream fact.

Bridge serves IMAP over STARTTLS with a self-signed certificate.

**It does also accept plaintext.** The pre-TLS banner advertises
`AUTH=PLAIN … STARTTLS`, and a mail account set to "No encryption" logs in and
fetches mail fine. An earlier version of this document claimed no plaintext
option existed; that came from secondary sources rather than from testing the
running server, and it is wrong. Verify for yourself:

```bash
kubectl exec deploy/paperless-app -n paperless -c app -- python -c \
  "import socket; s=socket.create_connection(('127.0.0.1',1143),5); print(s.recv(200))"
```

Plaintext is **not** used here. It would put the bridge password and every
message on the pod network in clear for no benefit, when STARTTLS with the
pinned certificate already works. But it matters that the reason for the socat
sidecar is the certificate's SAN, below — *not* an inability to avoid TLS.

Worse, that certificate carries exactly **one SAN: `IP:127.0.0.1`**. From
`internal/certs/tls.go`: `CommonName: "127.0.0.1"`, `IPAddresses:
{127.0.0.1}`, **no DNS names**, `IsCA: true`, 20-year validity. It is generated
once and stored **inside the encrypted vault**, not as a file on disk — so there
is no `cert.pem` for a sidecar to scrape.

Paperless builds its context with `ssl.create_default_context()`
(`paperless_mail/mail.py`), where `check_hostname` is `True` and not
configurable. That means:

- Trusting the certificate is only **half** the job. `PAPERLESS_EMAIL_CERTIFICATE_LOCATION`
  fixes *trust*; it does nothing for *hostname verification*.
- Connecting to `protonmail-bridge:143` fails verification no matter what CA is
  trusted, because the name is not in the certificate.
- The address dialled must be **literally `127.0.0.1`**.

Hence the `bridge` socat sidecar in the Paperless pod. It listens on
`127.0.0.1:1143` and forwards to the bridge Service. It is a **plain TCP** relay
— it terminates no TLS, so STARTTLS is still negotiated end-to-end and the
certificate arrives untouched; its only job is to make the dialled address match
the one SAN. The mail account is therefore configured as host `127.0.0.1`, port
`1143`, security **STARTTLS**.

The certificate itself is exported once during bootstrap (`cert export` in the
bridge CLI) and committed as a **SealedSecret**, `app-sealed.yml`, alongside the
bridge. A certificate is public and needs no sealing on its own merits; it is
sealed so that everything an app is handed follows one convention, rather than
leaving a reader to work out which credential-shaped file is the exception.

Note `cert export` writes `key.pem` beside `cert.pem` — export to `/tmp` in the
throwaway bootstrap pod, never to `/root`, or the private key persists in
plaintext on the state volume beside the vault that exists to keep it encrypted.

Mounted `optional: true` via `type: custom`, because app-template's
`type: secret` volume schema has no `optional` field and without it Paperless
would sit in `ContainerCreating` whenever the Secret is absent. Mounted as a
directory rather than a `subPath` so the kubelet refreshes it in place — a
`subPath` mount never updates at all. **But an in-place refresh is not enough:
Paperless must still be restarted, see below.**

### A cert refresh needs a restart — the resolved path is cached

The mount refreshing in place does *not* mean the running process picks it up.
Paperless reads the location through `Path(os.environ[key]).resolve()`
(`src/paperless/settings/parsers.py`), and `.resolve()` dereferences the
kubelet's atomic-writer symlink chain — `cert.pem` → `..data` →
`..2026_08_29_20_20_47.1731907293/` — freezing the **timestamped** directory into
`settings.EMAIL_CERTIFICATE_FILE` for the life of the process.

Updating the Secret makes the kubelet write a **new** timestamped directory,
repoint `..data`, and **delete the old one**. The long-running process is then
holding an absolute path that no longer exists, and every fetch dies at
`ssl_context.load_verify_locations(cafile=settings.EMAIL_CERTIFICATE_FILE)` with
`FileNotFoundError: [Errno 2]` — surfacing in the UI as a failed mail-account
test with no hint that a *path*, rather than the certificate or the credentials,
is at fault.

Hence `reloader.stakater.com/auto: "true"` on the paperless **controller**
(`paperless/app/helmrelease.yml`) — on `controllers.app.annotations`, not
`pod.annotations`, which Reloader never reads.

The diagnosis is slippery because **every ad-hoc check passes while the server is
broken**: `manage.py check` and any `python -c` are new processes that re-resolve
`..data` to the current directory and see a valid file. `ls -lL` on the mount path
likewise succeeds. An IMAP `NOOP` probe also succeeds, since it needs no login.
Only the real fetch path fails. Verify with a `mailbox_login()` against the
account, never with the system check.

The certificate is valid **20 years** (the deployed one to 2046-08-24), so this
is close to a one-time step — but it is regenerated if the vault is ever
rebuilt, which means re-exporting and re-sealing.

### `tls: bad record MAC` in the bridge log means "client rejected my cert"

When Paperless cannot verify the certificate, the bridge logs:

```
ERRO Cannot upgrade connection  error="local error: tls: bad record MAC"  pkg=gluon/session
```

This reads like stream corruption — a mangled proxy, an MTU problem, something
eating bytes between the two socat hops — and it is none of those. It is
Gluon's server-side view of a client aborting the STARTTLS handshake after
rejecting the certificate. Confirmed by correlation: two probes that failed
verification produced exactly two of these lines; the probe that passed produced
none.

So when this appears, check trust, not the network. The fast triage is to run
the handshake three ways from inside the Paperless pod — with no CA, with the
bridge CA, and against the Service name — which separates "not trusted" from
"wrong hostname" from "genuinely unreachable" in one pass:

```bash
python -c "import ssl,imaplib; c=ssl.create_default_context(cafile='/etc/ssl/protonmail/cert.pem'); \
m=imaplib.IMAP4('127.0.0.1',1143); m.starttls(c); print(m.noop())"
```

`CERTIFICATE_VERIFY_FAILED: self-signed certificate` = the cert Secret is
missing or not mounted. `Hostname mismatch` = something is dialling a name
instead of `127.0.0.1`.

### The bootstrap is two-phase, and skipping that caused an outage

`PAPERLESS_EMAIL_CERTIFICATE_LOCATION` is **coupled to the cert SealedSecret**:
the two must land together, and if the SealedSecret is ever removed the variable
must go with it. It was originally shipped ahead of the certificate and that
caused an outage.

This is not tidiness. Paperless registers a Django system check
(`paperless/checks.py::_email_certificate_validate`) that emits an **`Error`** —
`Email cert <path> is not a file` — whenever the variable is set and the path is
absent. An Error-level system check aborts startup with `SystemCheckError`, so
the container exits within seconds, never goes Ready, the Helm upgrade times out
on `Deployment/paperless-app status: 'InProgress'`, and Flux rolls the release
back. Present in 3.0.5 and 3.1.0 alike — this is not a version regression.

A second, quieter trap sits next to it: `**/*secret.yml` is gitignored, so
naming the *sealed* output `app-secret.yml` means `git add` accepts it, the file
never reaches the remote, Flux never creates the Secret, and mail fails to fetch
with no error anywhere in git. Commit `app-sealed.yml`.

The trap is that `optional: true` looks like it solves the ordering problem and
does not. It governs the **kubelet**, which will happily start a pod with an
absent optional Secret; it has no bearing on whether the *application* agrees
to boot. Reading `settings/__init__.py` alone reinforces the illusion —
`get_path_from_env` does no existence validation whatsoever, so the path looks
inert until it is read at mail-fetch time. The validation lives in a completely
separate file. **Check `checks.py`, not just `settings.py`, before assuming a
paperless env var is lazily evaluated.**

Reproduce in one line, without touching the deployment:

```bash
kubectl exec deploy/paperless-app -n paperless -c app -- \
  env PAPERLESS_EMAIL_CERTIFICATE_LOCATION=/nope python manage.py check
```

One further consequence worth knowing: `load_verify_locations(cafile=...)`
**replaces** the default trust store rather than adding to it. Once this
variable is set, a second mail account pointed at a publicly-trusted IMAP server
would stop verifying. Only the bridge is expected here.

**Rejected alternative:** bridge has a `cert import` CLI command that takes a
custom cert/key path (stored in the vault as `CustomCertPath`, re-read at
startup). A cert-manager certificate with a real DNS SAN would remove both the
socat sidecar and the committed certificate, and let Paperless dial the Service
name. It was not taken because it needs a new self-signed CA issuer, a mounted
keypair, and a Reloader hook to restart the bridge on renewal — a lot of moving
parts to delete one 5 MB sidecar. Revisit if the sidecar becomes a nuisance.

## `PAPERLESS_EMAIL_ALLOW_INTERNAL_HOSTS`

Paperless 3.x resolves the mail host and refuses to connect if it lands on a
non-public IP (`"Connection blocked: … resolves to a non-public address"`). The
bridge is `127.0.0.1`, i.e. the most non-public address there is. It **defaults
to `true` in 3.0.5** so nothing is broken today, but it is set explicitly
because it is exactly the kind of default that tightens in a later release, and
the failure would look like a mail problem rather than a policy one.

## Login is interactive and cannot be GitOps'd

Proton login needs an account password and a 2FA code typed by a human, so
there is no Job that can do this — unlike the Zitadel/Gotify/Kavita bootstraps.
It is a one-time manual procedure (RUNBOOK → "Bootstrap Proton Mail Bridge"),
re-run only if the vault is lost.

Two traps in that procedure:

1. Only one bridge instance may hold the vault, and the container's entrypoint
   **starts one automatically**. Logging in means stopping the Deployment first,
   not exec'ing into the running pod — `pkill bridge` inside the live pod ends
   the entrypoint pipeline and kills the container out from under the exec
   session.
2. Flux reverts a `kubectl scale` on its next reconcile (30m), so the HelmRelease
   must be **suspended** for the duration.

## Ports

The bridge binds only `127.0.0.1` (1143 IMAP / 1025 SMTP); the image's entrypoint
runs socat to republish those on the pod IP as `:143` / `:25`. `CONTAINER_*` is
socat's listen side and `PROTON_BRIDGE_*` is the bridge's own — **they must stay
different values or socat forwards to itself**.

The Service publishes **IMAP only**. The container also listens on `:25`, but
nothing here sends mail through Proton, so it is left unpublished.

Readiness probes the *bridge's* listener (`netstat … 127.0.0.1:1143`), not
socat's `:143` — socat binds immediately and accepts connections whether or not
the bridge behind it is up, so a TCP probe on `:143` is green from second one
and tells you nothing. There is no liveness probe: the entrypoint is
`cat faketty | bridge --cli`, so if the bridge exits the pipeline ends and the
container restarts on its own, and a liveness probe would only risk killing a
long initial sync.
