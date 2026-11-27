import atexit
import base64
import json
import os
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests


def _b64_decode(data: str) -> str:
    data = data.strip().replace("-", "+").replace("_", "/")
    padding = len(data) % 4
    if padding:
        data += "=" * (4 - padding)
    return base64.b64decode(data).decode("utf-8", errors="ignore")


def parse_proxy_uri(uri: str) -> dict:
    uri = uri.strip()
    if uri.startswith("vless://"):
        return _parse_vless(uri)
    if uri.startswith("vmess://"):
        return _parse_vmess(uri)
    if uri.startswith(("hy2://", "hysteria2://")):
        return _parse_hysteria2(uri)
    if uri.startswith("trojan://"):
        return _parse_trojan(uri)
    if uri.startswith("ss://"):
        return _parse_ss(uri)
    raise ValueError(f"不支持的代理协议: {uri.split('://')[0]}")


def _parse_vless(uri: str) -> dict:
    parsed = urlparse(uri)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    outbound = {
        "type": "vless",
        "tag": "proxy",
        "server": parsed.hostname,
        "server_port": parsed.port or 443,
        "uuid": unquote(parsed.username or ""),
    }

    flow = params.get("flow")
    if flow:
        outbound["flow"] = flow

    security = params.get("security", "none").lower()
    if security in ("tls", "reality"):
        tls_cfg = {
            "enabled": True,
            "server_name": params.get("sni") or parsed.hostname,
            "insecure": params.get("allowInsecure", "0") in ("1", "true"),
        }
        fp = params.get("fp")
        if fp:
            tls_cfg["utls"] = {"enabled": True, "fingerprint": fp}
        alpn = params.get("alpn")
        if alpn:
            tls_cfg["alpn"] = [x.strip() for x in unquote(alpn).split(",") if x.strip()]
        if security == "reality":
            tls_cfg["reality"] = {
                "enabled": True,
                "public_key": params.get("pbk", ""),
                "short_id": params.get("sid", ""),
            }
        outbound["tls"] = tls_cfg

    net_type = params.get("type", "tcp").lower()
    if net_type == "ws":
        ws_cfg = {"type": "ws", "path": unquote(params.get("path", "/"))}
        host = params.get("host")
        if host:
            ws_cfg["headers"] = {"Host": host}
        outbound["transport"] = ws_cfg
    elif net_type == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": params.get("serviceName", ""),
        }
    elif net_type == "httpupgrade":
        hu_cfg = {"type": "httpupgrade", "path": unquote(params.get("path", "/"))}
        if params.get("host"):
            hu_cfg["host"] = params["host"]
        outbound["transport"] = hu_cfg

    return outbound


def _parse_vmess(uri: str) -> dict:
    raw = _b64_decode(uri[len("vmess://"):])
    cfg = json.loads(raw)

    outbound = {
        "type": "vmess",
        "tag": "proxy",
        "server": cfg["add"],
        "server_port": int(cfg["port"]),
        "uuid": cfg["id"],
        "alter_id": int(cfg.get("aid", 0)),
        "security": cfg.get("scy") or "auto",
    }

    if str(cfg.get("tls", "")).lower() == "tls":
        tls_cfg = {
            "enabled": True,
            "server_name": cfg.get("sni") or cfg.get("host") or cfg["add"],
            "insecure": str(cfg.get("skip-cert-verify", "")).lower() in ("1", "true"),
        }
        if cfg.get("fp"):
            tls_cfg["utls"] = {"enabled": True, "fingerprint": cfg["fp"]}
        outbound["tls"] = tls_cfg

    net = str(cfg.get("net", "tcp")).lower()
    if net == "ws":
        ws_cfg = {"type": "ws", "path": cfg.get("path") or "/"}
        if cfg.get("host"):
            ws_cfg["headers"] = {"Host": cfg["host"]}
        outbound["transport"] = ws_cfg
    elif net == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": cfg.get("path") or "",
        }

    return outbound


def _parse_hysteria2(uri: str) -> dict:
    normalized = uri.replace("hy2://", "hysteria2://", 1)
    parsed = urlparse(normalized)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    outbound = {
        "type": "hysteria2",
        "tag": "proxy",
        "server": parsed.hostname,
        "server_port": parsed.port or 443,
        "password": unquote(parsed.username or ""),
        "tls": {
            "enabled": True,
            "server_name": params.get("sni") or parsed.hostname,
            "insecure": params.get("insecure", "0") in ("1", "true"),
        },
    }

    obfs_type = params.get("obfs")
    if obfs_type:
        outbound["obfs"] = {
            "type": obfs_type,
            "password": unquote(params.get("obfs-password", "")),
        }

    return outbound


def _parse_trojan(uri: str) -> dict:
    parsed = urlparse(uri)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

    outbound = {
        "type": "trojan",
        "tag": "proxy",
        "server": parsed.hostname,
        "server_port": parsed.port or 443,
        "password": unquote(parsed.username or ""),
        "tls": {
            "enabled": True,
            "server_name": params.get("sni") or parsed.hostname,
            "insecure": params.get("allowInsecure", "0") in ("1", "true"),
        },
    }

    if params.get("fp"):
        outbound["tls"]["utls"] = {"enabled": True, "fingerprint": params["fp"]}

    net_type = params.get("type", "tcp").lower()
    if net_type == "ws":
        ws_cfg = {"type": "ws", "path": unquote(params.get("path", "/"))}
        if params.get("host"):
            ws_cfg["headers"] = {"Host": params["host"]}
        outbound["transport"] = ws_cfg
    elif net_type == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": params.get("serviceName", ""),
        }

    return outbound


def _parse_ss(uri: str) -> dict:
    parsed = urlparse(uri)
    if parsed.username and parsed.hostname:
        user_info = unquote(parsed.username)
        if ":" not in user_info:
            user_info = _b64_decode(user_info)
        method, password = user_info.split(":", 1)
        server, port = parsed.hostname, parsed.port
    else:
        body = uri[len("ss://"):].split("#", 1)[0]
        decoded = _b64_decode(body)
        cred, addr = decoded.rsplit("@", 1)
        method, password = cred.split(":", 1)
        server, port_str = addr.rsplit(":", 1)
        port = int(port_str)

    return {
        "type": "shadowsocks",
        "tag": "proxy",
        "server": server,
        "server_port": port,
        "method": method,
        "password": password,
    }


class SingBoxManager:
    def __init__(self, proxy_uri: str, local_port: int = 10808, bin_path: str = "sing-box"):
        self.proxy_uri = proxy_uri
        self.local_port = local_port
        self.bin_path = bin_path
        self.proc = None
        self.config_path = None

    def start(self) -> str:
        outbound = parse_proxy_uri(self.proxy_uri)
        config = {
            "log": {"level": "warn"},
            "inbounds": [
                {
                    "type": "mixed",
                    "tag": "mixed-in",
                    "listen": "127.0.0.1",
                    "listen_port": self.local_port,
                }
            ],
            "outbounds": [outbound],
        }

        fd, self.config_path = tempfile.mkstemp(prefix="singbox_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        self.proc = subprocess.Popen(
            [self.bin_path, "run", "-c", self.config_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        atexit.register(self.stop)

        # 等待代理端口就绪并校验出口 IP
        proxies = {
            "http": f"http://127.0.0.1:{self.local_port}",
            "https": f"http://127.0.0.1:{self.local_port}",
        }
        for _ in range(12):
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode("utf-8", errors="ignore")
                raise RuntimeError(f"sing-box 启动失败: {err[:300]}")
            try:
                resp = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=8)
                if resp.status_code == 200:
                    ip = resp.json().get("ip", "unknown")
                    print(f"[sing-box] 代理已就绪，当前出口 IP: {ip}")
                    return ip
            except Exception:
                time.sleep(1.5)

        raise RuntimeError("sing-box 启动超时或节点不可连通")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.config_path and os.path.exists(self.config_path):
            try:
                os.remove(self.config_path)
            except OSError:
                pass
