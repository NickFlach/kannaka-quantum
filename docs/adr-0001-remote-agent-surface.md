# ADR-0001 — Remote-agent & SSH surface: trust model and mitigations

- Status: Accepted
- Date: 2026-07-01
- Scope: `kannaka_quantum/ssh_bridge.py`, `kannaka_quantum/lab.py` (the
  `lab_ssh_configure` → `lab_agent_setup` → `lab_agent_launch/list/read/send`
  chain), and the credentials those paths touch.

## Context

`kannaka-quantum` is the highest-consequence bridge in the Kannaka fleet: it can
spend real QPU credits and it can **run a coding agent on a remote cloud
instance over SSH**. The remote-agent feature (v0.2.x) lets the local Kannaka
agent provision a qBraid on-demand instance, upload an Anthropic API key, launch
an autonomous `claude` agent on it, and drive it. That is a large amount of
authority to hand to a process on a machine we do not fully control, so this ADR
writes down what the surface actually is, what a compromise reaches, and what we
do about it.

## The surface

```
lab_provision_instance ──▶ a paid qBraid on-demand instance (per-minute billing)
lab_ssh_configure      ──▶ qBraid writes a local SSH config (alias, ProxyCommand,
                           IdentityFile) + on Windows we harden it (_harden_windows_ssh)
lab_agent_setup        ──▶ uploads the Anthropic API key to the instance and
                           pre-accepts Claude Code onboarding + bypass-permissions
lab_agent_launch/…     ──▶ starts / reads / sends to a claude agent on the instance
```

Transport: SSH runs **through a WebSocket (`wss://`) ProxyCommand tunnel** to a
qBraid-hosted endpoint, authenticated by a qBraid bearer token. `ssh_bridge.py`
is a Windows-safe reimplementation of qBraid's stdio⇄WebSocket bridge (qBraid's
own bridge crashes under the Windows Proactor event loop). So the authenticity
of the channel rests primarily on **TLS + the qBraid token**, not on SSH
host-key verification.

### Credentials in play

| Secret | Sourced from | Lands where |
| --- | --- | --- |
| qBraid API key | `QBRAID_API_KEY` env → `~/.qbraid/qbraidrc` → `~/Downloads/QBraid.txt` | local only |
| qBraid SSH/WebSocket token | qBraid-generated SSH config (`ProxyCommand … --token`) | local SSH config file + bridge process argv |
| SSH private key | qBraid-generated (`IdentityFile`) | local file |
| Anthropic API key | `api_key` arg → `ANTHROPIC_API_KEY` → `~/.kannaka/config.toml [llm]` | **uploaded to the remote instance** (`~/.claude/anthropic_key`, `0600`) |
| OpenQuantum client id/secret | `OPENQUANTUM_CLIENT_ID/_SECRET` env → `OPENQUANTUM_SDK_KEY` json → `~/.openquantum/sdk-key.json` → `~/Downloads/sdk-key-*.json` | local only |

## Trust model

- **We trust:** the local machine, the qBraid control plane and its TLS
  endpoint, and the qBraid token/SSH key at rest (protected by file
  permissions — see mitigations).
- **We do NOT fully trust:** the remote instance once an autonomous third-party
  agent runs on it. `lab_agent_setup` deliberately pre-accepts Claude Code's
  bypass-permissions mode so the launched agent runs without prompts. A
  prompt-injected or buggy remote agent therefore has, on that instance:
  - the uploaded **Anthropic API key** (⇒ can spend on the Anthropic account
    until the key is rotated), and
  - whatever the instance itself can reach on the network / in its own
    filesystem.
- **Blast radius is bounded to the instance + the uploaded key.** The remote
  agent has no path back to the local machine except the text we choose to read
  with `lab_agent_read`. That read output is untrusted input to the local
  orchestrator (a prompt-injection vector for *this* agent) and should be
  treated as data, not instructions.

## Findings

1. **Anthropic key is uploaded to a third-party instance and the remote agent
   runs unattended (bypass-permissions).** By design for autonomy, but it is the
   single largest risk. Mitigation is operational: use a **scoped / short-lived
   key**, rotate after a session, and terminate the instance when done.
2. **qBraid disables SSH host-key checking.** The generated alias config sets
   `StrictHostKeyChecking no` + `UserKnownHostsFile /dev/null` (reasonable for
   ephemeral instances, but it means a swapped endpoint is not detected at the
   SSH layer). Fixed for the one command we own — see mitigation (b).
3. **The WebSocket token is exposed in argv.** qBraid's ProxyCommand passes the
   token as `--token <value>`, so it appears in the bridge process command line
   (`ps` / Task Manager) and in the SSH config file at rest. We cannot change
   the qBraid-generated ProxyCommand without risking the (live-verified) tunnel,
   so this is only partially mitigated — see (a), (c).
4. **(Positive) Secrets already avoid argv on the paths we control.**
   `_remote_ssh_py` passes the API-key payload over **stdin**, `shlex.quote`s the
   remote `python3 -c` script, and never uses `shell=True`; the key file is
   written `0600`; and `lab_agent_setup`'s return echoes only the `apiKeyHelper`
   *path*, never the key. These are the right patterns and now have regression
   tests.

## Decisions / mitigations (this change)

- **(a) Env alternative to `--token`.** `ssh_bridge` now reads
  `KANNAKA_SSH_BRIDGE_TOKEN` when `--token` is absent
  (`_resolve_bridge_token`), so a direct invocation of the bridge can keep the
  token off the command line. The qBraid-generated ProxyCommand still uses
  `--token`; that residual is documented, not eliminated.
- **(b) Restore host-key pinning on the command we control.** `_remote_ssh_py`
  (the SSH used by `lab_agent_setup`) now overrides the alias config with
  `StrictHostKeyChecking=accept-new` against a **persistent** known_hosts under
  the kannaka data dir, keyed per-instance via `HostKeyAlias`. Effect: an
  instance's key is pinned on first contact (TOFU) and a *changed* key for the
  same instance is refused, without cross-instance false positives. This is
  defense-in-depth on top of the TLS tunnel; it does **not** cover qBraid's own
  AgentLauncher SSH calls (`remote_launch/list/read/send`), which remain at
  qBraid's `no` default — a known gap accepted because the tunnel is
  TLS-authenticated.
- **(c) At-rest protection for the SSH config + key.** On Windows,
  `_harden_windows_ssh` already resets the config/identity ACLs to the current
  user only (`icacls /inheritance:r /grant:r <user>:F`); on POSIX, OpenSSH
  itself refuses group/world-readable config/key files. This bounds the at-rest
  exposure of finding 3 to the local user account.
- **(d) Spend can't run in CI or by accident.** Orthogonal but part of the same
  safety floor: every credit-spending path refuses without an explicit
  `allow_spend` / `KANNAKA_QUANTUM_ALLOW_SPEND` / `KANNAKA_LAB_ALLOW_SPEND`
  opt-in, and CI carries no provider secret. Covered by the spend-guard tests.

## Operator guidance

- Prefer a dedicated, rotatable Anthropic key for remote agents; rotate it after
  the session and **terminate** the instance (a stopped instance still bills
  `stopped_credits_per_min` for disk).
- Treat `lab_agent_read` output as untrusted data.
- If an instance legitimately rotates its SSH host key under the same id, prune
  its entry from `<KANNAKA_DATA_DIR or ~/.kannaka>/known_hosts`.
- On a shared host, set `KANNAKA_SSH_BRIDGE_TOKEN` rather than passing the token
  on a command line, and rely on the config-file ACLs for the qBraid-generated
  ProxyCommand.

## Consequences

- No behavioral change to the happy path: the free simulator, the paid-compute
  spend guards, and the live-verified remote-agent flow all still work; the
  host-key override only *adds* a refusal on a changed key.
- Residual accepted risk: the qBraid-generated ProxyCommand token in argv/config
  (bounded by ACLs), and qBraid's internal AgentLauncher SSH calls not pinning
  host keys (bounded by the TLS tunnel).

---

## Amendment — Wave 2 (2026-07-02): instance leases (T4.1) + scoped keys (T4.2)

Wave 2 implements two mitigations this ADR called for. Both live in `lab.py`.

### T4.1 — Instance leases (runaway-billing GATE)

Per-minute instance/server billing is the same hazard class as the per-minute
QPUs the circuit bridge refuses outright: a forgotten instance drains the budget
silently. The lease mechanism extends the spend-safety doctrine to compute.

- **Lease ledger** — `<KANNAKA_DATA_DIR or ~/.kannaka>/leases.jsonl`, append-only
  JSONL. Current state is the merge-fold of records per `instance_id` (last write
  per field wins), so a later partial record — a reap, a key fingerprint — updates
  without dropping the rest. A lease record: `instance_id`, `kind`
  (`"instance"`|`"server"`), `profile`, `cluster`, `ssh_alias`, `created_at`,
  `max_minutes` (default **60**), `expires_at`, `status`
  (`"active"`|`"reaped"`), `event`, and (after key setup) `key_fingerprint`.
- **Recorded on every paid start** — `lab_provision_instance`, `lab_compute_up`,
  and `lab_start_instance` write a lease (`--max-minutes` overrides the 60-min
  default).
- **`lab-reap`** (cron/systemd-timer friendly) stops anything past its
  `expires_at`: `stop_bma_instance` for instances, `stop_server` for the Lab
  server; then appends a `status: "reaped"` record. `--dry-run` reports without
  stopping.
- **Launch GATE** — `lab_agent_launch` refuses an `ssh_alias` with no *active*
  lease (`allow_unleased=True` / `--allow-unleased` is the deliberate override).
  An unleased instance is precisely one nothing will auto-stop.
- **Live-verification note:** reap's stop path is unit-tested against a mocked
  compute client (expired lease ⇒ `stop_*` called; fresh lease untouched; server
  vs instance branch). Verifying against a *real* expired instance is deferred to
  the first T4.3 lifecycle (which provisions under its own budget) — we do not
  provision a paid instance solely to kill it.

### T4.2 — Scoped per-instance Anthropic keys

The uploaded key on a bypass-permissions remote agent is this ADR's largest
blast radius. Mitigations:

- **Same-as-primary refusal** — `lab_agent_setup` refuses a key identical to the
  operator's primary `ANTHROPIC_API_KEY` unless `i_know=True` / `--i-know`. The
  default path forces a *scoped* key onto third-party compute.
- **Fingerprint, never the key** — the uploaded key's `sha256:<12 hex>`
  fingerprint is recorded against the instance's lease; the raw key is never
  written to local disk (only uploaded to the instance over stdin, 0600).
- **`lab-agent-teardown`** deletes the remote key file (`~/.claude/anthropic_key`)
  and scrubs the `apiKeyHelper` reference, clears the lease fingerprint, and
  prints a rotation reminder.
- **Recommended issuance path — Admin-API workspace keys.** For per-instance
  keys, mint a **workspace-scoped** key via the Anthropic **Admin API**
  (`/v1/organizations/api_keys`, an `sk-ant-admin…` admin key creates keys bound
  to a dedicated workspace) rather than reusing an operator key. A workspace key
  can be revoked and budget-capped independently, so teardown + revoke bounds the
  blast radius to that one instance's workspace. Treat every uploaded key as
  compromised on teardown and revoke/rotate it.

### Residual (Wave 2)

- Leasing is advisory local bookkeeping: an instance provisioned outside this
  tool has no lease, so `lab-reap` won't see it and `lab_agent_launch` refuses it
  (override with `--allow-unleased`). `lab-reap` must be scheduled (cron/timer) to
  enforce leases — it is not a daemon.
- Key teardown removes the on-instance material but cannot itself revoke the key
  upstream; revocation/rotation remains an operator step (hence the reminder).
