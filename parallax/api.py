from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .entitlements import Plan
from .service import PlayService


def handler_for(service: PlayService, resolve_plan=None):
    # Billing/auth integration seam: a trusted server-side principal resolver.
    # No query parameter or client-supplied plan header grants paid access.
    resolve_plan = resolve_plan or (lambda headers: Plan.EXPLORER)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                route = urlsplit(self.path)
                plan = Plan(resolve_plan(self.headers))
                query = parse_qs(route.query, keep_blank_values=True)
                if any(len(v) != 1 for v in query.values()):
                    raise ValueError("Filters cannot be repeated")
                filters = {k: v[0] for k, v in query.items()}
                if route.path == "/plays":
                    payload = service.plays(plan, **filters)
                elif route.path == "/inbox":
                    include_expired = filters.pop("include_expired", "false")
                    if filters:
                        raise ValueError("Inbox does not accept unknown filters")
                    payload = service.inbox(
                        include_expired=include_expired.casefold() == "true"
                    )
                elif route.path == "/signals/publishable":
                    if filters:
                        raise ValueError("Publishable signals do not accept filters")
                    payload = service.publishable_signals(plan)
                elif route.path == "/signals/explained":
                    if filters:
                        raise ValueError("Explained signals do not accept filters")
                    payload = service.explained_signals(plan)
                elif route.path == "/signals":
                    if filters:
                        raise ValueError("Signals do not accept filters")
                    payload = service.signals(plan)
                elif route.path == "/alerts/status":
                    if filters:
                        raise ValueError("Alert status does not accept filters")
                    payload = service.alerts_status()
                elif route.path == "/alerts/recent":
                    limit = int(filters.pop("limit", "20"))
                    if filters:
                        raise ValueError("Recent alerts do not accept unknown filters")
                    payload = service.alerts_recent(limit=limit)
                elif route.path.startswith("/plays/"):
                    payload = service.play(route.path.removeprefix("/plays/"), plan)
                elif route.path == "/markets":
                    payload = service.market_views(plan)
                elif route.path == "/venues":
                    payload = service.venues()
                elif route.path == "/track-record":
                    payload = service.store.summary()
                elif route.path == "/health":
                    payload = service.health()
                else:
                    raise KeyError(route.path)
                self.respond(200, payload)
            except PermissionError as exc:
                self.respond(403, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})
            except KeyError:
                self.respond(404, {"error": "Not found"})
            except Exception:  # noqa: BLE001 - HTTP boundary must not expose internal errors
                self.respond(503, {"error": "Intelligence temporarily unavailable"})

        def do_POST(self):
            try:
                route = urlsplit(self.path)
                if route.query:
                    raise ValueError("POST routes do not accept query parameters")
                if (
                    route.path.startswith("/inbox/")
                    and route.path.endswith("/seen")
                    and len(route.path.split("/")) == 4
                ):
                    inbox_id = route.path.split("/")[2]
                    payload = service.mark_inbox_seen(inbox_id)
                else:
                    raise KeyError(route.path)
                self.respond(200, payload)
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})
            except KeyError:
                self.respond(404, {"error": "Not found"})
            except Exception:  # noqa: BLE001 - HTTP boundary must not expose internal errors
                self.respond(503, {"error": "Intelligence temporarily unavailable"})

        def respond(self, status, payload):
            body = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    return Handler


def server(service: PlayService, port: int = 8765, resolve_plan=None):
    return ThreadingHTTPServer(("127.0.0.1", port), handler_for(service, resolve_plan))
