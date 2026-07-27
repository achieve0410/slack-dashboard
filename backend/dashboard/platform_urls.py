from django.urls import path

from . import platform_api


urlpatterns = [
    path("", platform_api.platform_root, name="platform-root"),
    path("agents/", platform_api.agents, name="platform-agents"),
    path("inbox/", platform_api.inbox_collection, name="platform-inbox"),
    path("tasks/", platform_api.task_collection, name="platform-tasks"),
    path("tasks/<uuid:task_id>/", platform_api.task_detail, name="platform-task-detail"),
    path(
        "tasks/<uuid:task_id>/context/",
        platform_api.task_context,
        name="platform-task-context",
    ),
    path("artifacts/", platform_api.artifact_collection, name="platform-artifacts"),
    path(
        "artifacts/<uuid:artifact_id>/",
        platform_api.artifact_detail,
        name="platform-artifact-detail",
    ),
    path("approvals/", platform_api.approval_collection, name="platform-approvals"),
    path(
        "approvals/<uuid:approval_id>/decision/",
        platform_api.approval_decision,
        name="platform-approval-decision",
    ),
    path("events/", platform_api.events, name="platform-events"),
    path(
        "workflows/<uuid:task_id>/",
        platform_api.workflow_detail,
        name="platform-workflow-detail",
    ),
    path("search/", platform_api.search, name="platform-search"),
]
