# gen_py_debug.py

Detect the languages/components in a project directory and generate a ready-to-use
**debuggable Docker setup** plus **VS Code attach configs** — no manual Dockerfile
writing required.

---

## What it generates

| File | Purpose |
|------|---------|
| `Dockerfile.debug.<component>` | Per-component debug image (debugger installed, optimizations off) |
| `docker-compose.debug.yml` | Orchestrates all components + optional nginx reverse proxy |
| `nginx/debug.conf` | Generated only when `nginx/` exists; proxies vhosts to Docker service names |
| `.vscode/launch.json` | Attach configs for every component (merges with existing, never clobbers) |

The original `Dockerfile` is **never overwritten**.

---

## Quick start

```bash
# From your project root:
python3 /path/to/gen_py_debug.py [project_dir] [options]

docker compose -f docker-compose.debug.yml up --build

# VS Code → Run and Debug → pick "Attach: <component>"
```

---

## CLI options

| Flag | Description |
|------|------------|
| `project` | Project directory (default: `.`) |
| `--dry-run` | Print generated files, write nothing |
| `--detect-only` | Show detected components and exit |
| `--no-port-check` | Skip host port probing; use defaults |
| `--no-watch` | Skip `watchmedo` auto-restart; use plain `debugpy` |
| `--watch-ignore` | Comma-separated dirs to exclude from watchmedo (default: `exploit,tests`) |

---

## Language support

| Language | Debugger | Debug port | Notes |
|----------|----------|-----------|-------|
| **Node.js** | `--inspect` / nodemon | 9229 | Puppeteer: Chromium installed automatically |
| **Python** | debugpy | 5678 | Supports watchmedo auto-restart; supervisord-aware |
| **Go** | Delve (`dlv`) | 2345 | Binary placed at `/usr/local/bin/server` to avoid bind-mount collision |
| **PHP** | Xdebug | 9003 | Container→IDE connect mode |
| **Java** | JDWP | 5005 | Maven or Gradle auto-detected |
| **Ruby** | rdbg | 1234 | |

---

## Detection logic

### Component detection
1. **Manifest files** (`package.json`, `requirements.txt`, `go.mod`, etc.) identify a
   component's language and root directory.
2. **Fallback**: if no manifest is found, the dominant source file extension wins.
3. Nested components of the same language inside a parent component are deduplicated.

### Entry point
- Node: `package.json` `"main"` / `"scripts.start"`, then `app.js / server.js / index.js / main.js`
- Python: `app.py / main.py / server.py / run.py / manage.py / wsgi.py`
- Go: recursive `**/main.go` search (handles `cmd/server/main.go` style layouts)
- Ruby: `app.rb / config.ru`

### Port detection
Scans source files for `PORT=`, `listen(`, `port =`, and `port = "..."` patterns.
Bare URL-style `:port` strings (e.g. in redirect URIs) are intentionally excluded to
avoid picking up proxy ports instead of the service's own listen port.

### Workdir
Reads `WORKDIR` from the existing `Dockerfile` (or parent `Dockerfile`); falls back to
`/app` or `/app/<rel>`.

---

## Environment variables

When `entrypoint.sh` exists at the project root, it is parsed for `export VAR=value`
lines and injected into every compose service:

- **Dynamic values** (`$(openssl rand ...)` etc.) → replaced with a static debug stub
  (`debugsecret1234567890abcdef` for secrets/passwords, `HTB{debug_flag}` for flags)
- **Absolute path variables** (`CHROME_PATH`, `*DIR`) → skipped (environment-specific)
- **Literal values** → used as-is

A `PORT` variable is always set to the detected component port so the service actually
listens on the mapped container port.

Puppeteer services automatically get `CHROME_PATH=/usr/bin/chromium-browser` to match
the Alpine Chromium install in the generated Dockerfile.

---

## Nginx reverse proxy (auto-generated)

When `nginx/` exists with virtual-host configs, the generator:

1. Parses `server_name` / `proxy_pass` blocks to map hostnames → services
2. Matches the `proxy_pass` port to a component by scanning each component's source
   files for listen-port patterns (handles cases where the detected port differs from
   the proxy target port)
3. Generates `nginx/debug.conf` using Docker service names as upstream targets
4. Adds an `nginx-proxy` service to `docker-compose.debug.yml` that:
   - Mounts the generated config read-only
   - Exposes the original nginx listen port on the host
   - Carries DNS aliases for all discovered vhosts, so inter-container traffic
     resolves correctly

This replaces the original single-host `proxy_pass http://127.0.0.1:<port>` setup with
a multi-container-aware equivalent without requiring changes to the original nginx conf.

---

## Python + supervisord

When the component uses supervisord, the generator patches `supervisord.conf` at build
time: it rewrites the `command=` line to prepend `python -m debugpy --listen ...` (and
optionally `watchmedo auto-restart`) so the existing process manager keeps working while
the debugger is attached.

Additional details extracted from the original Dockerfile:
- `RUN mkdir` directives (reproduced verbatim)
- `ENV` lines
- `apt-get` extra packages (`gcc`, `libssl-dev`, etc.)
- `useradd`/`adduser` setup blocks
- C source compilation blocks (with their source files and `flag.txt`)

---

## Node.js / bind-mount note

Node Dockerfiles copy the component directory directly to the `WORKDIR` (`/app`):

```dockerfile
COPY client /app/
```

The CMD therefore references the entry file directly (`server.js`), not with the
subdirectory prefix (`client/server.js`). The compose volume mount
`./client:/app/` preserves `node_modules` via an anonymous volume:

```yaml
volumes:
  - ./client:/app/
  - /app/node_modules   # keeps image-layer node_modules visible at runtime
```

---

## Go / bind-mount note

The built binary is placed at `/usr/local/bin/server` (outside the source bind-mount at
`/app/`) so the volume mount does not hide it at runtime. The build package is derived
from the entry-point path:

```
entry: cmd/server/main.go  →  go build ... ./cmd/server
```

---

## Port assignment

Host-side ports are probed for availability (disable with `--no-port-check`). When the
preferred port is taken, the next free port is used and a warning is printed. Debug ports
for PHP use Xdebug's container→IDE connect model and are not published.

---

## Exploit script support

If `exploit/script.py` exists, a `"Run: exploit/script.py"` launch config is added to
`.vscode/launch.json` automatically.
