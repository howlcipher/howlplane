#!/usr/bin/env python3
"""
acceptance_runner.py

Black-box acceptance test runner for synthesized HowlFrame products.
Validates observable product behavior: compilation, process lifecycle,
HTTP health probes, static browser assets, REST API CRUD semantics,
input validation rejection, and restart persistence.
"""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

from howlplane.control_plane.synthesis.product_spec import ProductSpec
from howlplane.control_plane.task_spec import DataClassSerializationMixin

ACCEPTANCE_REPORT_SCHEMA_VERSION = "howlplane.acceptance_report/v1"


@dataclass
class AcceptanceCheckResult(DataClassSerializationMixin):
    """Individual acceptance check outcome."""

    name: str
    description: str
    status: str  # "passed", "failed", "skipped"
    duration_seconds: float
    evidence: str = ""
    error_message: Optional[str] = None


@dataclass
class ProductAcceptanceReport(DataClassSerializationMixin):
    """Aggregated acceptance report across all product criteria."""

    product_name: str
    all_passed: bool
    passed_count: int
    total_count: int
    duration_seconds: float
    checks: List[AcceptanceCheckResult] = field(default_factory=list)
    schema: str = ACCEPTANCE_REPORT_SCHEMA_VERSION

    def render_markdown(self) -> str:
        lines = [
            f"# Acceptance Verification: {self.product_name}",
            "",
            f"- **Status:** {'✓ PASSED' if self.all_passed else '✗ FAILED'}",
            f"- **Summary:** {self.passed_count}/{self.total_count} checks passed ({self.duration_seconds}s)",
            "",
            "| Check | Status | Duration | Evidence / Error |",
            "| --- | --- | --- | --- |",
        ]
        for c in self.checks:
            mark = "✓ PASS" if c.status == "passed" else "✗ FAIL"
            detail = c.evidence if c.status == "passed" else (c.error_message or "Unknown failure")
            clean_detail = detail.replace("\n", " ").strip()[:100]
            lines.append(f"| {c.name} | {mark} | {c.duration_seconds}s | {clean_detail} |")
        lines.append("")
        return "\n".join(lines)


def find_free_port() -> int:
    """Finds an available ephemeral TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s.getsockname()[1]


class ProductAcceptanceRunner:
    """
    Executes automated black-box acceptance tests against a synthesized product bundle.
    """

    def __init__(self, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds

    def run_acceptance_suite(self, product_dir: Path, spec: ProductSpec) -> ProductAcceptanceReport:
        """
        Executes complete black-box verification suite against product bundle.
        """
        checks: List[AcceptanceCheckResult] = []
        start_time = time.time()
        prod_path = Path(product_dir).resolve()

        # 1. Check: Build Compilation
        build_check = self._check_build(prod_path)
        checks.append(build_check)
        if build_check.status != "passed":
            total_dur = round(time.time() - start_time, 3)
            return ProductAcceptanceReport(
                product_name=spec.name,
                all_passed=False,
                passed_count=0,
                total_count=len(checks),
                duration_seconds=total_dur,
                checks=checks,
            )

        # 2. Check: Static Assets Presence
        if "browser_ui" in spec.interfaces:
            checks.append(self._check_static_assets(prod_path))

        # 3. Dynamic HTTP & Persistence Checks
        if "http_api" in spec.interfaces or "browser_ui" in spec.interfaces:
            http_checks = self._run_http_and_persistence_checks(prod_path, spec)
            checks.extend(http_checks)

        passed = sum(1 for c in checks if c.status == "passed")
        total = len(checks)
        all_passed = (passed == total and total > 0)
        total_dur = round(time.time() - start_time, 3)

        return ProductAcceptanceReport(
            product_name=spec.name,
            all_passed=all_passed,
            passed_count=passed,
            total_count=total,
            duration_seconds=total_dur,
            checks=checks,
        )

    def _check_build(self, prod_path: Path) -> AcceptanceCheckResult:
        t0 = time.time()
        build_script = prod_path / "scripts" / "build.sh"
        if not build_script.exists():
            return AcceptanceCheckResult(
                name="build_script_exists",
                description="Verify scripts/build.sh exists in product bundle",
                status="failed",
                duration_seconds=round(time.time() - t0, 3),
                error_message="scripts/build.sh not found",
            )

        try:
            res = subprocess.run(
                ["bash", str(build_script)],
                cwd=str(prod_path),
                capture_output=True,
                text=True,
                timeout=20,
            )
            dur = round(time.time() - t0, 3)
            if res.returncode == 0:
                return AcceptanceCheckResult(
                    name="build_compilation",
                    description="Compile product bytecode and assets via scripts/build.sh",
                    status="passed",
                    duration_seconds=dur,
                    evidence="Compiled bytecode and static assets with exit code 0",
                )
            else:
                err = (res.stderr + "\n" + res.stdout).strip()
                return AcceptanceCheckResult(
                    name="build_compilation",
                    description="Compile product bytecode and assets via scripts/build.sh",
                    status="failed",
                    duration_seconds=dur,
                    error_message=f"Build failed with exit {res.returncode}: {err[:300]}",
                )
        except Exception as exc:
            return AcceptanceCheckResult(
                name="build_compilation",
                description="Compile product bytecode and assets via scripts/build.sh",
                status="failed",
                duration_seconds=round(time.time() - t0, 3),
                error_message=str(exc),
            )

    def _check_static_assets(self, prod_path: Path) -> AcceptanceCheckResult:
        t0 = time.time()
        index_html = prod_path / "static" / "index.html"
        app_js = prod_path / "static" / "app.js"
        if not index_html.exists():
            return AcceptanceCheckResult(
                name="browser_static_assets",
                description="Verify static/index.html exists",
                status="failed",
                duration_seconds=round(time.time() - t0, 3),
                error_message="static/index.html not found",
            )
        return AcceptanceCheckResult(
            name="browser_static_assets",
            description="Verify browser UI assets (index.html, app.js)",
            status="passed",
            duration_seconds=round(time.time() - t0, 3),
            evidence=f"Found static/index.html ({index_html.stat().st_size} bytes)" + (f" and static/app.js ({app_js.stat().st_size} bytes)" if app_js.exists() else ""),
        )

    def _run_http_and_persistence_checks(self, prod_path: Path, spec: ProductSpec) -> List[AcceptanceCheckResult]:
        checks: List[AcceptanceCheckResult] = []
        port = spec.default_port
        base_url = f"http://127.0.0.1:{port}"

        # Clean existing data store for fresh test
        data_dir = prod_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        ent_name = list(spec.entities.keys())[0] if spec.entities else "Item"
        ent_slug = ent_name.lower()
        (data_dir / f"{ent_slug}.json").write_text("{}", encoding="utf-8")
        clean_store_rel = spec.persistence.storage_path.replace("file://", "") if spec.persistence.storage_path.startswith("file://") else spec.persistence.storage_path
        if clean_store_rel.startswith("data/"):
            (prod_path / clean_store_rel).write_text("{}", encoding="utf-8")

        server_proc = self._start_server(prod_path, port)
        if server_proc is None:
            checks.append(AcceptanceCheckResult(
                name="server_lifecycle_start",
                description="Start server process",
                status="failed",
                duration_seconds=0.1,
                error_message="Could not start server process",
            ))
            return checks

        try:
            # 1. Health / Ready probe
            health_check = self._probe_health(base_url, timeout=5.0)
            checks.append(health_check)
            if health_check.status != "passed":
                return checks

            # 2. Static root probe
            if "browser_ui" in spec.interfaces:
                checks.append(self._check_http_endpoint(
                    name="http_root_html",
                    description="Root endpoint serves HTML",
                    url=f"{base_url}/",
                    method="GET",
                    expected_status=200,
                    content_type="text/html",
                ))

            # 3. Entity CRUD checks
            ent_name = list(spec.entities.keys())[0] if spec.entities else "Item"
            endpoint_slug = ent_name.lower() + "s"
            api_url = f"{base_url}/api/{endpoint_slug}"

            # Create Record 1
            payload_1 = {"title": "First Acceptance Test Item", "content": "Testing prompt to product synthesis."} if ent_name == "Note" else {"title": "First Task", "completed": False}
            create_check = self._check_create(api_url, payload_1)
            checks.append(create_check)

            # Retrieve List
            checks.append(self._check_list(api_url))

            # Input Validation Check (Rejected bad payload)
            bad_payload = {"content": "Missing title"} if ent_name == "Note" else {}
            checks.append(self._check_validation_rejection(api_url, bad_payload))

            # Create Record 2 for restart persistence
            payload_2 = {"title": "Restart Persistence Item", "content": "Must survive server process restart."} if ent_name == "Note" else {"title": "Restart Task", "completed": True}
            self._http_request(api_url, method="POST", data=payload_2)

        finally:
            self._stop_server(server_proc)

        # 4. Restart Persistence Check: Restart server and verify Record 2 survives
        if spec.persistence.survives_restart:
            t0 = time.time()
            server_proc_2 = self._start_server(prod_path, port)
            if server_proc_2 is None:
                checks.append(AcceptanceCheckResult(
                    name="restart_persistence",
                    description="Verify state persists across server restart",
                    status="failed",
                    duration_seconds=round(time.time() - t0, 3),
                    error_message="Could not restart server process",
                ))
            else:
                try:
                    self._probe_health(base_url, timeout=5.0)
                    status_code, body_text = self._http_request(api_url, method="GET")
                    dur = round(time.time() - t0, 3)
                    if status_code == 200 and ("Restart Persistence Item" in body_text or "Restart Task" in body_text or "First Acceptance" in body_text):
                        checks.append(AcceptanceCheckResult(
                            name="restart_persistence",
                            description="Verify state persists across server restart",
                            status="passed",
                            duration_seconds=dur,
                            evidence="Records retained across process reboot in file store.",
                        ))
                    else:
                        checks.append(AcceptanceCheckResult(
                            name="restart_persistence",
                            description="Verify state persists across server restart",
                            status="failed",
                            duration_seconds=dur,
                            error_message=f"Persisted record not found after restart (HTTP {status_code}): {body_text[:200]}",
                        ))
                finally:
                    self._stop_server(server_proc_2)

        return checks

    def _start_server(self, prod_path: Path, port: int) -> Optional[subprocess.Popen]:
        run_script = prod_path / "scripts" / "run.sh"
        env = os.environ.copy()
        env["PORT"] = str(port)

        # If run.sh exists, use it with PORT env
        if run_script.exists():
            cmd = ["bash", str(run_script), str(port)]
        else:
            howlframe_bin = os.environ.get("HOWLFRAME_BIN") or shutil.which("howlframe") or "howlframe"
            bc_file = prod_path / "build" / "backend.hfbc"
            cmd = [howlframe_bin, "-run-bc", "-allow-caps", "network,database,filesystem", str(bc_file)]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(prod_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return proc
        except Exception:
            return None

    def _stop_server(self, proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _probe_health(self, base_url: str, timeout: float = 5.0) -> AcceptanceCheckResult:
        t0 = time.time()
        deadline = t0 + timeout
        last_err = ""
        while time.time() < deadline:
            for endpoint in ["/health", "/api/notes", "/api/tasks", "/api/records", "/"]:
                try:
                    code, body = self._http_request(f"{base_url}{endpoint}", method="GET")
                    if code in (200, 201):
                        return AcceptanceCheckResult(
                            name="server_health_probe",
                            description="Server responds to health check probe",
                            status="passed",
                            duration_seconds=round(time.time() - t0, 3),
                            evidence=f"Responded {code} on {endpoint}",
                        )
                except Exception as exc:
                    last_err = str(exc)
            time.sleep(0.1)

        return AcceptanceCheckResult(
            name="server_health_probe",
            description="Server responds to health check probe",
            status="failed",
            duration_seconds=round(time.time() - t0, 3),
            error_message=f"Server did not respond within {timeout}s: {last_err}",
        )

    def _check_http_endpoint(
        self,
        name: str,
        description: str,
        url: str,
        method: str = "GET",
        expected_status: int = 200,
        content_type: Optional[str] = None,
    ) -> AcceptanceCheckResult:
        t0 = time.time()
        try:
            code, body = self._http_request(url, method=method)
            dur = round(time.time() - t0, 3)
            if code == expected_status:
                return AcceptanceCheckResult(
                    name=name,
                    description=description,
                    status="passed",
                    duration_seconds=dur,
                    evidence=f"HTTP {code} received ({len(body)} bytes)",
                )
            else:
                return AcceptanceCheckResult(
                    name=name,
                    description=description,
                    status="failed",
                    duration_seconds=dur,
                    error_message=f"Expected HTTP {expected_status}, received {code}: {body[:200]}",
                )
        except Exception as exc:
            return AcceptanceCheckResult(
                name=name,
                description=description,
                status="failed",
                duration_seconds=round(time.time() - t0, 3),
                error_message=str(exc),
            )

    def _check_create(self, url: str, payload: Dict[str, Any]) -> AcceptanceCheckResult:
        t0 = time.time()
        try:
            code, body = self._http_request(url, method="POST", data=payload)
            dur = round(time.time() - t0, 3)
            ok = code in (200, 201)
            return AcceptanceCheckResult(
                name="create_entity_crud",
                description="Create entity record via POST",
                status="passed" if ok else "failed",
                duration_seconds=dur,
                evidence=f"Record created successfully (HTTP {code}): {body[:100]}" if ok else None,
                error_message=f"Expected HTTP 200/201, got {code}: {body[:200]}" if not ok else None,
            )
        except Exception as exc:
            return AcceptanceCheckResult(
                name="create_entity_crud",
                description="Create entity record via POST",
                status="failed",
                duration_seconds=round(time.time() - t0, 3),
                error_message=str(exc),
            )

    def _check_list(self, url: str) -> AcceptanceCheckResult:
        t0 = time.time()
        try:
            code, body = self._http_request(url, method="GET")
            dur = round(time.time() - t0, 3)
            ok = code == 200
            return AcceptanceCheckResult(
                name="list_entities_crud",
                description="List entities via GET",
                status="passed" if ok else "failed",
                duration_seconds=dur,
                evidence=f"Retrieved entity list (HTTP 200): {body[:100]}" if ok else None,
                error_message=f"Expected HTTP 200, got {code}: {body[:200]}" if not ok else None,
            )
        except Exception as exc:
            return AcceptanceCheckResult(
                name="list_entities_crud",
                description="List entities via GET",
                status="failed",
                duration_seconds=round(time.time() - t0, 3),
                error_message=str(exc),
            )

    def _check_validation_rejection(self, url: str, bad_payload: Dict[str, Any]) -> AcceptanceCheckResult:
        t0 = time.time()
        try:
            code, body = self._http_request(url, method="POST", data=bad_payload)
            dur = round(time.time() - t0, 3)
            ok = code in (400, 422)
            return AcceptanceCheckResult(
                name="input_validation_rejection",
                description="Invalid input payload rejected with 400 Bad Request",
                status="passed" if ok else "failed",
                duration_seconds=dur,
                evidence=f"Invalid payload rejected with HTTP {code}: {body[:100]}" if ok else None,
                error_message=f"Expected HTTP 400 on invalid payload, received {code}: {body[:200]}" if not ok else None,
            )
        except urllib.error.HTTPError as exc:
            dur = round(time.time() - t0, 3)
            ok = exc.code in (400, 422)
            return AcceptanceCheckResult(
                name="input_validation_rejection",
                description="Invalid input payload rejected with 400 Bad Request",
                status="passed" if ok else "failed",
                duration_seconds=dur,
                evidence=f"Invalid payload rejected with HTTP {exc.code}" if ok else None,
                error_message=f"Received HTTP {exc.code}" if not ok else None,
            )
        except Exception as exc:
            return AcceptanceCheckResult(
                name="input_validation_rejection",
                description="Invalid input payload rejected with 400 Bad Request",
                status="failed",
                duration_seconds=round(time.time() - t0, 3),
                error_message=str(exc),
            )

    def _http_request(self, url: str, method: str = "GET", data: Optional[Dict[str, Any]] = None) -> Tuple[int, str]:
        req_data = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/html, */*"}
        req = urllib.request.Request(url, data=req_data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:  # nosec B310
                body = resp.read().decode("utf-8", errors="replace")
                return resp.status, body
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace") if err.fp else ""
            return err.code, body
