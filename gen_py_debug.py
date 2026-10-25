#!/usr/bin/env python3
"""Detect the languages/components of a project and generate a debuggable
Docker setup plus VS Code attach configs.

Generates (never overwrites your existing Dockerfile):
  Dockerfile.debug.<component>
  docker-compose.debug.yml
  .vscode/launch.json

Usage:
  python3 gen_debug_env.py [project_dir] [--dry-run] [--detect-only]
Then:
  docker compose -f docker-compose.debug.yml up --build
  VS Code -> Run and Debug -> "Attach: <component>"
"""
import argparse
import json
import os
import re
import socket
import sys
from collections import Counter
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "vendor",
             "target", "build", "dist", ".idea", ".vscode", "public", "static"}

EXT_LANG = {".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node",
            ".py": "python", ".php": "php", ".java": "java", ".go": "go",
            ".rb": "ruby", ".cs": "dotnet", ".rs": "rust"}

MANIFESTS = {"package.json": "node", "requirements.txt": "python",
             "pyproject.toml": "python", "Pipfile": "python",
             "composer.json": "php", "pom.xml": "java", "build.gradle": "java",
             "go.mod": "go", "Gemfile": "ruby", "Cargo.toml": "rust"}

DEBUG_PORT = {"node": 9229, "python": 5678, "php": 9003, "java": 5005,
              "go": 2345, "ruby": 1234, "dotnet": 4024, "rust": 1234}


# ---------------------------------------------------------------- detection
def walk(root: Path):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        yield Path(dp), fns


def detect(root: Path):
    comps, ext_count = {}, Counter()
    for d, files in walk(root):
        for f in files:
            if f in MANIFESTS:
                comps.setdefault(d, {"lang": MANIFESTS[f], "manifest": f})
            elif f.endswith(".csproj"):
                comps.setdefault(d, {"lang": "dotnet", "manifest": f})
            ext_count[Path(f).suffix] += 1
    # fallback: no manifest -> dominant source extension
    if not comps:
        langs = Counter({EXT_LANG[e]: n for e, n in ext_count.items() if e in EXT_LANG})
        if langs:
            comps[root] = {"lang": langs.most_common(1)[0][0], "manifest": None}
    # drop nested manifests of the same lang inside another component
    out = []
    for d, c in sorted(comps.items(), key=lambda kv: len(kv[0].parts)):
        if any(o["dir"] in d.parents and o["lang"] == c["lang"] for o in out):
            continue
        c["dir"] = d
        c["rel"] = os.path.relpath(d, root)
        c["name"] = re.sub(r"[^a-z0-9]+", "-",
                           (c["rel"] if c["rel"] != "." else root.name).lower()).strip("-")
        c["entry"] = find_entry(d, c["lang"])
        # manifests (e.g. package.json "main") may be relative to the project
        # root instead of the component dir; normalise to the component dir
        if c["entry"] and not (d / c["entry"]).exists():
            stripped = re.sub(rf"^{re.escape(d.name)}/", "", c["entry"])
            if (d / stripped).exists():
                c["entry"] = stripped
        c["port"] = find_port(d, c["lang"])
        out.append(c)
    return out, ext_count


def find_entry(d: Path, lang: str):
    if lang == "node":
        pj = d / "package.json"
        if pj.exists():
            try:
                data = json.loads(pj.read_text())
                if data.get("main"):
                    return data["main"]
                m = re.search(r"node\s+(?:--\S+\s+)*(\S+\.m?c?js)",
                              data.get("scripts", {}).get("start", ""))
                if m:
                    return m.group(1)
            except Exception:
                pass
        cands = ["app.js", "server.js", "index.js", "main.js"]
    elif lang == "python":
        cands = ["app.py", "main.py", "server.py", "run.py", "manage.py", "wsgi.py"]
    elif lang == "go":
        cands = ["main.go"]
    elif lang == "ruby":
        cands = ["app.rb", "config.ru"]
    else:
        return None
    for c in cands:
        for p in [d / c, *sorted(d.glob("**/" + c))]:
            if p.exists():
                return os.path.relpath(p, d)
    return cands[0] if cands else None


def find_port(d: Path, lang: str):
    # Match listen/PORT assignment patterns but NOT bare URL-style :port
    pat = re.compile(
        r'(?:PORT\s*(?:\|\||=|\?\?)\s*|listen\(\s*|port\s*=\s*)(\d{4,5})\b'
        r'|["\']PORT["\'],\s*["\'](\d{4,5})["\']'
        r'|port\s*=\s*["\'](\d{4,5})["\']'  # Go/Python: port = "3002"
    )
    for p, files in walk(d):
        for f in files:
            if Path(f).suffix in EXT_LANG:
                try:
                    for m in pat.finditer((p / f).read_text(errors="ignore")):
                        val = int(next(g for g in m.groups() if g is not None))
                        if 1024 <= val <= 65535:
                            return val
                except OSError:
                    continue
    return {"node": 3000, "python": 5000, "php": 80, "java": 8080,
            "go": 8080, "ruby": 3000}.get(lang, 8080)


def uses_puppeteer(d: Path):
    pj = d / "package.json"
    return pj.exists() and "puppeteer" in pj.read_text()


def uses_supervisord(d: Path) -> bool:
    if (d / "supervisord.conf").exists():
        return True
    for root_candidate in [d.parent, d]:
        df = root_candidate / "Dockerfile"
        if df.exists() and "supervisord" in df.read_text():
            return True
    return False


def find_supervisord_conf(d: Path, root: Path) -> Path | None:
    """Locate supervisord.conf, checking common places relative to component and project root."""
    for candidate in [
        d / "supervisord.conf",
        root / "supervisord.conf",
        root / "config" / "supervisord.conf",
        d / "config" / "supervisord.conf",
    ]:
        if candidate.exists():
            return candidate
    return None


def get_supervisord_command(conf_path: Path) -> str | None:
    """Extract the bare command= line from supervisord.conf."""
    m = re.search(r"^command\s*=\s*(.+)$", conf_path.read_text(), re.M)
    return m.group(1).strip() if m else None


def _find_dockerfile(d: Path) -> Path | None:
    """Return Dockerfile at d or its parent (project root)."""
    for candidate in [d / "Dockerfile", d.parent / "Dockerfile"]:
        if candidate.exists():
            return candidate
    return None


def extract_mkdirs(d: Path) -> str:
    """Return RUN mkdir lines from the original Dockerfile, or empty string if none."""
    df = _find_dockerfile(d)
    if not df:
        return ""
    mkdirs = re.findall(r"RUN mkdir[^\n]+", df.read_text())
    return "\n".join(mkdirs)


def detect_flag_dest(d: Path) -> str:
    """Return where the original Dockerfile moves flag.txt, defaulting to /app/flag.txt."""
    df = _find_dockerfile(d)
    if df:
        m = re.search(r"mv\s+\S*flag\.txt\s+(\S+)", df.read_text())
        if m:
            return m.group(1)
    return "/app/flag.txt"


def extract_env_lines(d: Path, root: Path) -> str:
    """Return ENV lines from the original Dockerfile."""
    df = _find_dockerfile(d)
    if not df:
        return ""
    return "\n".join(re.findall(r"^ENV\s+.+", df.read_text(), re.M))


def extract_run_blocks(d: Path, root: Path, keywords: list) -> str:
    """Return RUN blocks from the original Dockerfile that contain any of the keywords.
    Skips apt-get blocks (handled separately)."""
    df = _find_dockerfile(d)
    if not df:
        return ""
    # Match RUN ... including line continuations
    blocks = re.findall(r"RUN\s+(?:[^\n]*\\\n)*[^\n]+", df.read_text())
    return "\n".join(
        b for b in blocks
        if any(kw in b for kw in keywords) and "apt-get" not in b
    )


def extra_apt_packages(d: Path, root: Path) -> str:
    """Return extra apt packages from the original Dockerfile's apt-get install block."""
    df = _find_dockerfile(d)
    if not df:
        return ""
    text = df.read_text()
    # Match entire RUN blocks (with line continuations) and scan for apt-get installs
    pkgs = []
    for block in re.findall(r"RUN\s+(?:[^\n]*\\\n)*[^\n]+", text):
        if "apt-get install" in block:
            pkgs.extend(re.findall(
                r"\b(gcc|g\+\+|libc6-dev|libssl-dev|libffi-dev|curl|wget|make)\b", block))
    return " ".join(dict.fromkeys(pkgs))  # deduplicate, preserve order


def detect_python_version(d: Path) -> str:
    """Read the Python version from the original Dockerfile's FROM line."""
    df = _find_dockerfile(d)
    if df:
        m = re.search(r"FROM python:(\d+\.\d+)", df.read_text())
        if m:
            return m.group(1)
    return "3.12"


def detect_workdir(d: Path, fallback: str) -> str:
    """Read WORKDIR from the original Dockerfile, falling back to the given value."""
    df = _find_dockerfile(d)
    if df:
        m = re.search(r"^WORKDIR\s+(\S+)", df.read_text(), re.M)
        if m:
            return m.group(1)
    return fallback


# ------------------------------------------------------------------ ports
def port_free(p: int) -> bool:
    """True if nothing on the host is listening on p (bind to 0.0.0.0, no REUSEADDR)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", p))
            return True
        except OSError:
            return False


def next_free(start: int, claimed: set, check: bool) -> int:
    p = start
    while p in claimed or (check and not port_free(p)):
        p += 1
        if p > 65535:
            sys.exit(f"no free host port found at or above {start}")
    claimed.add(p)
    return p


def assign_ports(comps, check: bool):
    """Pick host-side ports (container side never changes)."""
    claimed = set()
    for c in comps:
        want = c["port"]
        c["host_port"] = next_free(want, claimed, check)
        if c["host_port"] != want:
            print(f"  ! {c['name']}: app port {want} in use -> host {c['host_port']}")
        # PHP/Xdebug connects container -> IDE, so there is nothing to publish
        if c["lang"] == "php":
            c["host_dbg"] = None
            continue
        want = DEBUG_PORT[c["lang"]]
        c["host_dbg"] = next_free(want, claimed, check)
        if c["host_dbg"] != want:
            print(f"  ! {c['name']}: debug port {want} in use -> host {c['host_dbg']}")


# ------------------------------------------------------------- generation
def dockerfile(c, root: Path):
    lang, rel, entry = c["lang"], c["rel"], c["entry"]
    dst = c["dst"]
    dbg = DEBUG_PORT[lang]
    copy_manifest = lambda *names: "\n".join(
        f"COPY {('' if rel == '.' else rel + '/')}{n} ./" for n in names
        if (c["dir"] / n).exists())

    if lang == "node":
        chromium = ""
        env = ""
        if uses_puppeteer(c["dir"]):
            chromium = ("RUN apk add --no-cache chromium nss freetype harfbuzz "
                        "ca-certificates ttf-freefont\n")
            env = ("ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true \\\n"
                   "    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-browser\n")
        # pre is only used for host-side COPY source paths, not for CMD paths inside
        # the container: COPY client /app/ puts files at /app/<entry>, not /app/client/<entry>
        pre = f"{rel + '/' if rel != '.' else ''}"
        run = f"nodemon --inspect=0.0.0.0:{dbg} {entry}"
        note = ""
        if (c["dir"] / "tailwind.config.js").exists() and (c["dir"] / "styles/input.css").exists():
            # built at start-up: the bind mount would hide a build baked into the image
            run = (f"mkdir -p public/css && npx tailwindcss -c tailwind.config.js "
                   f"-i styles/input.css -o public/css/app.css --minify && exec {run}")
            note = "# Tailwind CSS is built at start-up (bind mount hides image-time builds)\n"
        return f"""FROM node:20-alpine
WORKDIR /app
{chromium}{env}{copy_manifest('package.json', 'package-lock.json')}
RUN npm install --no-audit --no-fund
COPY {rel if rel != '.' else '.'} {dst if rel != '.' else './'}
# nodemon restarts on edits; --inspect exposes the debugger
RUN npm install -g nodemon
{note}EXPOSE {c['port']} {dbg}
CMD ["sh", "-c", "{run}"]
"""
    if lang == "python":
        pyver = detect_python_version(c["dir"])
        no_watch = c.get("no_watch", False)
        req = "requirements.txt" if (c["dir"] / "requirements.txt").exists() else None
        install = (f"{copy_manifest('requirements.txt')}\nRUN pip install -r requirements.txt"
                   if req else "")
        ignore_patterns = ";".join(f"*/{d}/*" for d in c.get("watch_ignore", []))
        ignore_flag = f' --ignore-patterns="{ignore_patterns}"' if ignore_patterns else ""
        watch_cmd = f"watchmedo auto-restart --directory={dst} --pattern=*.py{ignore_flag} --recursive --"
        debugpy_install = 'RUN pip install debugpy' if no_watch else 'RUN pip install debugpy "watchdog[watchmedo]"'
        if uses_supervisord(c["dir"]):
            # Patch supervisord.conf at build time: wrap the entry command with debugpy (and optionally watchmedo)
            conf_path = find_supervisord_conf(c["dir"], root)
            if conf_path:
                conf_rel = os.path.relpath(conf_path, root)
                orig_cmd = get_supervisord_command(conf_path) or f"python /app/{entry}"
                debug_prefix = "" if no_watch else f"{watch_cmd} "
                new_cmd = f"{debug_prefix}python -m debugpy --listen 0.0.0.0:{dbg} {orig_cmd.split()[-1]}"
                copy_conf = f"COPY {conf_rel} /tmp/supervisord_orig.conf"
                src_conf = "/tmp/supervisord_orig.conf"
            else:
                copy_conf = ""
                orig_cmd = f"python /app/{entry}"
                debug_prefix = "" if no_watch else f"{watch_cmd} "
                new_cmd = f"{debug_prefix}python -m debugpy --listen 0.0.0.0:{dbg} /app/{entry}"
                src_conf = "/app/supervisord.conf"
            # Use a heredoc so quotes/globs in watch_cmd don't need shell-escaping
            patch_script = (
                f"c = open({repr(src_conf)}).read()\n"
                f"c = c.replace({repr(orig_cmd)}, {repr(new_cmd)}, 1)\n"
                f"open('/etc/supervisor/conf.d/supervisord.conf', 'w').write(c)\n"
            )
            mkdirs = extract_mkdirs(c["dir"])
            env_lines = extract_env_lines(c["dir"], root)
            extra_pkgs = extra_apt_packages(c["dir"], root)
            apt_extra = f" {extra_pkgs}" if extra_pkgs else ""
            user_setup = extract_run_blocks(c["dir"], root, ["useradd", "adduser", "groupadd", "addgroup", "usermod"])
            # Compile helper binaries (readflag.c etc.) found at root level
            compile_setup = extract_run_blocks(c["dir"], root, ["gcc", "g++"])
            # Copy any .c sources and flag.txt referenced in the compile step
            c_sources = ""
            for src in re.findall(r"(\S+\.c)\b", compile_setup):
                src_path = root / src.lstrip("/")
                if not src_path.exists():
                    src_path = next(root.glob(f"**/{Path(src).name}"), None)
                if src_path and src_path.exists():
                    c_sources += f"\nCOPY {os.path.relpath(src_path, root)} {src}"
            if "flag.txt" in compile_setup and (root / "flag.txt").exists():
                m_flag = re.search(r"mv\s+(\S*flag\.txt)", compile_setup)
                flag_container_src = m_flag.group(1) if m_flag else f"{dst}/flag.txt"
                c_sources += f"\nCOPY flag.txt {flag_container_src}"
            flag_dest = detect_flag_dest(c["dir"])
            flag_move = (f"RUN mv /app/flag.txt {flag_dest}"
                         if flag_dest != "/app/flag.txt" and (c["dir"] / "flag.txt").exists()
                         else "")
            # DATA_DIR = settings.py.parent.parent/"data" = dst.parent/"data" (e.g. /srv/data)
            # This is outside the bind-mounted WORKDIR so we must create it in the image;
            # a named volume in compose ensures Docker initialises it with appuser ownership.
            data_dir = str(Path(dst).parent / "data")
            data_dir_setup = (
                f"RUN mkdir -p {data_dir} && chown appuser:appuser {data_dir}"
                if data_dir != dst  # skip if dst.parent == dst (e.g. /app)
                else ""
            )
            return f"""FROM python:{pyver}-slim
WORKDIR {dst}
{env_lines}
RUN apt-get update && apt-get install -y{apt_extra} supervisor && rm -rf /var/lib/apt/lists/*
{install}
{debugpy_install}
{mkdirs}
COPY {rel if rel != "." else "."} {dst if rel != "." else "./"}
{c_sources}
{copy_conf}
{user_setup}
{data_dir_setup}
{compile_setup}
{flag_move}
RUN python3 - << 'PYEOF'
{patch_script}PYEOF
EXPOSE {c['port']} {dbg}
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
"""
        flag_dest = detect_flag_dest(c["dir"])
        flag_move = (f"RUN mv /app/flag.txt {flag_dest}"
                     if flag_dest != "/app/flag.txt" and (c["dir"] / "flag.txt").exists()
                     else "")
        cmd = (f"python -m debugpy --listen 0.0.0.0:{dbg} {entry}"
               if no_watch else
               f"{watch_cmd} python -m debugpy --listen 0.0.0.0:{dbg} {entry}")
        return f"""FROM python:{pyver}-slim
WORKDIR {dst}
{install}
{debugpy_install}
COPY {rel if rel != "." else "."} .
{flag_move}
EXPOSE {c['port']} {dbg}
CMD ["sh", "-c", "{cmd}"]
"""
    if lang == "php":
        return f"""FROM php:8.3-apache
RUN pecl install xdebug && docker-php-ext-enable xdebug \\
 && echo "xdebug.mode=debug\\nxdebug.start_with_request=yes\\nxdebug.client_host=host.docker.internal\\nxdebug.client_port={dbg}" \\
    > /usr/local/etc/php/conf.d/xdebug.ini
COPY {rel} /var/www/html/
EXPOSE 80
"""
    if lang == "java":
        build = ("RUN mvn -q -DskipTests package" if c["manifest"] == "pom.xml"
                 else "RUN gradle -q build -x test")
        base = "maven:3-eclipse-temurin-21" if c["manifest"] == "pom.xml" else "gradle:8-jdk21"
        return f"""FROM {base}
WORKDIR /app
COPY {rel} .
{build}
EXPOSE {c['port']} {dbg}
CMD ["sh","-c","java -agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:{dbg} -jar $(ls target/*.jar build/libs/*.jar 2>/dev/null | head -1)"]
"""
    if lang == "go":
        # build package is the directory containing the entry point
        entry_dir = str(Path(entry).parent) if entry and Path(entry).parent != Path(".") else "."
        build_pkg = f"./{entry_dir}" if entry_dir != "." else "."
        # Binary goes outside the bind-mounted source dir so the volume mount doesn't hide it
        return f"""FROM golang:1.25
WORKDIR /app
RUN go install github.com/go-delve/delve/cmd/dlv@latest
COPY {rel} .
RUN go build -gcflags="all=-N -l" -o /usr/local/bin/server {build_pkg}
EXPOSE {c['port']} {dbg}
CMD ["dlv","exec","/usr/local/bin/server","--headless","--listen=:{dbg}","--api-version=2","--accept-multiclient","--continue"]
"""
    if lang == "ruby":
        return f"""FROM ruby:3.3
WORKDIR /app
RUN gem install debug
COPY {rel} .
RUN bundle install || true
EXPOSE {c['port']} {dbg}
CMD ["rdbg","-n","--open","--host","0.0.0.0","--port","{dbg}","-c","--","ruby","{entry}"]
"""
    return None


def launch_cfg(c):
    lang, rel = c["lang"], c["rel"]
    dbg = c.get("host_dbg") or DEBUG_PORT[lang]
    name = f"Attach: {c['name']} ({lang})"
    local = "${workspaceFolder}" + ("" if rel == "." else "/" + rel)
    remote = c["dst"]
    base = {"name": name, "request": "attach"}
    if lang == "node":
        return {**base, "type": "node", "address": "localhost", "port": dbg,
                "localRoot": local, "remoteRoot": remote, "restart": True,
                "skipFiles": ["<node_internals>/**"]}
    if lang == "python":
        return {**base, "type": "debugpy", "host": "localhost", "port": dbg,
                "pathMappings": [{"localRoot": local, "remoteRoot": remote}]}
    if lang == "php":
        return {**base, "type": "php", "port": dbg,
                "pathMappings": {"/var/www/html": local}}
    if lang == "java":
        return {**base, "type": "java", "hostName": "localhost", "port": dbg}
    if lang == "go":
        return {**base, "type": "go", "mode": "remote", "host": "localhost",
                "port": dbg, "remotePath": remote, "cwd": local}
    if lang == "ruby":
        return {**base, "type": "rdbg", "debugPort": f"localhost:{dbg}"}
    return None


def exploit_launch_cfg(root: Path):
    script = root / "exploit" / "script.py"
    if not script.exists():
        return None
    return {
        "name": "Run: exploit/script.py",
        "type": "debugpy",
        "request": "launch",
        "program": "${workspaceFolder}/exploit/script.py",
        "console": "integratedTerminal",
        "cwd": "${workspaceFolder}/exploit",
    }


def detect_nginx_aliases(root: Path, comps: list) -> dict:
    """Parse nginx configs in root/nginx/ to build {service_name: [hostname, ...]} map.
    Matches server_name → proxy_pass port → whichever component's sources mention that port."""
    # Collect ports each component listens on, using the same listen-port heuristic as find_port
    listen_re = re.compile(
        r'(?:PORT\s*(?:\|\||=|:|\?\?)\s*|listen\(\s*|port\s*=\s*)(\d{4,5})\b'
        r'|["\']PORT["\'],\s*["\'](\d{4,5})["\']'
        r'|["\'](\d{4,5})["\']'  # quoted bare port literals in config
    )

    def listen_ports(d: Path) -> set:
        ports = set()
        for p, files in walk(d):
            for f in files:
                if Path(f).suffix in EXT_LANG:
                    try:
                        for m in listen_re.finditer((p / f).read_text(errors="ignore")):
                            val = int(next(g for g in m.groups() if g is not None))
                            if 1024 <= val <= 65535:
                                ports.add(val)
                    except OSError:
                        pass
        return ports

    comp_ports = {c["name"]: (listen_ports(c["dir"]) | {c["port"]}) for c in comps}

    aliases: dict = {}
    nginx_dir = root / "nginx"
    if not nginx_dir.is_dir():
        return aliases
    for conf in nginx_dir.glob("*.conf"):
        text = conf.read_text()
        for block in re.findall(r"server\s*\{[^}]+\}", text, re.S):
            name_m = re.search(r"server_name\s+(\S+);", block)
            pass_m = re.search(r"proxy_pass\s+https?://\S+?:(\d+)", block)
            if not name_m or not pass_m:
                continue
            hostname = name_m.group(1)
            if hostname in ("_", "localhost"):
                continue
            proxy_port = int(pass_m.group(1))
            for svc, ports in comp_ports.items():
                if proxy_port in ports:
                    aliases.setdefault(svc, []).append(hostname)
                    break
    return aliases


def nginx_debug_conf(root: Path, nginx_aliases: dict, comps: list) -> str | None:
    """Generate a debug nginx.conf that proxies vhosts to Docker service names.
    Returns the config string, or None if no nginx dir exists."""
    nginx_dir = root / "nginx"
    if not nginx_dir.is_dir():
        return None
    # Find the external listen port from the original nginx config
    listen_port = 1337  # fallback
    for conf in nginx_dir.glob("*.conf"):
        m = re.search(r"listen\s+(\d+)\s+default_server", conf.read_text())
        if m:
            listen_port = int(m.group(1))
            break
    # Build hostname → (service_name, native_port) map
    name_to_comp = {c["name"]: c for c in comps}
    hostname_map = {}
    for svc, hostnames in nginx_aliases.items():
        c = name_to_comp.get(svc)
        if c:
            for h in hostnames:
                hostname_map[h] = (svc, c["port"])
    if not hostname_map:
        return None
    servers = []
    for hostname, (svc, svc_port) in hostname_map.items():
        servers.append(f"""    server {{
        listen {listen_port};
        server_name {hostname};
        location / {{
            proxy_pass http://{svc}:{svc_port};
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
        }}
    }}""")
    return "events {}\nhttp {\n" + "\n".join(servers) + "\n}\n"


def detect_entrypoint_env(root: Path) -> dict:
    """Extract env vars from entrypoint.sh; replace dynamic values with debug stubs."""
    ep = root / "entrypoint.sh"
    if not ep.exists():
        return {}
    env = {}
    for m in re.finditer(r"^export\s+(\w+)=(.+)$", ep.read_text(), re.M):
        name, val = m.group(1), m.group(2).strip()
        if "$(" in val or "`" in val:
            # Dynamic/random — use a static debug stub
            if any(k in name for k in ("SECRET", "PASSWORD", "KEY", "TOKEN")):
                env[name] = "debugsecret1234567890abcdef"
            elif "FLAG" in name:
                env[name] = "HTB{debug_flag}"
            elif "PATH" in name or "DIR" in name:
                pass  # skip path overrides (environment-specific)
            else:
                env[name] = "debug"
        else:
            literal = val.strip("'\"")
            # Skip env vars that point to absolute paths — they are host-specific
            if literal.startswith("/") and ("PATH" in name or "DIR" in name):
                pass
            else:
                env[name] = literal
    return env


def compose(comps, root: Path):
    lines = ["services:"]
    named_vols = []  # (vol_name, container_path) pairs
    entrypoint_env = detect_entrypoint_env(root)
    nginx_aliases = detect_nginx_aliases(root, comps)
    for c in comps:
        dbg = DEBUG_PORT[c["lang"]]
        rel = c["rel"]
        dst = c["dst"]
        lines += [f"  {c['name']}:",
                  "    build:",
                  "      context: .",
                  f"      dockerfile: Dockerfile.debug.{c['name']}",
                  "    ports:",
                  f"      - \"{c['host_port']}:{c['port']}\""]
        if c.get("host_dbg"):
            lines.append(f"      - \"127.0.0.1:{c['host_dbg']}:{dbg}\"")
        env_block = dict(entrypoint_env)
        env_block.setdefault("PORT", str(c["port"]))  # ensure service listens on the mapped port
        if c["lang"] == "node" and uses_puppeteer(c["dir"]):
            env_block["CHROME_PATH"] = "/usr/bin/chromium-browser"
        lines.append("    environment:")
        for k, v in env_block.items():
            lines.append(f"      {k}: \"{v}\"")
        lines += [
                  "    volumes:",
                  f"      - ./{'' if rel == '.' else rel}:{dst}"]
        if c["lang"] == "node":
            lines.append(f"      - {dst.rstrip('/')}/node_modules")
        if (c["dir"] / "flag.txt").exists():
            flag_dest = detect_flag_dest(c["dir"])
            lines.append(f"      - ./{'' if rel == '.' else rel + '/'}flag.txt:{flag_dest}:ro")
        # Named volume for the app's DATA_DIR (settings.py.parent.parent/"data"),
        # which lives outside the bind-mounted WORKDIR. A named volume lets Docker
        # initialise it from the image layer (with appuser ownership) on first start.
        if c["lang"] == "python" and uses_supervisord(c["dir"]):
            data_dir = str(Path(dst).parent / "data")
            if data_dir != dst:
                vol_name = f"{c['name']}_data"
                named_vols.append((vol_name, data_dir))
                lines.append(f"      - {vol_name}:{data_dir}")
        lines += ["    extra_hosts:", "      - \"host.docker.internal:host-gateway\"",
                  "    restart: unless-stopped"]
    # Add a debug nginx reverse proxy if nginx vhosts exist
    nginx_conf = nginx_debug_conf(root, nginx_aliases, comps)
    if nginx_conf:
        # Find the external listen port
        nginx_listen = 80
        for conf in (root / "nginx").glob("*.conf"):
            m = re.search(r"listen\s+(\d+)\s+default_server", conf.read_text())
            if m:
                nginx_listen = int(m.group(1))
                break
        all_aliases = [h for hlist in nginx_aliases.values() for h in hlist]
        lines += [
            "  nginx-proxy:",
            "    image: nginx:alpine",
            f"    ports:",
            f"      - \"{nginx_listen}:{nginx_listen}\"",
            "    volumes:",
            "      - ./nginx/debug.conf:/etc/nginx/nginx.conf:ro",
            "    depends_on:",
        ]
        for c in comps:
            lines.append(f"      - {c['name']}")
        lines += ["    networks:", "      default:", "        aliases:"]
        for alias in all_aliases:
            lines.append(f"          - {alias}")
        lines.append("    restart: unless-stopped")
    if named_vols:
        lines.append("volumes:")
        for vol_name, _ in named_vols:
            lines.append(f"  {vol_name}:")
    return "\n".join(lines) + "\n", nginx_conf


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    ap.add_argument("--detect-only", action="store_true")
    ap.add_argument("--no-port-check", action="store_true",
                    help="do not probe host ports; use defaults")
    ap.add_argument("--no-watch", action="store_true",
                    help="Skip watchmedo auto-restart; use plain debugpy (closer to production)")
    ap.add_argument("--watch-ignore", default="exploit,tests",
                    help="Comma-separated directory names to exclude from watchmedo (default: exploit,tests)")
    a = ap.parse_args()
    root = Path(a.project).resolve()

    comps, exts = detect(root)
    print(f"Project: {root}")
    print("Source files by extension:",
          ", ".join(f"{e or '(none)'}={n}" for e, n in exts.most_common(8)))
    if not comps:
        sys.exit("No components detected.")
    for c in comps:
        print(f"  - {c['name']:<20} lang={c['lang']:<7} dir={c['rel']:<12} "
              f"entry={c['entry']} port={c['port']} debug={DEBUG_PORT[c['lang']]}")
    if a.detect_only:
        return

    watch_ignore = [d.strip() for d in a.watch_ignore.split(",") if d.strip()]
    out = {}
    for c in comps:
        default_dst = "/app" if c["rel"] == "." else f"/app/{c['rel']}"
        c["dst"] = detect_workdir(c["dir"], default_dst)
    supported = [c for c in comps if dockerfile(c, root)]
    assign_ports(supported, check=not a.no_port_check)
    for c in supported:
        c["watch_ignore"] = watch_ignore
        c["no_watch"] = a.no_watch
        out[f"Dockerfile.debug.{c['name']}"] = dockerfile(c, root)
    compose_yaml, nginx_conf = compose(supported, root)
    out["docker-compose.debug.yml"] = compose_yaml
    if nginx_conf:
        out["nginx/debug.conf"] = nginx_conf

    vs = root / ".vscode" / "launch.json"
    existing = {"version": "0.2.0", "configurations": []}
    if vs.exists():
        try:
            existing = json.loads(re.sub(r"^\s*//.*$", "", vs.read_text(), flags=re.M))
        except Exception:
            print("! existing launch.json unparseable; leaving it untouched")
            vs = None
    if vs:
        cfgs = [x for x in existing["configurations"]
                if not x.get("name", "").startswith("Attach: ")
                and x.get("name") != "Run: exploit/script.py"]
        cfgs += [launch_cfg(c) for c in supported if launch_cfg(c)]
        exploit_cfg = exploit_launch_cfg(root)
        if exploit_cfg:
            cfgs.append(exploit_cfg)
        existing["configurations"] = cfgs
        out[str(vs.relative_to(root))] = json.dumps(existing, indent=2) + "\n"

    for rel, text in out.items():
        if a.dry_run:
            print(f"\n===== {rel} =====\n{text}")
        else:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            print(f"wrote {rel}")
    if not a.dry_run:
        print("\nNext: docker compose -f docker-compose.debug.yml up --build\n"
              "Then VS Code -> Run and Debug -> pick an 'Attach: ...' config.")


if __name__ == "__main__":
    main()
