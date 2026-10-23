"""HTTP 层：stdlib `http.server`（零第三方运行时依赖）+ 严格的请求校验。

安全设计（本地工具也需要）：
- 只绑 127.0.0.1，不监听 0.0.0.0。
- 每个进程生成一次性 token，注入首页；所有 /api 请求必须带 `X-LintLoom-Token`。
  这样同一台机器上别的网页（哪怕也是 localhost）无法通过 CSRF 驱动本工具的 API。
- `Origin` 必须为空或与 Host 一致，挡住跨站表单。
- POST 只接受 `application/json`，请求体有上限。
- 静态资源是白名单映射，不做目录拼接，天然没有路径穿越。
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import catalog as catalog_mod
from . import config, engine as engine_mod, paths
from .jobs import JobManager

ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

TOKEN_HEADER = "X-LintLoom-Token"


class App:
    def __init__(self, settings: config.Settings) -> None:
        self.settings = settings
        self.catalog = catalog_mod.CatalogService(settings.python_exe)
        self.engine = engine_mod.Engine(settings, self.catalog)
        self.jobs = JobManager()
        self.token = secrets.token_urlsafe(24)
        self.web_root = Path(__file__).resolve().parent / "web"
        self.started_at = time.time()

    def close(self) -> None:
        self.jobs.shutdown()


class Handler(BaseHTTPRequestHandler):
    server_version = f"LintLoom/{config.APP_VERSION}"
    protocol_version = "HTTP/1.1"

    app: App  # 由 make_server 注入

    # ------------------------------------------------------------ 基础工具
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if os.environ.get("LINTLOOM_QUIET"):
            return
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str, code: str = "BAD_REQUEST") -> None:
        self._json(status, {"ok": False, "error": message, "code": code})

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin in (f"http://{host}", f"https://{host}")

    def _token_ok(self) -> bool:
        provided = self.headers.get(TOKEN_HEADER, "")
        return secrets.compare_digest(provided, self.app.token)

    def _read_body(self) -> dict:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError:
            raise paths.ValidationError("Content-Length 非法") from None
        if length < 0 or length > config.MAX_BODY_BYTES:
            raise paths.ValidationError("请求体过大")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise paths.ValidationError("只接受 application/json")
        data = self.rfile.read(length)
        if not data:
            return {}
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise paths.ValidationError(f"JSON 解析失败: {exc}") from None
        if not isinstance(parsed, dict):
            raise paths.ValidationError("请求体必须是 JSON 对象")
        return parsed

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path
            query = urllib.parse.parse_qs(parsed.query)

            if route in ASSETS:
                return self._serve_asset(route)
            if route == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if not route.startswith("/api/"):
                return self._error(HTTPStatus.NOT_FOUND, "未找到该路径", "NOT_FOUND")
            if not self._origin_ok():
                return self._error(HTTPStatus.FORBIDDEN, "跨站请求被拒绝", "ORIGIN")
            if not self._token_ok():
                return self._error(HTTPStatus.FORBIDDEN, "缺少或错误的访问令牌，请从启动时打开的页面访问", "TOKEN")

            if route == "/api/env":
                return self._json(HTTPStatus.OK, {"ok": True, "data": self.app.engine.probe_env()})
            if route == "/api/catalog":
                force = query.get("force", ["0"])[0] in ("1", "true")
                return self._json(HTTPStatus.OK, {"ok": True, "data": self.app.catalog.get(force).as_dict()})
            if route == "/api/tree":
                return self._handle_tree(query)
            if route == "/api/file":
                return self._handle_file(query)
            if route == "/api/jobs":
                return self._json(HTTPStatus.OK, {"ok": True, "data": self.app.jobs.list_recent()})
            if route.startswith("/api/jobs/"):
                job_id = route.rsplit("/", 1)[-1]
                job = self.app.jobs.get(job_id)
                if job is None:
                    return self._error(HTTPStatus.NOT_FOUND, "作业不存在", "JOB_NOT_FOUND")
                return self._json(HTTPStatus.OK, {"ok": True, "data": job.to_dict()})
            return self._error(HTTPStatus.NOT_FOUND, "未知接口", "NOT_FOUND")
        except paths.ValidationError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "VALIDATION")
        except Exception as exc:  # noqa: BLE001
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}", "INTERNAL")

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path
            if not self._origin_ok():
                return self._error(HTTPStatus.FORBIDDEN, "跨站请求被拒绝", "ORIGIN")
            if not self._token_ok():
                return self._error(HTTPStatus.FORBIDDEN, "缺少或错误的访问令牌", "TOKEN")
            body = self._read_body()

            if route == "/api/jobs":
                return self._handle_create_job(body)
            if route.startswith("/api/jobs/") and route.endswith("/cancel"):
                job_id = route.split("/")[3]
                ok = self.app.jobs.cancel(job_id)
                if not ok:
                    return self._error(HTTPStatus.NOT_FOUND, "作业不存在", "JOB_NOT_FOUND")
                return self._json(HTTPStatus.OK, {"ok": True, "data": {"cancelled": job_id}})
            if route == "/api/catalog/reload":
                return self._json(HTTPStatus.OK, {"ok": True, "data": self.app.catalog.get(True).as_dict()})
            return self._error(HTTPStatus.NOT_FOUND, "未知接口", "NOT_FOUND")
        except paths.ValidationError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc), "VALIDATION")
        except Exception as exc:  # noqa: BLE001
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}", "INTERNAL")

    # ------------------------------------------------------------ 处理器
    def _serve_asset(self, route: str) -> None:
        filename, ctype = ASSETS[route]
        target = self.app.web_root / filename
        if not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, f"缺少前端资源 {filename}", "ASSET_MISSING")
        if filename == "index.html":
            html = target.read_text(encoding="utf-8")
            html = html.replace("%%TOKEN%%", self.app.token)
            html = html.replace("%%VERSION%%", config.APP_VERSION)
            html = html.replace("%%UPSTREAM%%", config.UPSTREAM_PINNED)
            body = html.encode("utf-8")
        else:
            body = target.read_bytes()
        self._send(HTTPStatus.OK, body, ctype)

    def _handle_tree(self, query: dict) -> None:
        root = (query.get("root") or [""])[0]
        rel = (query.get("path") or [""])[0]
        settings = self.app.settings
        if not root:
            self._json(
                HTTPStatus.OK,
                {"ok": True, "data": {"roots": [{"label": k, "path": str(v)} for k, v in settings.normalised_roots().items()]}},
            )
            return
        root_label, base = paths.resolve_under_root(
            settings, root, rel or ".", must_exist=True, kind="dir", allow_root=True
        )
        entries = []
        for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name in config.SKIP_DIRS or child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
                if not is_dir and child.suffix.lower() not in config.ALLOWED_EXT:
                    continue
                stat = child.stat()
                rel_child = child.relative_to(settings.normalised_roots()[root_label]).as_posix()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": rel_child,
                    "type": "dir" if is_dir else "file",
                    "size": 0 if is_dir else stat.st_size,
                    "mtime": round(stat.st_mtime, 3),
                }
            )
        self._json(
            HTTPStatus.OK,
            {"ok": True, "data": {"root": root_label, "path": rel, "entries": entries}},
        )

    def _handle_file(self, query: dict) -> None:
        root = (query.get("root") or [""])[0]
        rel = (query.get("path") or [""])[0]
        root_label, target = paths.resolve_under_root(
            self.app.settings, root, rel, must_exist=True, kind="file"
        )
        data = target.read_bytes()
        if len(data) > config.MAX_FILE_BYTES:
            raise paths.ValidationError("文件过大，无法在界面中打开")
        text = data.decode("utf-8", errors="replace")
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "data": {
                    "root": root_label,
                    "path": rel,
                    "content": text,
                    "size": len(data),
                    "lines": text.count("\n") + (0 if text.endswith("\n") else 1),
                    "sha256": engine_mod.sha256_of(target),
                },
            },
        )

    def _handle_create_job(self, body: dict) -> None:
        action = body.get("action")
        allowed = {"lint", "fix_preview", "fix_apply", "parse", "lint_text", "fix_text"}
        if action not in allowed:
            raise paths.ValidationError(f"不支持的 action: {action!r}")

        cat = self.app.catalog.get()
        settings = self.app.settings
        dialect = paths.validate_dialect(body.get("dialect", "ansi"), cat.dialect_labels)
        rules = paths.validate_rule_list(body.get("rules"), cat.rule_refs, "rules")
        exclude = paths.validate_rule_list(body.get("exclude"), cat.rule_refs, "exclude")
        noqa = paths.validate_bool(body.get("noqa"), True)
        timeout = paths.validate_timeout(body.get("timeout"), settings.timeout_cap)

        params: dict = {
            "dialect": dialect,
            "rules": rules,
            "exclude": exclude,
            "noqa": noqa,
            "timeout": timeout,
        }

        if action in ("lint_text", "fix_text"):
            text = body.get("text")
            if not isinstance(text, str):
                raise paths.ValidationError("text 必须是字符串")
            params["text"] = text
            fn = self.app.engine.action_lint_text if action == "lint_text" else self.app.engine.action_fix_text
            job = self.app.jobs.submit(action, params, lambda j: fn(j, **params))
            return self._json(HTTPStatus.ACCEPTED, {"ok": True, "data": {"job_id": job.id}})

        # 文件类动作：根目录 + 相对路径必须同时合法
        root = body.get("root")
        path = body.get("path")
        paths.check_root_label(settings, root)
        _, resolved = paths.resolve_under_root(
            settings, root, path, must_exist=True, kind="any", allow_root=True
        )
        params["root"] = root
        params["path"] = paths.clean_relative_path(path, allow_root=True)

        if action == "parse":
            if resolved.is_dir():
                raise paths.ValidationError("语法树只支持单个 .sql 文件")
            fn = self.app.engine.action_parse
            params_used = {k: params[k] for k in ("root", "path", "dialect", "timeout")}
            job = self.app.jobs.submit(action, params, lambda j: fn(j, **params_used))
            return self._json(HTTPStatus.ACCEPTED, {"ok": True, "data": {"job_id": job.id}})

        if action == "fix_apply":
            confirm = paths.validate_bool(body.get("confirm"), False)
            expected = body.get("expected") or {}
            if not isinstance(expected, dict):
                raise paths.ValidationError("expected 必须是 {file: sha256} 映射")
            if len(expected) > 500:
                raise paths.ValidationError("expected 条目过多")
            clean_expected = {}
            for key, value in expected.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise paths.ValidationError("expected 的键值必须是字符串")
                if not value.isalnum() or len(value) != 64:
                    raise paths.ValidationError("expected 里必须是 sha256 十六进制串")
                clean_expected[key] = value.lower()
            params["confirm"] = confirm
            params["expected"] = clean_expected

        fn = {
            "lint": self.app.engine.action_lint,
            "fix_preview": self.app.engine.action_fix_preview,
            "fix_apply": self.app.engine.action_fix_apply,
        }[action]
        job = self.app.jobs.submit(action, params, lambda j: fn(j, **params))
        self._json(HTTPStatus.ACCEPTED, {"ok": True, "data": {"job_id": job.id}})


def make_server(settings: config.Settings) -> tuple[ThreadingHTTPServer, App]:
    app = App(settings)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((settings.host, settings.port), handler)
    httpd.daemon_threads = True
    return httpd, app


def serve_forever(settings: config.Settings) -> None:
    httpd, app = make_server(settings)
    host, port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{port}/"
    print(f"{config.APP_NAME} {config.APP_VERSION} 已启动: {url}")
    print(f"  解释器      : {settings.python_exe}")
    print(f"  上游目标    : {config.UPSTREAM_REPO} @ {config.UPSTREAM_PINNED}")
    print(f"  根目录      : {', '.join(f'{k}={v}' for k, v in settings.root_map().items())}")
    print(f"  写回开关    : {'允许' if settings.allow_apply else '只读'}")
    print("  访问令牌已注入页面；直接打开上面的地址即可，Ctrl+C 退出。")
    if settings.open_browser:
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止…")
    finally:
        app.close()
        httpd.shutdown()
        httpd.server_close()


def _open_browser(url: str) -> None:
    time.sleep(0.6)
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
