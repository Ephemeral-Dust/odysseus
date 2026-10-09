# Design & Scope — Files Explorer, Terminal (Command Runner), SSH File Access

Status: Proposed (not yet implemented)
Last updated: 2026-10-01
Target branch: local/instance (fork: Ephemeral-Dust/odysseus; upstream: odysseus-dev/odysseus)

## Summary

Three related capabilities, all admin-gated and filesystem/shell-scoped:

1. **Files Explorer** — browse/view/edit the local filesystem (bare metal or the
   container), and attach a file to a chat or add it to a library.
2. **Terminal (command runner)** — run one-shot commands on the instance via a
   dedicated UI, reusing the existing `/api/shell/exec` + `/api/shell/stream`
   (Option B). No persistent PTY; guardrails for environments where it can't work.
3. **SSH file access** — browse/read/write files on another machine over SSH and
   apply the same "add to chat/library" actions. (Interactive SSH terminal is
   deferred.)

## Background (existing building blocks)

- `routes/workspace_routes.py` — `/api/workspace/browse` + `/vet` (dirs-only,
  admin-gated, realpath + `vet_workspace`). Closest thing to an explorer today.
- `src/agent_tools/filesystem_tools.py` — `read_file`/`write_file`/`edit_file`/
  `apply_patch`/`grep`/`glob`/`ls` with skip-dirs, sensitive-path deny lists,
  and confinement.
- `src/tool_security.py` — `NON_ADMIN_BLOCKED_TOOLS` (file tools admin-only),
  `owner_is_admin_or_single_user`.
- `src/tool_execution.py:vet_workspace` — workspace/sensitive-root validation.
- `routes/shell_routes.py` — `/api/shell/exec` + `/api/shell/stream` (SSE: pipe/
  PTY/tmux/Windows detached), admin-only. SSH arg builders already present
  (`_ssh_base_argv`).
- `core/platform_compat.py` — `_ssh_exec_argv`, `find_bash`, `IS_WINDOWS`.
- `routes/_validators.py` — `validate_remote_host`, `validate_ssh_port`.
- `routes/cookbook_routes.py` + `cookbook_helpers.py` — existing SSH model
  serving (host/port/platform), `/api/cookbook/ssh-key`, `/api/cookbook/test-ssh`.
- `src/host_docker_access.py` — `running_in_container()`.
- Frontend: raw ES modules, `static/app.js` coordinator, `static/js/panels.js`
  lazy loader, `static/js/settings/` registry, `static/sw.js` precache.

Upstream prior art: odysseus-dev/odysseus issue #4851 / PR #4866 ("Nextcloud
Files explorer + agent tools") was closed `not_planned`; it provides the
file/setting/tool/tests shape we adapt (for local FS + SSH, not WebDAV).

## Goals

- Admin UI to browse/read/edit the filesystem of the running instance.
- "Add to chat" (attach file to composer) and "Add to library" (import into
  Documents/RAG personal docs) actions on any browsed file.
- A safe one-shot command runner with clear degraded behavior per environment.
- SSH host profiles + remote browse/read/write with the same add-to-* actions.

## Non-goals (this change)

- Interactive PTY terminal / xterm.js (deferred — Option A).
- SSH interactive terminal (deferred).
- New agent tools (`files_*`/`ssh_*`) — MVP is user-driven; agent tool exposure
  is a follow-up.
- WebDAV/Nextcloud/S3/FTP integrations.
- Non-admin access (everything here stays admin-only, matching file/shell tools).

## Feature 1 — Files Explorer (local)

### Backend — new `routes/files/files_routes.py` (+ top-level `routes/files_routes.py` shim)

Admin-gated with `_require_admin`/`owner_is_admin_or_single_user`; all paths go
through `os.path.realpath` + the existing sensitive-dir/symlink deny lists.

- `GET  /api/files/browse?path=` — list entries (files + dirs) with type, size,
  mtime; dirs marked "selectable" via `vet_workspace`. Caps at a fixed max
  (mirror `_MAX_BROWSE_DIRS`). Hidden entries omitted (consistent with workspace).
- `GET  /api/files/read?path=` — return text content (capped by `MAX_READ_CHARS`);
  binary/oversize → explicit error (no accidental base64 dump).
- `POST /api/files/write` — `{path, content}`; requires explicit overwrite flag.
- `POST /api/files/edit` — `{path, old, new}` (or unified-diff) → `edit_file`
  semantics; fail-closed on no match.
- `POST /api/files/mkdir`, `POST /api/files/rename`, `POST /api/files/delete` —
  delete requires confirmation; refuse on filesystem roots/sensitive paths.
- `POST /api/files/add-to-chat` — return a reference the composer can attach
  (reuse the upload pipeline: fetch bytes → `fileHandler.addFiles`, or a path
  reference that chat resolves via `read_file`).
- `POST /api/files/add-to-library` — import/copy into Documents/personal_docs
  (owner-scoped, via `PersonalDocsManager`) so RAG can index it.

Reuse helper logic from `src/agent_tools/filesystem_tools.py` (confinement,
sensitive patterns, read caps) rather than re-implementing.

### Frontend — new `static/js/files.js`

- Breadcrumb tree/list pane + a read-only text viewer (reuse the document editor
  viewer where sensible), following the existing visual language (CSS vars, Fira
  Code, inline SVG, no emoji).
- Per-entry actions: Open/View, Edit, Copy path, Add to chat, Add to library.
- Lazily loaded via `static/js/panels.js`; `/files` deep link + rail button in
  `static/app.js` + `static/index.html`.

## Feature 2 — Terminal (command runner, Option B)

- New `static/js/terminal.js`: input + cwd field + "Run" button + SSE streaming
  output pane + copy. No persistent session.
- Backend: reuse `/api/shell/exec` and `/api/shell/stream` unchanged where
  possible. Optionally add an admin-validated `cwd` to the shell request
  (realpath + `isdir`); otherwise default `$HOME`.

### Guardrails (differing environments)

- Platform gate: on Windows hide PTY/tmux; show "line-by-line only" banner
  (existing `IS_WINDOWS` behavior already degrades).
- Shell detection: use existing `find_bash`/`git_bash_path`; on Windows without
  Git Bash, surface the existing "install hint" instead of failing silently.
- tmux detection (POSIX): only offer persistent mode when tmux exists.
- Container detection (`running_in_container()`): banner "commands run inside
  the Odysseus container, not the host."
- Reuse existing limits: `EXEC_TIMEOUT`, `STREAM_TIMEOUT`, max output caps,
  empty-command handling, `_require_admin`, `_reject_cross_site`.
- UI communicates one-shot semantics (no cwd/env carryover across commands).

## Feature 3 — SSH file access

### Host profiles (admin-only)

- New durable store `src/constants.py:SSH_HOSTS_FILE` under `data/` (host, port,
  user, identity-file path, optional platform hint). Keys referenced by path,
  not stored raw. Admin CRUD only.

### Backend — new `routes/ssh_routes.py` (or `services/ssh/` + routes)

- `POST /api/ssh/test` — connectivity check (`ssh -o ConnectTimeout=… host true`).
- `GET  /api/ssh/browse?host=&path=` — remote `ls`/`find` via argv (no shell
  interpolation); reuse `validate_remote_host`/`validate_ssh_port`.
- `GET  /api/ssh/read?host=&path=` — `ssh host -- cat -- <path>` (argv), capped.
- `POST /api/ssh/write` — write via `ssh host -- tee -- <path>` with overwrite
  confirmation; refuse suspicious paths.
- `POST /api/ssh/exec` — one-shot remote command (bounded, admin-only).
- Build commands as **argv lists** (reuse `_ssh_base_argv` / `_ssh_exec_argv`);
  base64-encode remote paths to avoid quoting/escaping issues.

### Frontend

- Host selector + reuse the Files explorer UI with a "target host" switch.
- Add-to-chat / add-to-library: pull remote bytes over SSH then run the same
  actions as local.

## Add-to-chat vs Add-to-library (semantics — needs confirmation)

- **Add to chat**: attach the file bytes to the current composer via the
  existing upload/attachment pipeline (`fileHandler.js`), so it flows through
  normal attachment handling and untrusted-content wrapping.
- **Add to library**: import into Documents (personal docs) for RAG indexing,
  owner-scoped. Target surface to confirm (Documents/RAG vs Gallery).

## File-by-file change outline

New:
- `routes/files/files_routes.py`, `routes/files_routes.py` (shim)
- `routes/ssh_routes.py`
- `services/ssh/service.py` (or `src/ssh_client.py`)
- `static/js/files.js`
- `static/js/terminal.js`
- `tests/test_files_routes.py`, `tests/test_ssh_routes.py`, `tests/test_files_*.py`, `tests/*_js.py`

Edited:
- `app.py` — mount the two routers
- `src/constants.py` — `SSH_HOSTS_FILE` (+ any new caps)
- `src/tool_security.py` — only if/when agent tools are added (deferred)
- `static/index.html` — nav + module tags
- `static/js/panels.js` — lazy-load entries
- `static/sw.js` — precache new modules
- `static/app.js` — deep links + rail wiring
- `static/js/settings/registry.js` — SSH host settings entry

## Security & privilege model

- Admin-only everywhere (consistent with `NON_ADMIN_BLOCKED_TOOLS` + shell routes).
- Path confinement: realpath, symlink reject, sensitive-dir deny lists,
  `vet_workspace` for selectable dirs, filesystem-root refusal.
- SSH: argv-only command construction (no shell strings), host/port validators,
  no secrets in logs (`log_safety`), identity files referenced not inlined.
- Cross-site request rejection for mutating endpoints.
- Read caps + binary refusal to avoid accidental large/secret dumps.

## Platform / degraded behavior

- Windows: no PTY; Git Bash detection with clear hint; pipe streaming.
- Docker: `running_in_container()` banner; SSH preferred for host access (per
  `host_docker_access.py`).
- Bare metal: full local filesystem browsing (still admin-only + confined).

## Testing

- Python: admin-gate (403 for non-admin), path confinement/symlink/sensitive-dir,
  read cap, write overwrite guard, SSH argv building + host/port validation, add
  to chat/library owner scope.
- JS: source-shape tests for `files.js`/`terminal.js` (no network).
- Manual: run app, attach screenshots (visual changes), per CONTRIBUTING.md.

## Rollout order

1. Files Explorer (local) + add-to-chat/add-to-library.
2. Terminal command runner + guardrails.
3. SSH file access (profiles + browse/read/write + add-to-*).
4. (Deferred) interactive PTY terminal + SSH terminal + agent tools.

## Open questions

1. Exact "Add to library" target (Documents/RAG vs Gallery vs new Files library).
2. Whether `cwd` should be added to the shell endpoints or left `$HOME`-only.
3. SSH host profile storage location (new `data/` store vs settings vs prefs).
