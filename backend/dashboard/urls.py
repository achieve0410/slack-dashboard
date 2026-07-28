from django.urls import path

from . import token_management, views


urlpatterns = [
    path("health/", views.health, name="health"),
    path("csrf/", views.csrf, name="csrf"),
    path(
        "platform-tokens/",
        token_management.platform_tokens,
        name="platform-tokens",
    ),
    path(
        "platform-tokens/<int:token_id>/rotate/",
        token_management.rotate_platform_token,
        name="platform-token-rotate",
    ),
    path(
        "platform-tokens/<int:token_id>/revoke/",
        token_management.revoke_platform_token,
        name="platform-token-revoke",
    ),
    path("summary/", views.summary, name="summary"),
    path("onboarding/", views.onboarding, name="onboarding"),
    path("jobs/", views.jobs, name="jobs"),
    path("operations/", views.operations, name="operations"),
    path("quiz/catalog/", views.quiz_catalog, name="quiz-catalog"),
    path("quiz/sessions/", views.quiz_sessions, name="quiz-sessions"),
    path(
        "quiz/sessions/<uuid:session_id>/",
        views.quiz_session_detail,
        name="quiz-session-detail",
    ),
    path(
        "quiz/sessions/<uuid:session_id>/items/<int:item_id>/answer/",
        views.quiz_session_answer,
        name="quiz-session-answer",
    ),
    path(
        "quiz/sessions/<uuid:session_id>/result/",
        views.quiz_session_result,
        name="quiz-session-result",
    ),
    path("quiz/review/", views.quiz_review, name="quiz-review"),
    path(
        "quiz/questions/<int:question_id>/wrong-note/",
        views.quiz_wrong_note,
        name="quiz-wrong-note",
    ),
    path("categories/", views.categories, name="categories"),
    path("search/", views.search, name="search"),
    path("ask/", views.knowledge_ask, name="knowledge-ask"),
    path(
        "ask/<int:ask_id>/feedback/",
        views.knowledge_ask_feedback,
        name="knowledge-ask-feedback",
    ),
    path("knowledge/", views.knowledge, name="knowledge"),
    path(
        "saved-knowledge-views/",
        views.saved_knowledge_views,
        name="saved-knowledge-views",
    ),
    path(
        "saved-knowledge-views/<int:view_id>/",
        views.saved_knowledge_view_detail,
        name="saved-knowledge-view-detail",
    ),
    path(
        "saved-knowledge-views/<int:view_id>/apply/",
        views.saved_knowledge_view_apply,
        name="saved-knowledge-view-apply",
    ),
    path(
        "knowledge/<int:item_id>/navigation/",
        views.knowledge_navigation,
        name="knowledge-navigation",
    ),
    path(
        "knowledge/bulk/preview/",
        views.knowledge_bulk_preview,
        name="knowledge-bulk-preview",
    ),
    path(
        "knowledge/bulk/execute/",
        views.knowledge_bulk_execute,
        name="knowledge-bulk-execute",
    ),
    path(
        "knowledge/bulk/undo/",
        views.knowledge_bulk_undo,
        name="knowledge-bulk-undo",
    ),
    path("knowledge/trash/", views.knowledge_trash, name="knowledge-trash"),
    path(
        "knowledge/<int:item_id>/restore/",
        views.knowledge_restore,
        name="knowledge-restore",
    ),
    path("knowledge/<int:item_id>/", views.knowledge_detail, name="knowledge-detail"),
    path(
        "knowledge/<int:item_id>/tags/",
        views.knowledge_tags,
        name="knowledge-tags",
    ),
    path(
        "knowledge/<int:item_id>/state/",
        views.knowledge_state,
        name="knowledge-state",
    ),
    path(
        "knowledge/<int:item_id>/verification/",
        views.knowledge_verification,
        name="knowledge-verification",
    ),
    path(
        "knowledge/<int:item_id>/feedback/",
        views.knowledge_feedback,
        name="knowledge-feedback",
    ),
    path(
        "knowledge/<int:item_id>/classification/",
        views.knowledge_classification,
        name="knowledge-classification",
    ),
    path("free-question/", views.free_question, name="free-question"),
    path("schedule/categories/", views.schedule_categories, name="schedule-categories"),
    path(
        "schedule/categories/<int:category_id>/",
        views.schedule_category_detail,
        name="schedule-category-detail",
    ),
    path("schedule/", views.schedule, name="schedule"),
    path("schedule/<int:event_id>/", views.schedule_detail, name="schedule-detail"),
    path("runs/", views.runs, name="runs"),
    path("runs/<int:run_id>/", views.run_detail, name="run-detail"),
    path("runs/<int:run_id>/state/", views.run_state, name="run-state"),
    path("runs/<int:run_id>/responses/", views.run_responses, name="run-responses"),
]
