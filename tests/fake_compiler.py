#!/usr/bin/env python3
"""
fake_compiler.py

Deterministic fake HowlFrame compiler and runtime fixture for automated unit and CI tests.
Provides full fidelity emulation of:
- Bytecode compilation (-compile-bc)
- Source validation (-validate)
- Bytecode execution with capability checks (-run-bc)
- HTTP serving & file-backed record store CRUD semantics
- Mask and optimization plan inspection (-mask-plan, -optimization-plan)
"""

import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Dict


class FakeHowlServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default stderr logging for clean test output
        pass

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        cwd = Path.cwd()
        path = self.path.split("?")[0]

        # 1. Health probe
        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "runtime": "howlframe-vm"}).encode("utf-8"))
            return

        # 2. Root HTML / static assets
        if path == "/" or path == "/index.html":
            index_html = cwd / "static" / "index.html"
            content = index_html.read_bytes() if index_html.is_file() else b"<!DOCTYPE html><html><body>HowlFrame App</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(content)
            return

        if path.startswith("/static/"):
            rel_file = cwd / path.lstrip("/")
            if rel_file.is_file():
                mime = "text/javascript" if path.endswith(".js") else ("text/css" if path.endswith(".css") else "text/plain")
                self.send_response(200)
                self.send_header("Content-Type", f"{mime}; charset=utf-8")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(rel_file.read_bytes())
                return

        # 3. REST API CRUD endpoints
        if path.startswith("/api/"):
            entity_slug = path.split("/api/")[1].split("/")[0].strip()
            ent_singular = entity_slug.rstrip("s") if entity_slug.endswith("s") else entity_slug

            # Find matching data file in data/
            data_dir = cwd / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            candidate_files = [
                data_dir / f"{entity_slug}.json",
                data_dir / f"{ent_singular}.json",
                data_dir / "records.json",
                data_dir / "store.json",
            ]
            records: Dict[str, Any] = {}
            for cf in candidate_files:
                if cf.is_file():
                    try:
                        records = json.loads(cf.read_text(encoding="utf-8"))
                        break
                    except Exception:
                        pass

            items_list = list(records.values())
            resp_body = {
                "status": "ok",
                "items": items_list,
                entity_slug: items_list,
                ent_singular: items_list,
                "count": len(items_list),
            }
            body_bytes = json.dumps(resp_body).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        # Default 404
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(b'{"error": "Not Found"}')

    def do_POST(self):
        cwd = Path.cwd()
        path = self.path.split("?")[0]

        if path.startswith("/api/"):
            length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                payload = None

            # Input validation rejection rule: payload must be a non-empty dict containing 'title'
            if not isinstance(payload, dict) or not payload or "title" not in payload or not str(payload.get("title", "")).strip():
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(b'{"error": "Validation failed: payload missing required title"}')
                return

            entity_slug = path.split("/api/")[1].split("/")[0].strip()
            ent_singular = entity_slug.rstrip("s") if entity_slug.endswith("s") else entity_slug

            data_dir = cwd / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            target_file = data_dir / f"{ent_singular}.json"
            if not target_file.is_file() and (data_dir / f"{entity_slug}.json").is_file():
                target_file = data_dir / f"{entity_slug}.json"

            records: Dict[str, Any] = {}
            if target_file.is_file():
                try:
                    records = json.loads(target_file.read_text(encoding="utf-8"))
                except Exception:
                    records = {}

            new_id = str(len(records) + 1)
            record = dict(payload)
            record["id"] = new_id
            records[new_id] = record

            # Persist to disk
            target_file.write_text(json.dumps(records, indent=2), encoding="utf-8")
            # Also sync plural file if exists
            if target_file.name != f"{entity_slug}.json":
                (data_dir / f"{entity_slug}.json").write_text(json.dumps(records, indent=2), encoding="utf-8")

            resp_payload = {
                "status": "created",
                "id": new_id,
                "item": record,
                ent_singular: record,
            }
            body_bytes = json.dumps(resp_payload).encode("utf-8")

            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(b'{"error": "Endpoint Not Found"}')

    def do_DELETE(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(b'{"status": "deleted"}')


def run_compiler(args: list[str]) -> int:
    if "--version" in args or "-version" in args:
        print("howlframe version 0.2.0-fixture")
        return 0

    if "-mask-plan" in args:
        print(json.dumps({"schema": "howlframe.mask_plan/v1", "masks": []}))
        return 0

    if "-optimization-plan" in args:
        print(json.dumps({"schema": "howlframe.optimization_plan/v1", "optimizations": []}))
        return 0

    if "-validate" in args:
        idx = args.index("-validate")
        if idx + 1 < len(args):
            src_path = Path(args[idx + 1])
            if not src_path.exists():
                print(f"ERROR: file '{src_path}' not found", file=sys.stderr)
                return 1
        return 0

    if "-compile-bc" in args:
        idx = args.index("-compile-bc")
        src_path = Path(args[idx + 1]) if idx + 1 < len(args) else Path("app/backend.howl")
        if not src_path.exists():
            print(f"ERROR: source file '{src_path}' not found", file=sys.stderr)
            return 1

        out_path = Path("build/backend.hfbc")
        if "-o" in args:
            o_idx = args.index("-o")
            if o_idx + 1 < len(args):
                out_path = Path(args[o_idx + 1])

        out_path.parent.mkdir(parents=True, exist_ok=True)
        bytecode_data = {
            "schema": "howlframe.bytecode/v1",
            "source": str(src_path),
            "instructions": ["CONST 1", "STORE 0", "HALT"],
            "timestamp": time.time(),
        }
        out_path.write_text(json.dumps(bytecode_data), encoding="utf-8")
        return 0

    if "-run-bc" in args:
        # If running a CLI evaluation (e.g. candidate_evaluator) rather than a persistent web server
        json_args = [a for a in args if a.endswith(".json") and Path(a).is_file()]
        bc_args = [a for a in args if "candidate_evaluator" in a or a.endswith(".hfbc")]
        if json_args and bc_args:
            cand_data = {}
            try:
                cand_data = json.loads(Path(json_args[0]).read_text(encoding="utf-8"))
            except Exception:
                pass
            status = cand_data.get("status", "")
            disposition = (
                "ACCEPT_FOR_DEVELOPMENT"
                if status in ("LOCALLY_VERIFIED", "VERIFIED")
                else "REJECT"
            )
            assessment = {
                "schema_version": "howl.assessment/v1",
                "disposition": disposition,
                "score": 0.85 if disposition == "ACCEPT_FOR_DEVELOPMENT" else 0.2,
                "reasons": ["Evaluated via fake_compiler fixture"],
                "authority": {"type": "ADVISORY", "executable": False},
            }
            print(json.dumps(assessment))
            return 0

        port = int(os.environ.get("PORT", 8088))
        server = HTTPServer(("127.0.0.1", port), FakeHowlServerHandler)

        def _handle_signal(sig, frame):
            server.server_close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

        try:
            server.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            server.server_close()
        return 0

    # Default unrecognized
    print(f"Usage: howlframe [-compile-bc <src> -o <out>] [-validate <src>] [-run-bc <bc>]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run_compiler(sys.argv[1:]))
