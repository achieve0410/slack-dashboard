from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import ValidationError
from django.db import transaction
from django.template.response import TemplateResponse

from .models import (
    Category,
    Citation,
    ContentRun,
    CronJob,
    FreeQuestionMessage,
    KnowledgeItem,
    PlatformAgent,
    PlatformApiToken,
    PlatformApproval,
    PlatformArtifact,
    PlatformEvent,
    PlatformInboxItem,
    PlatformTask,
    QuizGenerationBatch,
    QuizProgress,
    QuizQuestion,
    QuizSession,
    QuizSessionItem,
    ScheduleCategory,
    ScheduleEvent,
    UserResponse,
    UserRunState,
)
from .review import approve_knowledge_items


class CitationInline(admin.TabularInline):
    model = Citation
    extra = 0


class ManualApprovalForm(forms.Form):
    category = forms.ModelChoiceField(
        label="카테고리",
        queryset=Category.objects.none(),
    )
    review_note = forms.CharField(
        label="검토 사유",
        max_length=980,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Category.objects.filter(
            pk__in=Category.active_tree_ids()
        ).order_by("path")


def _lock_category_for_deactivation(category_id: int) -> Category:
    categories = list(Category.objects.select_for_update().order_by("pk"))
    by_id = {category.pk: category for category in categories}
    if category_id not in by_id:
        raise ValidationError("카테고리를 찾을 수 없습니다.")

    subtree_ids = {category_id}
    for category in categories:
        current = category
        seen = set()
        while current.parent_id is not None and current.pk not in seen:
            seen.add(current.pk)
            if current.parent_id == category_id:
                subtree_ids.add(category.pk)
                break
            current = by_id.get(current.parent_id)
            if current is None:
                break

    if any(
        category.pk != category_id
        and category.pk in subtree_ids
        and category.is_active
        for category in categories
    ):
        raise ValidationError("활성 하위 카테고리를 먼저 비활성화해야 합니다.")
    if KnowledgeItem.objects.filter(
        category_id__in=subtree_ids,
        status__in=(
            KnowledgeItem.Status.CLASSIFIED,
            KnowledgeItem.Status.PENDING,
        ),
    ).exists():
        raise ValidationError("분류된 항목이 있는 카테고리는 비활성화할 수 없습니다.")
    return by_id[category_id]


@admin.register(CronJob)
class CronJobAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "schedule", "enabled", "last_status", "last_run_at")
    list_filter = ("category", "enabled", "last_status")
    search_fields = ("name", "external_id")


@admin.register(ContentRun)
class ContentRunAdmin(admin.ModelAdmin):
    list_display = ("title", "job", "status", "generated_at", "hidden_at")
    list_filter = ("status", "job__category", "hidden_at")
    search_fields = ("title", "body", "job__name")
    inlines = [CitationInline]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("path", "depth", "created_by", "is_active", "updated_at")
    list_filter = ("depth", "created_by", "is_active")
    search_fields = ("name", "path")
    readonly_fields = ("created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj:
            readonly.extend(
                ("name", "path", "path_key", "identity_hash", "parent", "depth")
            )
        return readonly

    def save_model(self, request, obj, form, change):
        if change and not obj.is_active and "is_active" in form.changed_data:
            with transaction.atomic():
                _lock_category_for_deactivation(obj.pk)
                super().save_model(request, obj, form, change)
            return
        super().save_model(request, obj, form, change)


@admin.register(KnowledgeItem)
class KnowledgeItemAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "source_type",
        "status",
        "category",
        "generated_at",
        "hidden_at",
    )
    list_filter = ("source_type", "status", "category", "hidden_at")
    search_fields = ("source_key", "title", "summary", "question", "answer")
    actions = ("approve_classification",)
    readonly_fields = (
        "source_type",
        "source_key",
        "content_run",
        "category",
        "status",
        "title",
        "summary",
        "question",
        "answer",
        "source_hash",
        "generated_at",
        "classification_model",
        "classification_confidence",
        "classification_reason",
        "classified_at",
        "reviewed_by",
        "reviewed_at",
        "created_at",
        "updated_at",
    )

    @admin.action(description="선택 항목 분류 승인/재지정")
    def approve_classification(self, request, queryset):
        item_ids = list(queryset.values_list("pk", flat=True))
        if "apply" in request.POST:
            form = ManualApprovalForm(request.POST)
            if form.is_valid():
                try:
                    updated, skipped = approve_knowledge_items(
                        item_ids,
                        form.cleaned_data["category"].pk,
                        request.user,
                        form.cleaned_data["review_note"],
                    )
                except ValidationError as error:
                    self.message_user(request, str(error), level=messages.ERROR)
                    return None
                self.message_user(
                    request,
                    f"{updated}개 항목을 승인했고 {skipped}개 항목은 건너뛰었습니다.",
                    level=messages.SUCCESS,
                )
                return None
        else:
            form = ManualApprovalForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "지식 항목 분류 승인/재지정",
            "opts": self.model._meta,
            "items": queryset,
            "form": form,
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
        }
        return TemplateResponse(
            request,
            "admin/dashboard/knowledgeitem/approve_classification.html",
            context,
        )


@admin.register(QuizGenerationBatch)
class QuizGenerationBatchAdmin(admin.ModelAdmin):
    list_display = ("inventory_version", "status", "dry_run", "candidate_count", "published_count", "started_at")
    list_filter = ("status", "dry_run")
    search_fields = ("inventory_version", "generator_version", "model_name", "prompt_version")
    readonly_fields = ("created_at", "updated_at")


@admin.register(QuizQuestion)
class QuizQuestionAdmin(admin.ModelAdmin):
    list_display = (
        "prompt",
        "domain",
        "difficulty",
        "question_type",
        "publish_state",
        "is_active",
        "knowledge_item",
    )
    list_filter = ("domain", "difficulty", "question_type", "publish_state", "is_active")
    search_fields = ("prompt", "explanation", "knowledge_item__title", "source_hash")
    readonly_fields = ("active_identity_hash", "created_at", "updated_at")


@admin.register(QuizSession)
class QuizSessionAdmin(admin.ModelAdmin):
    list_display = ("session_id", "domain", "difficulty", "mode", "status", "started_at", "completed_at")
    list_filter = ("domain", "difficulty", "mode", "status")
    readonly_fields = ("session_id", "created_at", "updated_at")


@admin.register(QuizSessionItem)
class QuizSessionItemAdmin(admin.ModelAdmin):
    list_display = ("session", "position", "question", "answered_at", "correct")
    list_filter = ("correct",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(QuizProgress)
class QuizProgressAdmin(admin.ModelAdmin):
    list_display = ("question", "stage", "wrong_count", "correct_streak", "next_review_at", "mastered_at")
    list_filter = ("stage",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(FreeQuestionMessage)
class FreeQuestionMessageAdmin(admin.ModelAdmin):
    list_display = ("role", "generated_at", "knowledge_item", "content")
    list_filter = ("role",)
    search_fields = ("content",)


@admin.register(ScheduleEvent)
class ScheduleEventAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "item_type",
        "todo_category",
        "starts_at",
        "source_type",
        "completed",
    )
    list_filter = ("item_type", "todo_category", "source_type", "completed")
    search_fields = ("title", "notes")


@admin.register(ScheduleCategory)
class ScheduleCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "sort_order", "is_fallback", "updated_at")
    list_filter = ("is_fallback",)
    search_fields = ("name",)


admin.site.register(UserRunState)
admin.site.register(UserResponse)


@admin.register(PlatformAgent)
class PlatformAgentAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("key", "name")


@admin.register(PlatformApiToken)
class PlatformApiTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "agent", "token_prefix", "is_active", "expires_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("name", "agent__key", "token_prefix")
    readonly_fields = ("token_prefix", "token_hash", "created_at", "last_used_at")


@admin.register(PlatformInboxItem)
class PlatformInboxItemAdmin(admin.ModelAdmin):
    list_display = ("title", "source_type", "status", "collected_by", "collected_at")
    list_filter = ("source_type", "status")
    search_fields = ("title", "external_id", "content")


@admin.register(PlatformTask)
class PlatformTaskAdmin(admin.ModelAdmin):
    list_display = ("title", "status", "priority", "created_by", "created_at")
    list_filter = ("status", "priority")
    search_fields = ("title", "description")


@admin.register(PlatformArtifact)
class PlatformArtifactAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "version", "task", "created_by", "created_at")
    list_filter = ("kind", "mime_type")
    search_fields = ("title", "task__title", "content_sha256")
    readonly_fields = (
        "series_id",
        "version",
        "artifact_path",
        "content_sha256",
        "size_bytes",
        "created_at",
    )


@admin.register(PlatformApproval)
class PlatformApprovalAdmin(admin.ModelAdmin):
    list_display = ("task", "status", "requested_by", "decided_by", "requested_at")
    list_filter = ("status",)
    search_fields = ("task__title", "target_sha256")


@admin.register(PlatformEvent)
class PlatformEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "entity_type", "entity_id", "actor", "created_at")
    list_filter = ("event_type", "entity_type")
    search_fields = ("event_type", "entity_id", "actor__key")
    readonly_fields = ("event_type", "entity_type", "entity_id", "task", "actor", "data", "created_at")
