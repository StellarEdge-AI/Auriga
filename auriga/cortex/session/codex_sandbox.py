"""A small bubblewrap boundary for the native Codex actor process.

The Harness remains outside this process boundary. Only the actor workspace,
Codex home, native executable, selected source snapshot, and run-owned IPC are
mounted into the actor's filesystem namespace.
"""

from __future__ import annotations

import os
import importlib.util
import http.client
import ipaddress
import re
from pathlib import Path
import shutil
import socket
import ssl
import stat
import subprocess
import threading
from urllib.parse import urlsplit, unquote

from .system_runtime import runtime_library_mounts, system_runtime_mounts


_SUPERVISOR = r'''import ctypes,os,signal,socket,subprocess,sys,threading
if ctypes.CDLL(None,use_errno=True).prctl(38,1,0,0,0) != 0: raise OSError("PR_SET_NO_NEW_PRIVS failed")
ports=[int(x) for x in os.environ.get("AURIGA_RELAY_PORTS","").split(",") if x]
sockdir="/run/auriga-relays"
def pipe(a,b):
    def cp(src,dst):
        try:
            while True:
                data=src.recv(65536)
                if not data: break
                dst.sendall(data)
        except OSError: pass
        try: dst.shutdown(socket.SHUT_WR)
        except OSError: pass
    threading.Thread(target=cp,args=(a,b),daemon=True).start(); cp(b,a)
def serve(port,path):
    server=socket.socket(); server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); server.bind(("127.0.0.1",port)); server.listen(32)
    while True:
        client,_=server.accept()
        try:
            upstream=socket.socket(socket.AF_UNIX); upstream.connect(path)
            threading.Thread(target=pipe,args=(client,upstream),daemon=True).start()
        except OSError: client.close()
for port in ports:
    threading.Thread(target=serve,args=(port,sockdir+"/tcp-"+str(port)),daemon=True).start()
proxy=int(os.environ.get("AURIGA_PACKAGE_PROXY_PORT","0"))
if proxy:
    threading.Thread(target=serve,args=(proxy,sockdir+"/packages"),daemon=True).start()
argv=sys.argv[1:]
if not argv: raise SystemExit("sandbox supervisor requires a command")
owner=socket.socket(socket.AF_UNIX); owner.connect(os.environ["AURIGA_OWNER_SOCKET"])
owner.shutdown(socket.SHUT_WR)
child=subprocess.Popen(argv,start_new_session=True)
def forward(sig,_):
    try: os.killpg(child.pid,sig)
    except ProcessLookupError: pass
for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP): signal.signal(sig,forward)
def watch_owner():
    try:
        while owner.recv(1): pass
    except OSError: pass
    forward(signal.SIGTERM,None)
    try: child.wait(timeout=2)
    except subprocess.TimeoutExpired: forward(signal.SIGKILL,None)
threading.Thread(target=watch_owner,daemon=True).start()
raise SystemExit(child.wait())
'''


class _UnixForwarder:
    """Forward one sandbox Unix stream to a fixed host loopback port."""
    def __init__(self, path: Path, port: int, *, packages=False, package_port=None):
        self.path, self.port, self.packages = path, port, packages
        self.package_port = package_port
        self.listener = None
        self.thread = None
        self.closed = False

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path)); os.chmod(self.path, 0o600); listener.listen(32); listener.settimeout(.2)
        self.listener = listener
        self.thread = threading.Thread(target=self._run, daemon=True, name="codex-sandbox-relay")
        self.thread.start()
        return self

    @staticmethod
    def _copy(left, right):
        def pump(src, dst):
            try:
                while data := src.recv(65536): dst.sendall(data)
            except OSError: pass
            try: dst.shutdown(socket.SHUT_WR)
            except OSError: pass
        threading.Thread(target=pump, args=(left, right), daemon=True).start()
        pump(right, left)
        for sock in (left, right):
            try: sock.close()
            except OSError: pass

    @staticmethod
    def _https_request(host, path, headers):
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
        if not addresses or any(not ipaddress.ip_address(address.split("%", 1)[0]).is_global
                                for address in addresses):
            raise OSError("package_host_dns_rejected")
        secure = ssl.create_default_context()
        last_error = None
        for address in addresses:
            raw = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
            try:
                raw.settimeout(20); raw.connect((address, 443))
                tls = secure.wrap_socket(raw, server_hostname=host)
                connection = http.client.HTTPSConnection(host, timeout=30)
                connection.sock = tls
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    connection.close()
                    raise OSError("package_host_redirect_rejected")
                return connection, response
            except OSError as error:
                last_error = error
                raw.close()
        raise last_error or OSError("package_host_unreachable")

    @staticmethod
    def _package_reply(client, status, body=b"", headers=None):
        phrases = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                   405: "Method Not Allowed", 502: "Bad Gateway"}
        lines = [f"HTTP/1.1 {status} {phrases.get(status, 'Error')}",
                 "Connection: close", "Cache-Control: no-store"]
        for key, value in (headers or {}).items():
            if key.lower() in {"content-type", "content-length", "etag", "last-modified", "cache-control"}:
                lines.append(f"{key}: {value}")
        if not any(line.lower().startswith("content-length:") for line in lines):
            lines.append(f"Content-Length: {len(body)}")
        client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)

    def _package_connection(self, client):
        """Serve only PEP 503 indexes and package objects from pinned PyPI hosts."""
        client.settimeout(10)
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) <= 16384:
            part = client.recv(2048)
            if not part: return
            data.extend(part)
        if len(data) > 16384 or b"\r\n\r\n" not in data:
            return self._package_reply(client, 400)
        head, _, body = bytes(data).partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        request = lines[0].split()
        headers = {}
        for line in lines[1:]:
            if ":" not in line: return self._package_reply(client, 400)
            key, value = line.split(":", 1)
            headers[key.lower().strip()] = value.strip()
        if (len(request) != 3 or request[0] != "GET" or request[2] not in ("HTTP/1.0", "HTTP/1.1")
                or headers.get("host", "").lower() not in {
                    f"127.0.0.1:{self.package_port}", f"localhost:{self.package_port}"}
                or headers.get("transfer-encoding") or headers.get("content-length", "0") != "0" or body):
            return self._package_reply(client, 405)
        parsed = urlsplit(request[1])
        if parsed.scheme or parsed.netloc or parsed.query:
            return self._package_reply(client, 403)
        encoded_path = parsed.path
        path = unquote(encoded_path)
        if ("\\" in path or any(part in (".", "..") for part in path.split("/"))
                or any(ord(char) < 0x21 or ord(char) == 0x7f for char in path)):
            return self._package_reply(client, 403)
        try:
            if path.startswith("/simple/"):
                suffix = path[len("/simple/"):]
                if (not suffix.endswith("/") or suffix.count("/") != 1
                        or not suffix[:-1] or not all(char.isalnum() or char in "-_." for char in suffix[:-1])):
                    return self._package_reply(client, 404)
                normalized = re.sub(r"[-_.]+", "-", suffix[:-1]).lower()
                upstream_path = "/simple/" + normalized + "/"
                accept = headers.get("accept", "text/html")[:512]
                request_headers = {"Accept": accept,
                                   "User-Agent": "auriga-package-index/1"}
                connection, response = self._https_request("pypi.org", upstream_path, request_headers)
                try:
                    if response.status != 200:
                        return self._package_reply(client, 404 if response.status == 404 else 502)
                    raw = bytearray()
                    while chunk := response.read(65536):
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024 * 1024: return self._package_reply(client, 502)
                    content_type = response.getheader("Content-Type", "text/html")
                    if "html" not in content_type.lower() and "json" not in content_type.lower():
                        return self._package_reply(client, 502)
                    rewritten = bytes(raw).replace(b"https://files.pythonhosted.org/",
                        f"http://127.0.0.1:{self.package_port}/files/".encode("ascii"))
                    return self._package_reply(client, 200, rewritten,
                                               {"Content-Type": content_type, "Cache-Control": "no-store"})
                finally:
                    connection.close()
            if path.startswith("/files/"):
                file_path = path[len("/files"):]
                if (not file_path.startswith("/packages/") or len(file_path) > 4096
                        or not file_path.rsplit("/", 1)[-1].lower().endswith(
                            (".whl", ".whl.metadata", ".tar.gz", ".zip", ".metadata"))):
                    return self._package_reply(client, 404)
                connection, response = self._https_request("files.pythonhosted.org", encoded_path[len("/files"):],
                    {"Accept-Encoding": "identity", "User-Agent": "auriga-package-download/1"})
                try:
                    if response.status != 200: return self._package_reply(client, 404 if response.status == 404 else 502)
                    content_length = response.getheader("Content-Length")
                    headers_out = {"Content-Type": response.getheader("Content-Type", "application/octet-stream")}
                    chunked = content_length is None
                    if content_length is not None: headers_out["Content-Length"] = content_length
                    lines = ["HTTP/1.1 200 OK", "Connection: close", "Cache-Control: no-store",
                             f"Content-Type: {headers_out['Content-Type']}"]
                    if chunked: lines.append("Transfer-Encoding: chunked")
                    else: lines.append(f"Content-Length: {content_length}")
                    client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
                    while chunk := response.read(65536):
                        if chunked: client.sendall(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
                        else: client.sendall(chunk)
                    if chunked: client.sendall(b"0\r\n\r\n")
                finally:
                    connection.close()
                return
            return self._package_reply(client, 404)
        except (OSError, http.client.HTTPException, ssl.SSLError, UnicodeError):
            try: self._package_reply(client, 502)
            except OSError: pass

    def _run(self):
        while not self.closed:
            try: client, _ = self.listener.accept()
            except socket.timeout: continue
            except OSError: break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        try:
            if self.packages:
                self._package_connection(client)
                return
            upstream = socket.create_connection(("127.0.0.1", self.port), timeout=10)
            client.settimeout(None); upstream.settimeout(None)
            self._copy(client, upstream)
        except OSError:
            pass
        finally:
            try: client.close()
            except OSError: pass

    def close(self):
        self.closed = True
        if self.listener:
            self.listener.close()
        if self.thread:
            self.thread.join(timeout=1)
        self.path.unlink(missing_ok=True)


class CodexSandbox:
    """Build and own the isolated native actor command prefix.

    ``read_paths`` is a sequence of ``(host_path, sandbox_path)`` read-only
    mounts. The caller appends the native Codex argv to the returned prefix.
    """
    def __init__(self, private_root, ipc, actor_dir, codex_home, executable, *, ports=(), packages=True,
                 read_paths=()):
        self.private_root = Path(private_root).absolute()
        self.ipc = Path(ipc).absolute()
        self.actor_dir = Path(actor_dir).absolute()
        self.codex_home = Path(codex_home).absolute()
        self.executable = Path(executable).resolve(strict=True)
        self.ports = tuple(sorted(set(int(port) for port in ports)))
        self.package_proxy_port = 18888
        while self.package_proxy_port in self.ports and self.package_proxy_port < 65535:
            self.package_proxy_port += 1
        if self.package_proxy_port in self.ports:
            raise ValueError("codex_sandbox_no_package_proxy_port_available")
        self.packages = bool(packages)
        rg_path = shutil.which("rg") if self.packages else None
        self.rg_executable = Path(rg_path).resolve(strict=True) if rg_path else None
        if self.rg_executable and not self.rg_executable.is_file():
            raise ValueError("codex_sandbox_ripgrep_must_be_regular_file")
        self.read_paths = tuple((Path(src).resolve(strict=True), Path(dst)) for src, dst in read_paths)
        if any(port < 1 or port > 65535 for port in self.ports):
            raise ValueError("codex_sandbox_invalid_relay_port")
        if any(not dst.is_absolute() or ".." in dst.parts for _, dst in self.read_paths):
            raise ValueError("codex_sandbox_invalid_read_mount")
        self.forwarders = []
        self.owner_listener = None
        self.owner_accept_thread = None
        self.owner_connections = set()
        self.owner_connections_lock = threading.Lock()
        self.owner_accept_stop = threading.Event()
        self.daemon_directory_fd = None
        self.native_socket_fd = None
        self.prepared = False

    def _accept_owner_connections(self):
        listener = self.owner_listener
        while listener is not None and not self.owner_accept_stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.owner_connections_lock:
                if self.owner_accept_stop.is_set():
                    connection.close()
                else:
                    self.owner_connections.add(connection)

    @staticmethod
    def _find_host_tool(name):
        for search_path in (os.environ.get("PATH"), os.defpath):
            candidate = shutil.which(name, path=search_path)
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return str(Path(candidate).absolute())
        return None

    @classmethod
    def _find_python_runtime(cls):
        candidate = shutil.which("python3", path=os.defpath)
        if not candidate:
            raise RuntimeError("codex_sandbox_system_python3_not_found_on_os_defpath")
        path = Path(candidate).resolve(strict=True)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError("codex_sandbox_system_python3_not_executable")
        return path

    @staticmethod
    def _probe_user_namespace(bwrap, python, uid, gid, runtime_mounts=None):
        runtime_mounts = tuple(runtime_mounts if runtime_mounts is not None else system_runtime_mounts())
        probe_code = ("import ctypes,os; assert ctypes.CDLL(None,use_errno=True).prctl(38,1,0,0,0)==0; "
                      "s=open('/proc/self/status').read(); "
                      f"assert os.getuid()=={uid} and os.geteuid()=={uid}; "
                      f"assert os.getgid()=={gid} and os.getegid()=={gid}; "
                      f"assert set(os.getgroups()).issubset({{{gid}}}); "
                      "assert 'CapEff:\\t0000000000000000' in s and 'NoNewPrivs:\\t1' in s")
        command = [bwrap, "--die-with-parent", "--unshare-user", "--uid", str(uid), "--gid", str(gid),
                   "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts",
                   "--disable-userns", "--cap-drop", "ALL", "--clearenv", "--dir", "/usr",
                   "--symlink", "usr/bin", "/bin", "--dir", "/lib", "--dir", "/lib64",
                   "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        destinations = [destination for _, destination in runtime_mounts]
        parents = set()
        for destination in destinations:
            parent = destination.parent
            while parent != Path("/") and parent not in (Path("/bin"), Path("/proc"), Path("/dev"), Path("/tmp")):
                parents.add(parent)
                parent = parent.parent
        for parent in sorted(parents, key=lambda path: (len(path.parts), str(path))):
            if parent in (Path("/usr"), Path("/lib"), Path("/lib64")): continue
            command.extend(["--dir", str(parent)])
        for source, destination in runtime_mounts:
            command.extend(["--ro-bind", str(source), str(destination)])
        command.extend(["--", str(python), "-c", probe_code])
        try:
            return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=5, check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _safe_directory(path, mode=0o700):
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        if path.is_symlink() or not path.is_dir(): raise ValueError("codex_sandbox_directory_must_be_real")
        path.chmod(mode)

    def prepare(self):
        if self.prepared: raise ValueError("codex_sandbox_already_prepared")
        bwrap = self._find_host_tool("bwrap")
        if not bwrap: raise RuntimeError("codex_sandbox_bubblewrap_not_found_on_path_or_os_defpath")
        env_runtime = shutil.which("env", path=os.defpath)
        if not env_runtime or not Path(env_runtime).is_file() or not os.access(env_runtime, os.X_OK):
            raise RuntimeError("codex_sandbox_system_env_not_found_on_os_defpath")
        self.python_runtime = self._find_python_runtime()
        self.system_runtime = system_runtime_mounts()
        self.user_namespace = self._probe_user_namespace(bwrap, self.python_runtime, os.getuid(), os.getgid(),
                                                         self.system_runtime)
        sudo = None if self.user_namespace else self._find_host_tool("sudo")
        setpriv = None if self.user_namespace else self._find_host_tool("setpriv")
        if not self.user_namespace and not sudo:
            raise RuntimeError("codex_sandbox_user_namespace_unavailable_and_sudo_not_found")
        if not self.user_namespace and not setpriv:
            raise RuntimeError("codex_sandbox_user_namespace_unavailable_and_setpriv_not_found")
        if not Path("/etc/ssl/certs").is_dir():
            raise RuntimeError("codex_sandbox_system_ca_directory_missing_at_etc_ssl_certs")
        self._safe_directory(self.private_root)
        self._safe_directory(self.actor_dir)
        self._safe_directory(self.codex_home)
        relay_dir = self.ipc / "relays"
        daemon_dir = self.ipc / "daemon"
        # IPC is an explicitly run-owned mount. Validate its directory itself;
        # bwrap masks every other host /run and /tmp socket path.
        if self.ipc.is_symlink() or not self.ipc.is_dir(): raise ValueError("codex_sandbox_ipc_must_be_directory")
        self.ipc.chmod(0o700)
        try:
            self._safe_directory(relay_dir)
            self._safe_directory(daemon_dir)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            self.daemon_directory_fd = os.open(daemon_dir, directory_flags)
            owner_path = self.ipc / "sandbox-owner.sock"
            owner_path.unlink(missing_ok=True)
            self.owner_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.owner_listener.bind(str(owner_path)); os.chmod(owner_path, 0o600)
            self.owner_listener.listen(32); self.owner_listener.settimeout(.2)
            self.owner_accept_thread = threading.Thread(target=self._accept_owner_connections,
                                                        daemon=True, name="codex-sandbox-owner")
            self.owner_accept_thread.start()
            for port in self.ports:
                self.forwarders.append(_UnixForwarder(relay_dir / f"tcp-{port}", port).start())
            if self.packages:
                self._safe_directory(self.actor_dir / ".packages")
                self.forwarders.append(_UnixForwarder(relay_dir / "packages", 0, packages=True,
                                                       package_port=self.package_proxy_port).start())
                pip_spec = importlib.util.find_spec("pip")
                if not pip_spec or not pip_spec.origin:
                    raise RuntimeError("codex_sandbox_package_install_requires_host_pip")
                bootstrap = self.actor_dir / ".python-bootstrap"
                if bootstrap.is_symlink(): raise ValueError("codex_sandbox_bootstrap_must_not_be_symlink")
                bootstrap.mkdir(mode=0o700, exist_ok=True)
                pip_target = bootstrap / "pip"
                if pip_target.is_symlink(): raise ValueError("codex_sandbox_pip_bootstrap_must_not_be_symlink")
                if pip_target.exists(): shutil.rmtree(pip_target)
                shutil.copytree(Path(pip_spec.origin).parent, pip_target,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            bwrap_argv = [env_runtime, "-i"]
            bwrap_argv.extend([bwrap] if self.user_namespace else [sudo, "-n", bwrap])
            bwrap_argv.extend(["--die-with-parent", "--new-session", "--unshare-pid", "--unshare-net",
                    "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try", "--clearenv",
                    "--dir", "/usr", "--symlink", "usr/bin", "/bin", "--dir", "/lib", "--dir", "/lib64",
                    "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--chmod", "1777", "/tmp",
                    "--dir", "/etc", "--dir", "/etc/ssl", "--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs",
                    "--dir", "/run/auriga-relays"])
            if self.user_namespace:
                bwrap_argv.extend(["--unshare-user", "--uid", str(os.getuid()), "--gid", str(os.getgid()),
                                   "--disable-userns", "--cap-drop", "ALL"])
            setpriv_runtime = None
            if not self.user_namespace:
                setpriv_runtime = Path(setpriv).resolve(strict=True)
                if not setpriv_runtime.is_file():
                    raise RuntimeError("codex_sandbox_setpriv_not_a_regular_executable")
            extra_runtime_objects = [self.executable]
            if self.rg_executable:
                extra_runtime_objects.append(self.rg_executable)
            if setpriv_runtime:
                extra_runtime_objects.append(setpriv_runtime)
            extra_runtime_mounts = runtime_library_mounts(extra_runtime_objects)
            # bwrap sees a minimal root. Add only empty ancestor directories for
            # the exact mounted paths, never the host home or run parent itself.
            mounts = [(self.actor_dir, self.actor_dir, False),
                      (self.codex_home, self.codex_home, False),
                      (self.ipc, self.ipc, False),
                      (relay_dir, Path("/run/auriga-relays"), True),
                      (daemon_dir, Path(f"/tmp/codex-daemon-{os.getuid()}"), False),
                      (self.executable, self.executable, True)]
            mounts.extend((source, target, True) for source, target in self.system_runtime)
            mounts.extend((source, target, True) for source, target in extra_runtime_mounts)
            if setpriv_runtime:
                if setpriv_runtime.is_relative_to("/usr"):
                    mounts.append((setpriv_runtime, setpriv_runtime, True))
                    self.setpriv_target = str(setpriv_runtime)
                else:
                    mounts.append((setpriv_runtime, Path("/opt/auriga-bin/setpriv"), True))
                    self.setpriv_target = "/opt/auriga-bin/setpriv"
            if self.rg_executable:
                mounts.append((self.rg_executable, Path("/opt/auriga-bin/rg"), True))
            mounts.extend((source, target, True) for source, target in self.read_paths)
            destinations = [target for _, target, _ in mounts]
            parents = set()
            for destination in destinations:
                parent = destination.parent
                while parent != Path("/") and parent not in (Path("/bin"), Path("/proc"), Path("/dev"), Path("/tmp")):
                    parents.add(parent)
                    parent = parent.parent
            for parent in sorted(parents, key=lambda path: (len(path.parts), str(path))):
                if parent in (Path("/usr"), Path("/etc"), Path("/run"), Path("/lib"), Path("/lib64")): continue
                bwrap_argv.extend(["--dir", str(parent)])
            for source, target, readonly in mounts:
                bwrap_argv.extend(["--ro-bind" if readonly else "--bind", str(source), str(target)])
            # Keep the writable app-server IPC mount, but expose relay sockets
            # only through their separate read-only mount below.
            bwrap_argv.extend(["--tmpfs", str(self.ipc / "relays")])
            env = {"PATH": (("/opt/auriga-bin:" if self.rg_executable else "") + os.defpath),
                   "LANG": "C.UTF-8", "TERM": "xterm-256color",
                   "COLORTERM": "truecolor",
                   "HOME": str(self.actor_dir), "CODEX_HOME": str(self.codex_home), "TMPDIR": "/tmp",
                   "AURIGA_OWNER_SOCKET": str(owner_path),
                   "AURIGA_RELAY_PORTS": ",".join(map(str, self.ports)),
                   "AURIGA_PACKAGE_PROXY_PORT": str(self.package_proxy_port if self.packages else 0)}
            if self.packages:
                env.update(PIP_INDEX_URL=f"http://127.0.0.1:{self.package_proxy_port}/simple",
                           PIP_TRUSTED_HOST="127.0.0.1", PIP_CONFIG_FILE="/dev/null",
                           PIP_TARGET=str(self.actor_dir / ".packages"),
                           PYTHONPATH=str(self.actor_dir / ".python-bootstrap") + os.pathsep + str(self.actor_dir / ".packages"))
            ca_path = self.codex_home / "account-control-ca.pem"
            if ca_path.is_file(): env["CODEX_CA_CERTIFICATE"] = str(ca_path)
            for key, value in env.items(): bwrap_argv.extend(["--setenv", key, value])
            bwrap_argv.extend(["--chdir", str(self.actor_dir)])
            if self.user_namespace:
                launcher = [str(self.python_runtime), "-c", _SUPERVISOR]
            else:
                launcher = [self.setpriv_target, "--reuid=" + str(os.getuid()), "--regid=" + str(os.getgid()),
                            "--clear-groups", "--bounding-set=-all", "--inh-caps=-all",
                            "--ambient-caps=-all", "--no-new-privs", str(self.python_runtime), "-c", _SUPERVISOR]
            bwrap_argv.extend(["--", *launcher])
            self.command_prefix = bwrap_argv
            self.env_overrides = {key: value for key, value in env.items()
                                  if key not in {"AURIGA_RELAY_PORTS", "AURIGA_PACKAGE_PROXY_PORT",
                                                 "AURIGA_OWNER_SOCKET"}}
            self.prepared = True
            return self.command_prefix, self.env_overrides
        except BaseException:
            self.close()
            raise

    def close(self):
        self.owner_accept_stop.set()
        if self.owner_listener:
            self.owner_listener.close()
            self.owner_listener = None
        with self.owner_connections_lock:
            owner_connections = tuple(self.owner_connections)
            self.owner_connections.clear()
        for connection in owner_connections:
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            connection.close()
        if self.owner_accept_thread:
            self.owner_accept_thread.join(timeout=1)
            self.owner_accept_thread = None
        self.owner_accept_stop.clear()
        for descriptor_name in ("native_socket_fd", "daemon_directory_fd"):
            descriptor = getattr(self, descriptor_name)
            if descriptor is not None:
                try: os.close(descriptor)
                except OSError: pass
                setattr(self, descriptor_name, None)
        (self.ipc / "sandbox-owner.sock").unlink(missing_ok=True)
        for forwarder in self.forwarders:
            forwarder.close()
        self.forwarders.clear()
        try: (self.ipc / "relays").rmdir()
        except OSError: pass
        daemon_dir = self.ipc / "daemon"
        if daemon_dir.is_dir() and not daemon_dir.is_symlink():
            shutil.rmtree(daemon_dir)
        self.prepared = False

    def repair_socket_alias(self, socket_path):
        """Pin Codex's namespace-only socket inode and repair its shared alias.

        Codex binds its Unix listener below /tmp/codex-daemon-UID even when the
        requested listen URI is in the run IPC directory. Its directory FD is
        pinned before the actor starts, and O_PATH|O_NOFOLLOW pins the socket
        inode itself. Host clients use host_socket_path so actor changes to the
        writable IPC directory or alias cannot redirect a connection.
        """
        requested = Path(socket_path)
        daemon = self.ipc / "daemon"
        if requested.parent != self.ipc or requested.name != "codex.sock":
            raise ValueError("codex_sandbox_socket_alias_not_run_owned")
        try:
            target = Path(os.readlink(requested))
        except (OSError, ValueError):
            return False
        expected_parent = Path(f"/tmp/codex-daemon-{os.getuid()}")
        if target.parent != expected_parent or not re.fullmatch(r"[0-9a-f]{64}", target.name):
            raise ValueError("codex_sandbox_unexpected_native_socket_target")
        try:
            if self.daemon_directory_fd is None:
                raise RuntimeError("codex_sandbox_socket_directory_not_pinned")
            pinned_directory = os.fstat(self.daemon_directory_fd)
            current_directory = daemon.lstat()
            if (not stat.S_ISDIR(current_directory.st_mode)
                    or (current_directory.st_dev, current_directory.st_ino)
                    != (pinned_directory.st_dev, pinned_directory.st_ino)):
                raise ValueError("codex_sandbox_native_socket_directory_changed")
            flags = getattr(os, "O_PATH", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            if not flags:
                raise RuntimeError("codex_sandbox_socket_descriptor_pinning_unavailable")
            native_fd = os.open(target.name, flags, dir_fd=self.daemon_directory_fd)
            info = os.fstat(native_fd)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                os.close(native_fd)
                return False
        except (FileNotFoundError, NotADirectoryError):
            return False
        previous_fd = self.native_socket_fd
        self.native_socket_fd = native_fd
        if previous_fd is not None:
            try: os.close(previous_fd)
            except OSError: pass
        actual = daemon / target.name
        replacement = self.ipc / ".codex.sock.repair"
        replacement.unlink(missing_ok=True)
        os.symlink(actual, replacement)
        os.replace(replacement, requested)
        return True

    @property
    def host_socket_path(self):
        """A host-only path to the pinned native socket inode."""
        if self.native_socket_fd is None:
            raise RuntimeError("codex_sandbox_native_socket_not_pinned")
        return Path(f"/proc/{os.getpid()}/fd/{self.native_socket_fd}")
