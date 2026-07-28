#!/usr/bin/env python3
import json
import os
import ssl
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


class DashboardApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        ca_cert: str | None = None,
        timeout: int = 60,
    ):
        parsed_url = urlsplit(base_url)
        if not parsed_url.hostname or parsed_url.username or parsed_url.password:
            raise ValueError(
                "DASHBOARD_API_URL must be an absolute URL without credentials."
            )
        if parsed_url.query or parsed_url.fragment:
            raise ValueError(
                "DASHBOARD_API_URL must not include a query string or fragment."
            )
        if parsed_url.scheme == "http":
            if parsed_url.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError(
                    "DASHBOARD_API_URL must use HTTPS unless it targets a loopback host."
                )
            if ca_cert:
                raise ValueError("DASHBOARD_API_CA_CERT is only valid with HTTPS.")
        elif parsed_url.scheme != "https":
            raise ValueError(
                "DASHBOARD_API_URL must use HTTPS unless it targets a loopback host."
            )
        if not token.strip():
            raise ValueError("Dashboard API token is required.")
        if timeout <= 0:
            raise ValueError("DASHBOARD_API_TIMEOUT must be greater than zero.")
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = timeout
        self.ssl_context = (
            ssl.create_default_context(cafile=ca_cert or None)
            if parsed_url.scheme == "https"
            else None
        )

    @classmethod
    def from_environment(cls) -> "DashboardApiClient":
        token_file = os.environ.get("DASHBOARD_API_TOKEN_FILE", "").strip()
        if not token_file:
            raise ValueError("DASHBOARD_API_TOKEN_FILE is required.")
        token_path = Path(token_file).expanduser().resolve()
        token = token_path.read_text(encoding="utf-8").strip()
        return cls(
            base_url=os.environ.get("DASHBOARD_API_URL", "").strip(),
            token=token,
            ca_cert=os.environ.get("DASHBOARD_API_CA_CERT", "").strip() or None,
            timeout=int(os.environ.get("DASHBOARD_API_TIMEOUT", "60")),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            values = {key: value for key, value in query.items() if value not in (None, "")}
            if values:
                url = f"{url}?{urlencode(values)}"
        encoded = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "dashboard-platform-mcp/1.0",
        }
        if payload is not None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method.upper() == "POST":
            headers["Idempotency-Key"] = idempotency_key or str(uuid4())
        request = Request(url, data=encoded, headers=headers, method=method.upper())
        try:
            open_options: dict[str, Any] = {"timeout": self.timeout}
            if self.ssl_context is not None:
                open_options["context"] = self.ssl_context
            with urlopen(request, **open_options) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
                detail = body.get("error", {})
                code = detail.get("code", "api_error")
                message = detail.get("message", f"HTTP {error.code}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                code = "api_error"
                message = f"HTTP {error.code}"
            raise RuntimeError(f"Dashboard API {code}: {message}") from error
        except URLError as error:
            raise RuntimeError("Dashboard API에 연결할 수 없습니다.") from error

    def get(self, path: str, **query) -> dict[str, Any]:
        return self.request("GET", path, query=query)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, payload=payload)

    def patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", path, payload=payload)


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP(
        "dashboard-platform",
        instructions=(
            "Dashboard Platform API를 통해 작업, 아티팩트, 승인과 감사 이력을 관리합니다. "
            "처음 사용하는 에이전트는 read_mcp_guide를 먼저 호출하세요. "
            "승인 전 외부 부작용을 실행하지 말고, 긴 결과는 create_artifact로 저장하세요."
        ),
    )
    client = DashboardApiClient.from_environment()
    docs_root = Path(__file__).resolve().parent.parent / "docs"

    @server.resource("dashboard://guides/mcp", mime_type="text/markdown")
    def mcp_guide() -> str:
        """설치부터 운영까지 한 문서로 설명하는 Dashboard MCP A-Z 가이드입니다."""
        return (docs_root / "MCP_TOOLS.md").read_text(encoding="utf-8")

    @server.resource("dashboard://guides/agent", mime_type="text/markdown")
    def agent_guide() -> str:
        """에이전트가 플랫폼 작업을 처리할 때 따라야 하는 필수 규칙입니다."""
        return (docs_root / "AGENT_GUIDE.md").read_text(encoding="utf-8")

    @server.resource("dashboard://guides/api", mime_type="text/markdown")
    def api_guide() -> str:
        """인증, scope, 멱등성과 API 호출 규칙입니다."""
        return (docs_root / "API_GUIDE.md").read_text(encoding="utf-8")

    @server.resource("dashboard://guides/workflow", mime_type="text/markdown")
    def workflow_guide() -> str:
        """상태 전이, 아티팩트 리비전과 승인 정책입니다."""
        return (docs_root / "WORKFLOW_GUIDE.md").read_text(encoding="utf-8")

    @server.tool()
    def read_mcp_guide() -> str:
        """처음 연결할 때 읽어야 하는 설치·도구·워크플로·문제 해결 A-Z 가이드입니다."""
        return (docs_root / "MCP_TOOLS.md").read_text(encoding="utf-8")

    @server.tool()
    def search_context(query: str, limit: int = 20) -> dict:
        """플랫폼 작업과 기존 대시보드 지식을 함께 검색합니다."""
        return client.get("search/", q=query, limit=limit)

    @server.tool()
    def list_agents(limit: int = 100) -> dict:
        """등록된 활성 에이전트와 기능을 조회합니다."""
        return client.get("agents/", limit=limit)

    @server.tool()
    def list_tasks(
        status: str = "",
        assigned_agent: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """플랫폼 작업 목록을 상태나 담당 에이전트로 조회합니다."""
        return client.get(
            "tasks/",
            status=status,
            assigned_agent=assigned_agent,
            limit=limit,
            offset=offset,
        )

    @server.tool()
    def collect_item(
        title: str,
        content: str,
        source_type: str,
        external_id: str = "",
        source_url: str = "",
    ) -> dict:
        """뉴스, Slack, SNS 등 외부 원본을 플랫폼 inbox에 수집합니다."""
        return client.post(
            "inbox/",
            {
                "title": title,
                "content": content,
                "source_type": source_type,
                "external_id": external_id,
                "source_url": source_url,
            },
        )

    @server.tool()
    def create_task(
        title: str,
        description: str = "",
        inbox_item_id: str = "",
        assigned_agents: list[str] | None = None,
        priority: str = "normal",
    ) -> dict:
        """수집 원본 또는 사용자 요청을 처리할 플랫폼 작업을 생성합니다."""
        payload: dict[str, Any] = {
            "title": title,
            "description": description,
            "priority": priority,
            "assigned_agents": assigned_agents or [],
        }
        if inbox_item_id:
            payload["inbox_item_id"] = inbox_item_id
        return client.post("tasks/", payload)

    @server.tool()
    def update_task_status(task_id: str, status: str) -> dict:
        """허용된 워크플로 상태 전이에 따라 작업 상태를 변경합니다."""
        return client.patch(f"tasks/{task_id}/", {"status": status})

    @server.tool()
    def get_task_context(task_id: str) -> dict:
        """원본, 모든 아티팩트 리비전, 승인과 이벤트를 한 번에 조회합니다."""
        return client.get(f"tasks/{task_id}/context/")

    @server.tool()
    def create_artifact(
        task_id: str,
        kind: str,
        title: str,
        content: str,
        series_id: str = "",
        mime_type: str = "text/markdown",
    ) -> dict:
        """작업 결과를 불변 아티팩트 리비전으로 저장합니다."""
        payload = {
            "task_id": task_id,
            "kind": kind,
            "title": title,
            "content": content,
            "mime_type": mime_type,
        }
        if series_id:
            payload["series_id"] = series_id
        return client.post("artifacts/", payload)

    @server.tool()
    def submit_analysis(
        task_id: str,
        title: str,
        content: str,
        series_id: str = "",
    ) -> dict:
        """분석 아티팩트를 저장하고 작업을 검토 대기 상태로 전환합니다."""
        artifact = create_artifact(
            task_id=task_id,
            kind="analysis",
            title=title,
            content=content,
            series_id=series_id,
        )
        task = update_task_status(task_id, "needs_review")
        return {"artifact": artifact["data"], "task": task["data"]}

    @server.tool()
    def request_approval(task_id: str, artifact_id: str, note: str = "") -> dict:
        """특정 아티팩트 해시에 대해 관리자 승인 또는 수정 요청을 받습니다."""
        return client.post(
            "approvals/",
            {"task_id": task_id, "artifact_id": artifact_id, "note": note},
        )

    @server.tool()
    def decide_approval(approval_id: str, decision: str, note: str = "") -> dict:
        """관리자 권한으로 승인, 거절 또는 수정 요청을 기록합니다."""
        return client.post(
            f"approvals/{approval_id}/decision/",
            {"decision": decision, "note": note},
        )

    @server.tool()
    def get_workflow_history(task_id: str) -> dict:
        """현재 작업 상태와 감사 가능한 상태 변경 이력을 조회합니다."""
        return client.get(f"workflows/{task_id}/")

    return server


if __name__ == "__main__":
    build_server().run()
