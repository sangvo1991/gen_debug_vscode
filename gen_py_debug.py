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
        for p in [d / c, *d.glob("*/" + c)]:
            if p.exists():
                return os.path.relpath(p, d)
    return cands[0] if cands else None


def find_port(d: Path, lang: str):
    pat = re.compile(r"(?:PORT\s*(?:\|\||=|:|\?\?)\s*|listen\(\s*|port\s*=\s*|:)(\d{4,5})\b")
    for p, files in walk(d):
        for f in files:
            if Path(f).suffix in EXT_LANG:
                try:
                    m = pat.search((p / f).read_text(errors="ignore"))
                except OSError:
                    continue
                if m and 1024 <= int(m.group(1)) <= 65535:
                    return int(m.group(1))
    return {"node": 3000, "python": 5000, "php": 80, "java": 8080,
            "go": 8080, "ruby": 3000}.get(lang, 8080)


def uses_puppeteer(d: Path):
    pj = d / "package.json"
    return pj.exists() and "puppeteer" in pj.read_text()


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
    dst = "/app" if rel == "." else f"/app/{rel}"
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
        pre = f"{rel + '/' if rel != '.' else ''}"
        run = f"nodemon --inspect=0.0.0.0:{dbg} {pre}{entry}"
        note = ""
        if (c["dir"] / "tailwind.config.js").exists() and (c["dir"] / "styles/input.css").exists():
            # built at start-up: the bind mount would hide a build baked into the image
            run = (f"mkdir -p {pre}public/css && npx tailwindcss -c {pre}tailwind.config.js "
                   f"-i {pre}styles/input.css -o {pre}public/css/app.css --minify && exec {run}")
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
        req = "requirements.txt" if (c["dir"] / "requirements.txt").exists() else None
        install = (f"{copy_manifest('requirements.txt')}\nRUN pip install -r requirements.txt"
                   if req else "")
        return f"""FROM python:3.12-slim
WORKDIR {dst}
{install}
RUN pip install debugpy
COPY {rel} .
EXPOSE {c['port']} {dbg}
CMD ["python", "-m", "debugpy", "--listen", "0.0.0.0:{dbg}", "{entry}"]
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
        return f"""FROM golang:1.22
WORKDIR /app
RUN go install github.com/go-delve/delve/cmd/dlv@latest
COPY {rel} .
RUN go build -gcflags="all=-N -l" -o /app/server .
EXPOSE {c['port']} {dbg}
CMD ["dlv","exec","/app/server","--headless","--listen=:{dbg}","--api-version=2","--accept-multiclient","--continue"]
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
    remote = "/app" if rel == "." else f"/app/{rel}"
    base = {"name": name, "request": "attach"}
    if lang == "node":
        return {**base, "type": "node", "address": "localhost", "port": dbg,
                "localRoot": local, "remoteRoot": remote, "restart": True,
                "skipFiles": ["<node_internals>/**"]}
    if lang == "python":
        return {**base, "type": "debugpy", "connect": {"host": "localhost", "port": dbg},
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


def compose(comps, root: Path):
    lines = ["services:"]
    for c in comps:
        dbg = DEBUG_PORT[c["lang"]]
        rel = c["rel"]
        dst = "/app" if rel == "." else f"/app/{rel}"
        lines += [f"  {c['name']}:",
                  "    build:",
                  "      context: .",
                  f"      dockerfile: Dockerfile.debug.{c['name']}",
                  "    ports:",
                  f"      - \"{c['host_port']}:{c['port']}\""]
        if c.get("host_dbg"):
            lines.append(f"      - \"127.0.0.1:{c['host_dbg']}:{dbg}\"")
        lines += [
                  "    volumes:",
                  f"      - ./{'' if rel == '.' else rel}:{dst}"]
        if c["lang"] == "node" and rel == ".":
            lines.append("      - /app/node_modules")
        # HTB challenges usually expect the flag at the app root (/app/flag.txt)
        if (c["dir"] / "flag.txt").exists():
            lines.append(f"      - ./{'' if rel == '.' else rel + '/'}flag.txt:/app/flag.txt:ro")
        lines += ["    extra_hosts:", "      - \"host.docker.internal:host-gateway\"",
                  "    restart: unless-stopped"]
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    ap.add_argument("--detect-only", action="store_true")
    ap.add_argument("--no-port-check", action="store_true",
                    help="do not probe host ports; use defaults")
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

    out = {}
    supported = [c for c in comps if dockerfile(c, root)]
    assign_ports(supported, check=not a.no_port_check)
    for c in supported:
        out[f"Dockerfile.debug.{c['name']}"] = dockerfile(c, root)
    out["docker-compose.debug.yml"] = compose(supported, root)

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
                if not x.get("name", "").startswith("Attach: ")]
        cfgs += [launch_cfg(c) for c in supported if launch_cfg(c)]
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
