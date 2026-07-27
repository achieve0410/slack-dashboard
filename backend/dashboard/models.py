import hashlib
import hmac
import json
import secrets
import unicodedata
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone


class CronJob(models.Model):
    """A content source. In the open-source edition this is one row per
    configured Slack channel (see management command `sync_slack`); the name
    is kept from the original single-owner design where each row represented
    a scheduled agent job.
    """

    class Category(models.TextChoices):
        OTHER = "other", "기타"

    external_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=200)
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.OTHER,
        db_index=True,
    )
    prompt = models.TextField(blank=True)
    schedule = models.CharField(max_length=100, blank=True)
    timezone = models.CharField(max_length=50, default="Asia/Seoul")
    channel_id = models.CharField(max_length=50, blank=True)
    thread_ts = models.CharField(max_length=50, blank=True)
    enabled = models.BooleanField(default=True)
    state = models.CharField(max_length=30, blank=True)
    last_status = models.CharField(max_length=30, blank=True, db_index=True)
    last_error = models.TextField(blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self) -> str:
        return self.name


class Category(models.Model):
    class CreatedBy(models.TextChoices):
        SYSTEM = "system", "시스템"
        AI = "ai", "AI"
        USER = "user", "사용자"

    name = models.CharField(max_length=100)
    path = models.CharField(max_length=400)
    path_key = models.CharField(max_length=400, blank=True, editable=False)
    identity_hash = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
        editable=False,
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="children",
    )
    depth = models.PositiveSmallIntegerField()
    created_by = models.CharField(
        max_length=10,
        choices=CreatedBy.choices,
        default=CreatedBy.SYSTEM,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["path"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(depth__gte=1, depth__lte=3),
                name="category_depth_between_1_and_3",
            )
        ]

    @staticmethod
    def normalize_segment(value: str) -> str:
        return " ".join(unicodedata.normalize("NFC", value).strip().split())

    @classmethod
    def canonical_path_key(cls, value: str) -> str:
        return "/".join(cls.normalize_segment(segment).casefold() for segment in value.split("/"))

    @staticmethod
    def identity_digest(path_key: str) -> str:
        return hashlib.sha256(path_key.encode()).hexdigest()

    @classmethod
    def exact_categories(
        cls,
        path_keys: list[str] | tuple[str, ...],
        *,
        for_update: bool = False,
    ) -> dict[str, "Category"]:
        expected_by_digest = {cls.identity_digest(path_key): path_key for path_key in path_keys}
        if len(expected_by_digest) != len(set(path_keys)):
            raise cls.DoesNotExist("카테고리 식별 해시 충돌이 감지되었습니다.")
        queryset = cls.objects
        if for_update:
            queryset = queryset.select_for_update()
        result = {}
        for category in queryset.filter(identity_hash__in=expected_by_digest).order_by("pk"):
            expected_key = expected_by_digest[category.identity_hash]
            if category.path_key != expected_key:
                raise cls.DoesNotExist("카테고리 식별 해시 충돌이 감지되었습니다.")
            result[expected_key] = category
        return result

    @classmethod
    def exact_category(cls, path_key: str, *, for_update: bool = False) -> "Category":
        categories = cls.exact_categories([path_key], for_update=for_update)
        try:
            return categories[path_key]
        except KeyError as error:
            raise cls.DoesNotExist(path_key) from error

    @classmethod
    def active_tree_ids(cls) -> set[int]:
        rows = {
            row["id"]: (row["parent_id"], row["is_active"])
            for row in cls.objects.values("id", "parent_id", "is_active")
        }
        resolved: dict[int, bool] = {}

        def is_active(category_id: int, visiting: set[int]) -> bool:
            if category_id in resolved:
                return resolved[category_id]
            if category_id in visiting or category_id not in rows:
                return False
            parent_id, own_active = rows[category_id]
            active = bool(own_active) and (
                parent_id is None or is_active(parent_id, {*visiting, category_id})
            )
            resolved[category_id] = active
            return active

        return {category_id for category_id in rows if is_active(category_id, set())}

    @classmethod
    def lock_active_chain(cls, category_id: int) -> tuple["Category", ...]:
        parent_by_id = dict(cls.objects.values_list("pk", "parent_id"))
        chain_ids = []
        current_id = category_id
        while current_id is not None:
            if current_id in chain_ids or current_id not in parent_by_id:
                raise cls.DoesNotExist(category_id)
            chain_ids.append(current_id)
            current_id = parent_by_id[current_id]

        locked_by_id = {
            category.pk: category
            for category in cls.objects.select_for_update()
            .filter(pk__in=chain_ids)
            .order_by("pk")
        }
        if len(locked_by_id) != len(chain_ids):
            raise cls.DoesNotExist(category_id)

        chain = []
        current_id = category_id
        seen = set()
        while current_id is not None:
            if current_id in seen or current_id not in locked_by_id:
                raise cls.DoesNotExist(category_id)
            seen.add(current_id)
            category = locked_by_id[current_id]
            chain.append(category)
            current_id = category.parent_id
        if seen != set(chain_ids) or any(not category.is_active for category in chain):
            raise cls.DoesNotExist(category_id)
        return tuple(reversed(chain))

    def save(self, *args, **kwargs):
        self.path_key = self.canonical_path_key(self.path)
        self.identity_hash = self.identity_digest(self.path_key)
        update_fields = kwargs.get("update_fields")
        if update_fields and {"name", "path", "path_key", "parent", "parent_id", "depth"} & set(
            update_fields
        ):
            kwargs["update_fields"] = {*update_fields, "path_key", "identity_hash"}
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors: dict[str, str] = {}
        normalized_name = self.normalize_segment(self.name)
        if not normalized_name:
            errors["name"] = "카테고리 이름은 비워둘 수 없습니다."
        elif normalized_name != self.name:
            errors["name"] = "카테고리 이름은 NFC 및 단일 공백 형식이어야 합니다."
        elif "/" in self.name or any(
            unicodedata.category(character) == "Cc" for character in self.name
        ):
            errors["name"] = "카테고리 이름에는 '/' 또는 제어 문자를 사용할 수 없습니다."

        expected_depth = 1
        expected_path = normalized_name
        if self.parent_id:
            if self.pk and self.parent_id == self.pk:
                errors["parent"] = "카테고리는 자기 자신을 상위 카테고리로 가질 수 없습니다."
            expected_depth = self.parent.depth + 1
            expected_path = f"{self.parent.path}/{normalized_name}"
            ancestor = self.parent
            visited: set[int] = set()
            while ancestor:
                if ancestor.pk is not None:
                    if ancestor.pk == self.pk or ancestor.pk in visited:
                        errors["parent"] = "카테고리 계층에 순환이 발생할 수 없습니다."
                        break
                    visited.add(ancestor.pk)
                ancestor = ancestor.parent

        if expected_depth < 1 or expected_depth > 3 or self.depth != expected_depth:
            errors["depth"] = "카테고리 깊이는 상위 경로에 맞는 1~3이어야 합니다."
        if self.path != expected_path:
            errors["path"] = "카테고리 경로가 상위 경로와 이름에 일치하지 않습니다."
        elif len(expected_path) > self._meta.get_field("path").max_length:
            errors["path"] = "카테고리 경로가 최대 길이를 초과했습니다."
        expected_path_key = self.canonical_path_key(expected_path)
        if len(expected_path_key) > self._meta.get_field("path_key").max_length:
            errors["path_key"] = "정규화 경로 키가 최대 길이를 초과했습니다."
        elif self.path_key and self.path_key != expected_path_key:
            errors["path_key"] = "정규화 경로 키가 카테고리 경로와 일치하지 않습니다."
        else:
            self.path_key = expected_path_key
        expected_identity_hash = self.identity_digest(expected_path_key)
        if self.identity_hash and self.identity_hash != expected_identity_hash:
            errors["identity_hash"] = "카테고리 식별 해시가 정규화 경로 키와 일치하지 않습니다."
        else:
            self.identity_hash = expected_identity_hash
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.path


class ContentRun(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "성공"
        FAILED = "failed", "실패"

    job = models.ForeignKey(CronJob, on_delete=models.CASCADE, related_name="runs")
    external_ts = models.CharField(max_length=50, unique=True, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, db_index=True)
    title = models.CharField(max_length=250)
    body = models.TextField(blank=True)
    raw_text = models.TextField(blank=True)
    error = models.TextField(blank=True)
    structured_data = models.JSONField(default=dict, blank=True)
    model_name = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=50, blank=True)
    generated_at = models.DateTimeField(db_index=True)
    hidden_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-generated_at"]
        indexes = [models.Index(fields=["job", "-generated_at"])]

    def __str__(self) -> str:
        return f"{self.job.name} · {self.generated_at:%Y-%m-%d %H:%M}"


class KnowledgeItem(models.Model):
    class SourceType(models.TextChoices):
        CRON = "cron", "Cron"
        SLACK_QA = "slack_qa", "Slack Q&A"

    class Status(models.TextChoices):
        AWAITING_ANSWER = "awaiting_answer", "답변 대기"
        PENDING = "pending", "분류 대기"
        CLASSIFIED = "classified", "분류 완료"
        NEEDS_REVIEW = "needs_review", "검토 필요"

    source_type = models.CharField(max_length=10, choices=SourceType.choices)
    source_key = models.CharField(max_length=180, unique=True)
    content_run = models.OneToOneField(
        ContentRun,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="knowledge_item",
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="items",
    )
    status = models.CharField(max_length=20, choices=Status.choices)
    title = models.CharField(max_length=250)
    summary = models.TextField(blank=True, validators=[MaxLengthValidator(600)])
    question = models.TextField(blank=True)
    answer = models.TextField(blank=True)
    source_hash = models.CharField(max_length=64)
    generated_at = models.DateTimeField(db_index=True)
    classification_model = models.CharField(max_length=100, blank=True)
    classification_confidence = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    classification_reason = models.TextField(
        blank=True,
        validators=[MaxLengthValidator(1000)],
    )
    classified_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_knowledge_items",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    classification_stale_at = models.DateTimeField(null=True, blank=True)
    slack_channel_id = models.CharField(max_length=50, blank=True, db_index=True)
    slack_thread_ts = models.CharField(max_length=50, blank=True, db_index=True)
    slack_source_url = models.URLField(max_length=700, blank=True)
    hidden_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-generated_at"]
        indexes = [
            models.Index(fields=["status", "-generated_at"], name="kn_item_status_gen_idx"),
            models.Index(fields=["category", "-generated_at"], name="kn_item_cat_gen_idx"),
            models.Index(fields=["source_type"], name="kn_item_source_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source_type="cron",
                        content_run__isnull=False,
                        question="",
                        answer="",
                    )
                    | (
                        models.Q(source_type="slack_qa", content_run__isnull=True)
                        & ~models.Q(question="")
                    )
                ),
                name="knowledge_source_fields_match",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source_type="cron")
                    | models.Q(
                        source_type="slack_qa",
                        status="awaiting_answer",
                        answer="",
                    )
                    | (
                        models.Q(source_type="slack_qa")
                        & ~models.Q(status="awaiting_answer")
                        & ~models.Q(answer="")
                    )
                ),
                name="knowledge_slack_answer_matches_status",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="classified",
                        category__isnull=False,
                        classified_at__isnull=False,
                    )
                    | (
                        ~models.Q(status="classified")
                        & models.Q(category__isnull=True, classified_at__isnull=True)
                    )
                ),
                name="knowledge_status_fields_match",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(classification_confidence__isnull=True)
                    | models.Q(
                        classification_confidence__gte=0,
                        classification_confidence__lte=1,
                    )
                ),
                name="knowledge_confidence_between_0_and_1",
            ),
        ]

    def clean(self):
        super().clean()
        errors: dict[str, str] = {}
        if self.source_type == self.SourceType.CRON:
            if not self.content_run_id:
                errors["content_run"] = "Cron 지식 항목에는 실행 결과가 필요합니다."
            else:
                if self.content_run.status != ContentRun.Status.SUCCESS:
                    errors["content_run"] = "성공한 Cron 실행 결과만 지식 항목이 될 수 있습니다."
                if self.source_key != f"cron:{self.content_run_id}":
                    errors["source_key"] = "Cron 소스 키는 ContentRun PK를 사용해야 합니다."
            if self.question or self.answer:
                errors["question"] = "Cron 지식 항목은 질문과 답변을 복제하지 않습니다."
        elif self.source_type == self.SourceType.SLACK_QA:
            if self.content_run_id:
                errors["content_run"] = "Slack Q&A는 Cron 실행 결과를 참조할 수 없습니다."
            if not self.question:
                errors["question"] = "Slack Q&A에는 질문이 필요합니다."
            if len(self.source_key.split(":", 2)) != 3 or not self.source_key.startswith(
                "slack:"
            ):
                errors["source_key"] = "Slack 소스 키에는 thread와 질문 timestamp가 필요합니다."
            if self.status == self.Status.AWAITING_ANSWER:
                if self.answer:
                    errors["answer"] = "답변 대기 항목에는 답변이 없어야 합니다."
            elif not self.answer:
                errors["answer"] = "답변 대기 이외의 Slack Q&A에는 답변이 필요합니다."

        if self.status == self.Status.CLASSIFIED:
            if not self.category_id:
                errors["category"] = "분류 완료 항목에는 카테고리가 필요합니다."
            if not self.classified_at:
                errors["classified_at"] = "분류 완료 시각이 필요합니다."
        elif self.category_id or self.classified_at:
            errors["status"] = "분류 전 항목에는 카테고리나 분류 완료 시각을 지정할 수 없습니다."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.title


class KnowledgeConsumptionState(models.Model):
    knowledge_item = models.OneToOneField(
        KnowledgeItem,
        on_delete=models.CASCADE,
        related_name="consumption_state",
    )
    read_at = models.DateTimeField(null=True, blank=True)
    bookmarked_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, max_length=5000)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class KnowledgeTag(models.Model):
    label = models.CharField(max_length=120)
    normalized_label = models.CharField(max_length=120, editable=False)
    identity_hash = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["normalized_label", "id"]

    @staticmethod
    def display_label(value: str) -> str:
        return " ".join(unicodedata.normalize("NFC", value).strip().split())

    @classmethod
    def normalize_label(cls, value: str) -> str:
        return cls.display_label(value).casefold()

    @staticmethod
    def identity_digest(normalized_label: str) -> str:
        return hashlib.sha256(normalized_label.encode()).hexdigest()

    @classmethod
    def for_label(cls, label: str) -> "KnowledgeTag":
        display_label = cls.display_label(label)
        normalized_label = cls.normalize_label(label)
        if not normalized_label:
            raise ValidationError({"label": "태그 라벨은 비워둘 수 없습니다."})
        identity_hash = cls.identity_digest(normalized_label)
        tag, _ = cls.objects.get_or_create(
            identity_hash=identity_hash,
            defaults={
                "label": display_label,
                "normalized_label": normalized_label,
            },
        )
        if tag.normalized_label != normalized_label:
            raise ValidationError({"label": "태그 라벨 식별 해시 충돌이 감지되었습니다."})
        return tag

    def clean(self):
        super().clean()
        display_label = self.display_label(self.label)
        normalized_label = self.normalize_label(self.label)
        if not normalized_label:
            raise ValidationError({"label": "태그 라벨은 비워둘 수 없습니다."})
        if any(unicodedata.category(character) == "Cc" for character in display_label):
            raise ValidationError({"label": "태그 라벨에는 제어 문자를 사용할 수 없습니다."})
        self.label = display_label
        self.normalized_label = normalized_label
        self.identity_hash = self.identity_digest(normalized_label)

    def save(self, *args, **kwargs):
        self.label = self.display_label(self.label)
        self.normalized_label = self.normalize_label(self.label)
        self.identity_hash = self.identity_digest(self.normalized_label)
        update_fields = kwargs.get("update_fields")
        if update_fields and "label" in update_fields:
            kwargs["update_fields"] = {
                *update_fields,
                "normalized_label",
                "identity_hash",
            }
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.label


class KnowledgeTagSnapshot(models.Model):
    class Status(models.TextChoices):
        STAGING = "staging", "Staging"
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    snapshot_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    operation_run = models.ForeignKey(
        "OperationRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tag_snapshots",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.STAGING,
        db_index=True,
    )
    inventory_digest = models.CharField(max_length=64, blank=True)
    artifact_manifest = models.JSONField(default=dict, blank=True)
    item_count = models.PositiveIntegerField(default=0)
    tag_count = models.PositiveIntegerField(default=0)
    assignment_count = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="kn_tag_snap_status_idx"),
        ]

    def clean(self):
        super().clean()
        errors: dict[str, str] = {}
        if self.inventory_digest and not _is_sha256_digest(self.inventory_digest):
            errors["inventory_digest"] = "Inventory digest must be a SHA-256 hex digest."
        if not isinstance(self.artifact_manifest, dict):
            errors["artifact_manifest"] = "Artifact manifest must be a JSON object."
        if self.published_at and self.status == self.Status.STAGING:
            errors["published_at"] = "Staging snapshots cannot be marked published."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.snapshot_key} ({self.status})"


class KnowledgeTagAssignment(models.Model):
    class Source(models.TextChoices):
        AI = "ai", "AI"
        USER = "user", "User"

    snapshot = models.ForeignKey(
        KnowledgeTagSnapshot,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    knowledge_item = models.ForeignKey(
        KnowledgeItem,
        on_delete=models.CASCADE,
        related_name="tag_assignments",
    )
    tag = models.ForeignKey(
        KnowledgeTag,
        on_delete=models.PROTECT,
        related_name="assignments",
    )
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.AI)
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["snapshot_id", "knowledge_item_id", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "knowledge_item", "tag"],
                name="knowledge_tag_assignment_unique_tag",
            ),
        ]
        indexes = [
            models.Index(
                fields=["snapshot", "knowledge_item", "position"],
                name="kn_tag_assign_item_idx",
            ),
            models.Index(fields=["snapshot", "tag"], name="kn_tag_assign_tag_idx"),
        ]


class KnowledgeTagActiveSnapshot(models.Model):
    singleton_key = models.PositiveSmallIntegerField(
        default=1,
        unique=True,
        editable=False,
    )
    snapshot = models.OneToOneField(
        KnowledgeTagSnapshot,
        on_delete=models.PROTECT,
        related_name="active_pointer",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Knowledge tag active snapshot"
        verbose_name_plural = "Knowledge tag active snapshot"

    def clean(self):
        super().clean()
        if self.singleton_key != 1:
            raise ValidationError({"singleton_key": "활성 태그 스냅샷 포인터는 하나만 허용됩니다."})

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        super().save(*args, **kwargs)


class KnowledgeTagMutationLock(models.Model):
    singleton_key = models.PositiveSmallIntegerField(
        default=1,
        unique=True,
        editable=False,
    )
    name = models.CharField(max_length=40, default="knowledge_tag_mutation")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Knowledge tag mutation lock"
        verbose_name_plural = "Knowledge tag mutation lock"

    @classmethod
    def lock(cls) -> "KnowledgeTagMutationLock":
        lock, _ = cls.objects.get_or_create(singleton_key=1)
        return cls.objects.select_for_update().get(pk=lock.pk)

    def clean(self):
        super().clean()
        if self.singleton_key != 1:
            raise ValidationError({"singleton_key": "태그 변경 잠금 행은 하나만 허용됩니다."})

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        super().save(*args, **kwargs)


class KnowledgeTagCorpusRevision(models.Model):
    singleton_key = models.PositiveSmallIntegerField(
        default=1,
        unique=True,
        editable=False,
    )
    revision = models.PositiveBigIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Knowledge tag corpus revision"
        verbose_name_plural = "Knowledge tag corpus revision"

    @classmethod
    def get_current(cls) -> "KnowledgeTagCorpusRevision":
        row, _ = cls.objects.get_or_create(singleton_key=1)
        return row

    @classmethod
    def fence(cls) -> "KnowledgeTagCorpusRevision":
        row = cls.get_current()
        cls.objects.filter(pk=row.pk).update(revision=models.F("revision"))
        return cls.objects.get(pk=row.pk)

    def clean(self):
        super().clean()
        if self.singleton_key != 1:
            raise ValidationError({"singleton_key": "태깅 코퍼스 revision 행은 하나만 허용됩니다."})

    def save(self, *args, **kwargs):
        self.singleton_key = 1
        super().save(*args, **kwargs)


class SavedKnowledgeView(models.Model):
    name = models.CharField(max_length=100)
    normalized_name = models.CharField(max_length=200, editable=False)
    identity_hash = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
    )
    canonical_filters = models.JSONField(default=dict, blank=True)
    sort = models.CharField(max_length=10, default="newest")
    default_slot = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        unique=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["normalized_name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(default_slot__isnull=True)
                | models.Q(default_slot=1),
                name="saved_view_default_slot_is_one",
            )
        ]

    @staticmethod
    def normalize_name(value: str) -> str:
        return " ".join(unicodedata.normalize("NFC", value).strip().split()).casefold()

    @staticmethod
    def identity_digest(normalized_name: str) -> str:
        return hashlib.sha256(normalized_name.encode()).hexdigest()

    def clean_fields(self, exclude=None):
        if isinstance(self.name, str):
            self.normalized_name = self.normalize_name(self.name)
            self.identity_hash = self.identity_digest(self.normalized_name)
        super().clean_fields(exclude=exclude)

    def clean(self):
        super().clean()
        self.name = " ".join(unicodedata.normalize("NFC", self.name).strip().split())
        if not self.name:
            raise ValidationError({"name": "보기 이름을 입력해주세요."})
        self.normalized_name = self.normalize_name(self.name)
        self.identity_hash = self.identity_digest(self.normalized_name)

    def save(self, *args, **kwargs):
        self.normalized_name = self.normalize_name(self.name)
        self.identity_hash = self.identity_digest(self.normalized_name)
        update_fields = kwargs.get("update_fields")
        if update_fields and "name" in update_fields:
            kwargs["update_fields"] = {
                *update_fields,
                "normalized_name",
                "identity_hash",
            }
        super().save(*args, **kwargs)


class BulkSelectionSnapshot(models.Model):
    class ActionType(models.TextChoices):
        READ = "read", "읽음"
        BOOKMARKED = "bookmarked", "저장"
        COMPLETED = "completed", "완료"
        ARCHIVED = "archived", "보관"
        CATEGORY = "category", "카테고리"
        HIDE = "hide", "숨김"

    token_hash = models.CharField(max_length=64, unique=True)
    target_ids = models.JSONField()
    target_digest = models.CharField(max_length=64)
    target_count = models.PositiveIntegerField()
    action_type = models.CharField(max_length=12, choices=ActionType.choices)
    action_parameters = models.JSONField(default=dict, blank=True)
    canonical_filter = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    affected_ids = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def clean(self):
        super().clean()
        from .knowledge_actions import (
            KnowledgeActionError,
            canonical_action_parameters,
            normalize_target_ids,
            target_digest,
        )

        errors = {}
        try:
            normalized_ids = normalize_target_ids(self.target_ids)
            if normalized_ids != self.target_ids:
                errors["target_ids"] = "대상 IDs는 정규화된 정렬 순서여야 합니다."
            if self.target_count != len(normalized_ids):
                errors["target_count"] = "대상 개수가 target IDs와 일치하지 않습니다."
            if self.target_digest != target_digest(normalized_ids):
                errors["target_digest"] = "대상 digest가 target IDs와 일치하지 않습니다."
        except KnowledgeActionError as error:
            errors["target_ids"] = error.message
            normalized_ids = []
        try:
            if canonical_action_parameters(
                self.action_type,
                self.action_parameters,
            ) != self.action_parameters:
                errors["action_parameters"] = "action parameters가 canonical하지 않습니다."
        except KnowledgeActionError as error:
            errors["action_parameters"] = error.message
        if not isinstance(self.canonical_filter, dict):
            errors["canonical_filter"] = "canonical filter는 객체여야 합니다."
        try:
            affected_ids = normalize_target_ids(self.affected_ids, allow_empty=True)
            if affected_ids != self.affected_ids or not set(affected_ids).issubset(
                normalized_ids
            ):
                errors["affected_ids"] = "affected IDs는 canonical target IDs의 부분집합이어야 합니다."
        except KnowledgeActionError as error:
            errors["affected_ids"] = error.message
        if errors:
            raise ValidationError(errors)


class OperationRun(models.Model):
    class Kind(models.TextChoices):
        SYNC = "sync", "동기화"
        CLASSIFY = "classify", "분류"
        QUIZ = "quiz", "퀴즈"
        TAGGING = "tagging", "태깅"

    class Status(models.TextChoices):
        RUNNING = "running", "실행 중"
        SUCCESS = "success", "성공"
        FAILED = "failed", "실패"
        SKIPPED = "skipped", "건너뜀"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices)
    error_code = models.CharField(max_length=40, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def clean(self):
        super().clean()
        from .operation_runs import validate_operation_details

        validate_operation_details(
            self.kind,
            self.status,
            self.error_code,
            self.summary,
        )


class QuizGenerationBatch(models.Model):
    class Status(models.TextChoices):
        DRY_RUN = "dry_run", "Dry run"
        WRITING = "writing", "Writing"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    inventory_version = models.CharField(max_length=80)
    allowlist_snapshot = models.JSONField(default=dict, blank=True)
    dry_run = models.BooleanField(default=True)
    status = models.CharField(max_length=12, choices=Status.choices)
    candidate_count = models.PositiveIntegerField(default=0)
    published_count = models.PositiveIntegerField(default=0)
    quarantined_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    candidate_outcomes = models.JSONField(default=list, blank=True)
    generator_version = models.CharField(max_length=80)
    model_name = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=80, blank=True)
    prompt_digest = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]

    def clean(self):
        super().clean()
        errors: dict[str, str] = {}
        if not isinstance(self.allowlist_snapshot, dict):
            errors["allowlist_snapshot"] = "Allowlist snapshot must be a JSON object."
        if not isinstance(self.candidate_outcomes, list) or any(
            not isinstance(outcome, dict) for outcome in self.candidate_outcomes
        ):
            errors["candidate_outcomes"] = "Candidate outcomes must be a list of objects."
        if self.prompt_digest and not _is_sha256_digest(self.prompt_digest):
            errors["prompt_digest"] = "Prompt digest must be a SHA-256 hex digest."
        if self.finished_at and self.finished_at < self.started_at:
            errors["finished_at"] = "Finished time cannot be before start time."
        if errors:
            raise ValidationError(errors)


def _is_sha256_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


class QuizQuestion(models.Model):
    class Domain(models.TextChoices):
        ENGLISH = "english", "English"
        JAPANESE = "japanese", "Japanese"
        AWS_SAA = "aws_saa", "AWS SAA"

    class Difficulty(models.TextChoices):
        BEGINNER = "beginner", "Beginner"
        INTERMEDIATE = "intermediate", "Intermediate"
        ADVANCED = "advanced", "Advanced"

    class QuestionType(models.TextChoices):
        SINGLE_CHOICE = "single_choice", "Single choice"
        MULTIPLE_SELECT = "multiple_select", "Multiple select"

    class PublishState(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        INACTIVE = "inactive", "Inactive"
        SUPERSEDED = "superseded", "Superseded"

    batch = models.ForeignKey(
        QuizGenerationBatch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="questions",
    )
    knowledge_item = models.ForeignKey(
        KnowledgeItem,
        on_delete=models.PROTECT,
        related_name="quiz_questions",
    )
    domain = models.CharField(max_length=20, choices=Domain.choices, db_index=True)
    difficulty = models.CharField(
        max_length=20,
        choices=Difficulty.choices,
        db_index=True,
    )
    question_type = models.CharField(
        max_length=20,
        choices=QuestionType.choices,
        db_index=True,
    )
    prompt = models.TextField()
    choices = models.JSONField()
    correct_choice_ids = models.JSONField()
    question_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        db_index=True,
    )
    explanation = models.TextField()
    evidence_excerpt = models.TextField(blank=True)
    evidence_digest = models.CharField(max_length=64, blank=True)
    source_hash = models.CharField(max_length=64)
    generator_version = models.CharField(max_length=80)
    model_name = models.CharField(max_length=100, blank=True)
    prompt_version = models.CharField(max_length=80, blank=True)
    prompt_digest = models.CharField(max_length=64, blank=True)
    publish_state = models.CharField(
        max_length=12,
        choices=PublishState.choices,
        default=PublishState.DRAFT,
        db_index=True,
    )
    is_active = models.BooleanField(default=False, db_index=True)
    active_identity_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="superseded_versions",
    )
    generated_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    IMMUTABLE_PUBLISHED_FIELDS = (
        "knowledge_item_id",
        "domain",
        "difficulty",
        "question_type",
        "prompt",
        "choices",
        "correct_choice_ids",
        "question_fingerprint",
        "explanation",
        "evidence_excerpt",
        "evidence_digest",
        "source_hash",
        "generator_version",
        "model_name",
        "prompt_version",
        "prompt_digest",
    )

    class Meta:
        ordering = ["domain", "difficulty", "knowledge_item_id", "-generated_at"]
        indexes = [
            models.Index(
                fields=["domain", "difficulty", "question_type", "publish_state", "is_active"],
                name="quiz_q_bank_lookup_idx",
            ),
            models.Index(fields=["knowledge_item", "source_hash"], name="quiz_q_source_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(publish_state__in=["draft", "published", "inactive", "superseded"]),
                name="quiz_question_publish_state_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(is_active=False)
                | models.Q(publish_state="published", superseded_by__isnull=True),
                name="quiz_question_active_is_published",
            ),
        ]

    def clean(self):
        super().clean()
        errors: dict[str, str] = {}
        choice_ids = self._clean_choice_ids(errors)
        correct_ids = self._clean_correct_choice_ids(choice_ids, errors)
        if (
            self.question_type == self.QuestionType.SINGLE_CHOICE
            and len(correct_ids) != 1
        ):
            errors["correct_choice_ids"] = "Single-choice questions require exactly one correct choice."
        if (
            self.question_type == self.QuestionType.MULTIPLE_SELECT
            and len(correct_ids) < 2
        ):
            errors["correct_choice_ids"] = "Multiple-select questions require at least two correct choices."
        for field in ("source_hash", "evidence_digest", "prompt_digest"):
            value = getattr(self, field)
            if value and not _is_sha256_digest(value):
                errors[field] = "Value must be a SHA-256 hex digest."
        if self.publish_state == self.PublishState.PUBLISHED and not self.published_at:
            errors["published_at"] = "Published questions require published_at."
        if self.publish_state != self.PublishState.PUBLISHED and self.is_active:
            errors["is_active"] = "Only published questions can be active."
        if self.superseded_by_id and self.superseded_by_id == self.pk:
            errors["superseded_by"] = "A question cannot supersede itself."
        if errors:
            raise ValidationError(errors)
        self.question_fingerprint = self._question_fingerprint()
        self.active_identity_hash = self._active_identity_hash() if self.is_active else None

    def _clean_choice_ids(self, errors: dict[str, str]) -> list[str]:
        if not isinstance(self.choices, list) or len(self.choices) < 2:
            errors["choices"] = "Choices must be a list with at least two objects."
            return []
        choice_ids = []
        for choice in self.choices:
            if (
                not isinstance(choice, dict)
                or not isinstance(choice.get("id"), str)
                or not choice["id"]
                or not isinstance(choice.get("text"), str)
                or not choice["text"]
            ):
                errors["choices"] = "Each choice must contain non-empty string id and text."
                return []
            choice_ids.append(choice["id"])
        if len(choice_ids) != len(set(choice_ids)):
            errors["choices"] = "Choice IDs must be unique."
        return choice_ids

    def _clean_correct_choice_ids(
        self,
        choice_ids: list[str],
        errors: dict[str, str],
    ) -> list[str]:
        if not isinstance(self.correct_choice_ids, list) or any(
            not isinstance(choice_id, str) or not choice_id
            for choice_id in self.correct_choice_ids
        ):
            errors["correct_choice_ids"] = "Correct choice IDs must be a list of strings."
            return []
        if len(self.correct_choice_ids) != len(set(self.correct_choice_ids)):
            errors["correct_choice_ids"] = "Correct choice IDs must be unique."
            return []
        unknown_ids = sorted(set(self.correct_choice_ids) - set(choice_ids))
        if unknown_ids:
            errors["correct_choice_ids"] = "Correct choice IDs must exist in choices."
        return list(self.correct_choice_ids)

    def _active_identity_hash(self) -> str:
        parts = (
            str(self.knowledge_item_id),
            self.domain,
            self.difficulty,
            self.question_type,
            self.source_hash,
            self.question_fingerprint,
        )
        return hashlib.sha256("\0".join(parts).encode()).hexdigest()

    def _question_fingerprint(self) -> str:
        choices = sorted(
            (
                {
                    "id": choice["id"],
                    "text": " ".join(choice["text"].split()),
                }
                for choice in self.choices
            ),
            key=lambda choice: choice["id"],
        )
        content = {
            "prompt": " ".join(self.prompt.split()),
            "choices": choices,
            "correct_choice_ids": sorted(self.correct_choice_ids),
        }
        payload = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def _assert_published_immutable(self):
        if not self.pk:
            return
        previous = type(self).objects.filter(pk=self.pk).first()
        if not previous or previous.publish_state != self.PublishState.PUBLISHED:
            return
        changed = [
            field
            for field in self.IMMUTABLE_PUBLISHED_FIELDS
            if getattr(previous, field) != getattr(self, field)
        ]
        if changed:
            raise ValidationError(
                {"publish_state": "Published quiz content is immutable."}
            )

    def save(self, *args, **kwargs):
        self._assert_published_immutable()
        self.full_clean()
        super().save(*args, **kwargs)


class QuizSession(models.Model):
    class Domain(models.TextChoices):
        ENGLISH = "english", "English"
        JAPANESE = "japanese", "Japanese"
        AWS_SAA = "aws_saa", "AWS SAA"

    class Difficulty(models.TextChoices):
        BEGINNER = "beginner", "Beginner"
        INTERMEDIATE = "intermediate", "Intermediate"
        ADVANCED = "advanced", "Advanced"

    class Mode(models.TextChoices):
        NEW = "new", "New"
        REVIEW = "review", "Review"
        WRONG = "wrong", "Wrong"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        COMPLETED = "completed", "Completed"
        ABANDONED = "abandoned", "Abandoned"

    session_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    domain = models.CharField(max_length=20, choices=Domain.choices, db_index=True)
    difficulty = models.CharField(max_length=20, choices=Difficulty.choices, db_index=True)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.NEW)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    required_count = models.PositiveSmallIntegerField(default=10)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        indexes = [
            models.Index(fields=["domain", "difficulty", "status"], name="quiz_session_pool_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(required_count__gte=1, required_count__lte=10),
                name="quiz_session_required_count_1_10",
            ),
            models.CheckConstraint(
                condition=models.Q(status="completed", completed_at__isnull=False)
                | (~models.Q(status="completed") & models.Q(completed_at__isnull=True)),
                name="quiz_session_completed_at_matches_status",
            ),
        ]


class QuizSessionItem(models.Model):
    session = models.ForeignKey(
        QuizSession,
        on_delete=models.CASCADE,
        related_name="items",
    )
    question = models.ForeignKey(
        QuizQuestion,
        on_delete=models.PROTECT,
        related_name="session_items",
    )
    position = models.PositiveSmallIntegerField()
    accepted_choice_ids = models.JSONField(default=list, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    correct = models.BooleanField(null=True, blank=True)
    feedback_snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["session_id", "position"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "position"],
                name="quiz_session_item_unique_position",
            ),
            models.UniqueConstraint(
                fields=["session", "question"],
                name="quiz_session_item_unique_question",
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1, position__lte=10),
                name="quiz_session_item_position_1_10",
            ),
        ]

    def clean(self):
        super().clean()
        errors: dict[str, str] = {}
        if not isinstance(self.accepted_choice_ids, list) or any(
            not isinstance(choice_id, str) for choice_id in self.accepted_choice_ids
        ):
            errors["accepted_choice_ids"] = "Accepted choice IDs must be a list of strings."
        if len(self.accepted_choice_ids) != len(set(self.accepted_choice_ids)):
            errors["accepted_choice_ids"] = "Accepted choice IDs must be unique."
        if self.answered_at and self.correct is None:
            errors["correct"] = "Answered items require correctness."
        if not self.answered_at and self.correct is not None:
            errors["answered_at"] = "Correctness requires answered_at."
        if not isinstance(self.feedback_snapshot, dict):
            errors["feedback_snapshot"] = "Feedback snapshot must be a JSON object."
        if errors:
            raise ValidationError(errors)


class QuizProgress(models.Model):
    class Stage(models.TextChoices):
        RESET = "0", "Reset"
        ONE_DAY = "1d", "1 day"
        THREE_DAYS = "3d", "3 days"
        SEVEN_DAYS = "7d", "7 days"
        FOURTEEN_DAYS = "14d", "14 days"
        THIRTY_DAYS = "30d", "30 days"

    question = models.OneToOneField(
        QuizQuestion,
        on_delete=models.CASCADE,
        related_name="progress",
    )
    stage = models.CharField(max_length=4, choices=Stage.choices, default=Stage.RESET)
    wrong_count = models.PositiveIntegerField(default=0)
    correct_streak = models.PositiveIntegerField(default=0)
    next_review_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_answered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    mastered_at = models.DateTimeField(null=True, blank=True, db_index=True)
    manual_wrong_note_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_review_at", "question_id"]


class ScheduleCategory(models.Model):
    name = models.CharField(max_length=50, unique=True)
    keywords = models.JSONField(default=list, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_fallback = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def clean(self):
        super().clean()
        errors = {}
        self.name = self.name.strip()
        if not self.name:
            errors["name"] = "카테고리 이름을 입력해주세요."
        if not isinstance(self.keywords, list) or any(
            not isinstance(keyword, str) for keyword in self.keywords
        ):
            errors["keywords"] = "키워드는 문자열 목록이어야 합니다."
        else:
            normalized = []
            for keyword in self.keywords:
                value = keyword.strip().casefold()
                if value and value not in normalized:
                    normalized.append(value[:50])
            self.keywords = normalized
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.name


class ScheduleEvent(models.Model):
    class ItemType(models.TextChoices):
        SCHEDULE = "schedule", "일정"
        TODO = "todo", "할 일"

    class SourceType(models.TextChoices):
        MANUAL = "manual", "웹"
        SLACK = "slack", "Slack"

    title = models.CharField(max_length=200)
    item_type = models.CharField(
        max_length=10,
        choices=ItemType.choices,
        default=ItemType.SCHEDULE,
    )
    todo_category = models.ForeignKey(
        ScheduleCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="events",
    )
    todo_category_manual = models.BooleanField(default=False)
    starts_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    all_day = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    completed = models.BooleanField(default=False, db_index=True)
    source_type = models.CharField(
        max_length=10,
        choices=SourceType.choices,
        default=SourceType.MANUAL,
    )
    slack_channel_id = models.CharField(max_length=50, null=True, blank=True)
    slack_message_ts = models.CharField(max_length=50, null=True, blank=True)
    source_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["completed", "starts_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True) | models.Q(ends_at__gte=models.F("starts_at")),
                name="schedule_end_not_before_start",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(item_type="schedule", starts_at__isnull=False)
                    | models.Q(item_type="todo", ends_at__isnull=True)
                ),
                name="schedule_item_timing_matches_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        item_type="schedule",
                        todo_category__isnull=True,
                        todo_category_manual=False,
                    )
                    | models.Q(item_type="todo", todo_category__isnull=False)
                ),
                name="schedule_item_category_matches_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source_type="manual",
                        slack_channel_id__isnull=True,
                        slack_message_ts__isnull=True,
                    )
                    | models.Q(
                        source_type="slack",
                        slack_channel_id__isnull=False,
                        slack_message_ts__isnull=False,
                    )
                ),
                name="schedule_source_fields_match",
            ),
            models.UniqueConstraint(
                fields=("slack_channel_id", "slack_message_ts"),
                name="unique_slack_schedule_message",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        self.title = self.title.strip()
        if not self.title:
            errors["title"] = "제목을 입력해주세요."
        if self.item_type == self.ItemType.SCHEDULE and not self.starts_at:
            errors["starts_at"] = "일정 시작 시각을 입력해주세요."
        if self.item_type == self.ItemType.TODO and self.ends_at:
            errors["ends_at"] = "할 일에는 종료 시각을 지정할 수 없습니다."
        if self.item_type == self.ItemType.TODO and not self.todo_category_id:
            errors["todo_category"] = "할 일 카테고리가 필요합니다."
        if self.item_type == self.ItemType.SCHEDULE and (
            self.todo_category_id or self.todo_category_manual
        ):
            errors["todo_category"] = "일정에는 할 일 카테고리를 지정할 수 없습니다."
        if self.ends_at and (not self.starts_at or self.ends_at < self.starts_at):
            errors["ends_at"] = "종료 시각은 시작 시각보다 빠를 수 없습니다."
        if self.source_type == self.SourceType.SLACK:
            if not self.slack_channel_id or not self.slack_message_ts:
                errors["source_type"] = "Slack 일정에는 채널과 메시지 정보가 필요합니다."
        elif self.slack_channel_id or self.slack_message_ts:
            errors["source_type"] = "웹 일정에는 Slack 메시지 정보를 지정할 수 없습니다."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.title


class Citation(models.Model):
    run = models.ForeignKey(ContentRun, on_delete=models.CASCADE, related_name="citations")
    title = models.CharField(max_length=300, blank=True)
    url = models.URLField(max_length=700)
    publisher = models.CharField(max_length=200, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["run", "url"], name="unique_run_citation")
        ]


class FreeQuestionMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "사용자"
        ASSISTANT = "assistant", "봇"

    class Kind(models.TextChoices):
        REQUEST = "request", "요청"
        CLARIFICATION = "clarification", "요청 보완"
        WORKFLOW_STATUS = "workflow_status", "진행 상태"
        OTHER = "other", "기타"

    external_ts = models.CharField(max_length=50, unique=True)
    channel_id = models.CharField(max_length=50, blank=True, db_index=True)
    thread_ts = models.CharField(max_length=50, db_index=True)
    role = models.CharField(max_length=10, choices=Role.choices)
    message_kind = models.CharField(
        max_length=24,
        choices=Kind.choices,
        default=Kind.OTHER,
        db_index=True,
    )
    content = models.TextField()
    knowledge_item = models.ForeignKey(
        KnowledgeItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_messages",
    )
    generated_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["generated_at", "id"]

    def __str__(self) -> str:
        return f"{self.get_role_display()} · {self.generated_at:%Y-%m-%d %H:%M}"


class UserRunState(models.Model):
    run = models.ForeignKey(ContentRun, on_delete=models.CASCADE, related_name="user_states")
    session_key = models.CharField(max_length=64, db_index=True)
    bookmarked = models.BooleanField(default=False)
    completed = models.BooleanField(default=False)
    note = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "session_key"], name="unique_session_run_state")
        ]


class UserResponse(models.Model):
    run = models.ForeignKey(ContentRun, on_delete=models.CASCADE, related_name="responses")
    session_key = models.CharField(max_length=64, db_index=True)
    question_key = models.CharField(max_length=200, blank=True)
    answer = models.TextField()
    feedback = models.TextField(blank=True)
    score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class PlatformAgent(models.Model):
    key = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120)
    capabilities = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return self.name


class PlatformApiToken(models.Model):
    name = models.CharField(max_length=120)
    agent = models.ForeignKey(
        PlatformAgent,
        on_delete=models.PROTECT,
        related_name="api_tokens",
    )
    token_prefix = models.CharField(max_length=24, unique=True)
    token_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    is_active = models.BooleanField(default=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name", "id"]

    @staticmethod
    def digest(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @classmethod
    def issue(
        cls,
        *,
        name: str,
        agent: PlatformAgent,
        scopes: list[str],
        expires_at=None,
    ) -> tuple["PlatformApiToken", str]:
        prefix = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        raw_token = f"dpt_{prefix}_{secret}"
        record = cls.objects.create(
            name=name,
            agent=agent,
            token_prefix=prefix,
            token_hash=cls.digest(raw_token),
            scopes=sorted(set(scopes)),
            expires_at=expires_at,
        )
        return record, raw_token

    @classmethod
    def authenticate(cls, raw_token: str) -> "PlatformApiToken | None":
        parts = raw_token.split("_", 2)
        if len(parts) != 3 or parts[0] != "dpt":
            return None
        record = cls.objects.select_related("agent").filter(token_prefix=parts[1]).first()
        if not record or not hmac.compare_digest(record.token_hash, cls.digest(raw_token)):
            return None
        now = timezone.now()
        if (
            not record.is_active
            or not record.agent.is_active
            or record.revoked_at
            or (record.expires_at and record.expires_at <= now)
        ):
            return None
        cls.objects.filter(pk=record.pk).update(last_used_at=now)
        record.last_used_at = now
        return record

    def allows(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes

    def __str__(self) -> str:
        return f"{self.name} ({self.token_prefix})"


class PlatformInboxItem(models.Model):
    class Status(models.TextChoices):
        COLLECTED = "collected", "수집됨"
        PROCESSED = "processed", "처리됨"
        REJECTED = "rejected", "제외됨"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_type = models.CharField(max_length=60, db_index=True)
    external_id = models.CharField(max_length=200, null=True, blank=True)
    title = models.CharField(max_length=250)
    content = models.TextField(blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.COLLECTED,
        db_index=True,
    )
    collected_by = models.ForeignKey(
        PlatformAgent,
        on_delete=models.PROTECT,
        related_name="collected_inbox_items",
    )
    collected_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-collected_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "external_id"],
                name="unique_platform_inbox_source",
            ),
        ]

    def __str__(self) -> str:
        return self.title


class PlatformTask(models.Model):
    class Status(models.TextChoices):
        COLLECTED = "collected", "수집됨"
        ANALYZING = "analyzing", "분석 중"
        DRAFT = "draft", "초안"
        NEEDS_REVIEW = "needs_review", "검토 필요"
        APPROVED = "approved", "승인됨"
        REJECTED = "rejected", "거절됨"
        REVISION_REQUESTED = "revision_requested", "수정 요청"
        QUEUED = "queued", "실행 대기"
        EXECUTING = "executing", "실행 중"
        COMPLETED = "completed", "완료"
        FAILED = "failed", "실패"

    class Priority(models.TextChoices):
        LOW = "low", "낮음"
        NORMAL = "normal", "보통"
        HIGH = "high", "높음"
        URGENT = "urgent", "긴급"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.COLLECTED,
        db_index=True,
    )
    priority = models.CharField(
        max_length=10,
        choices=Priority.choices,
        default=Priority.NORMAL,
        db_index=True,
    )
    inbox_item = models.ForeignKey(
        PlatformInboxItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
    )
    created_by = models.ForeignKey(
        PlatformAgent,
        on_delete=models.PROTECT,
        related_name="created_tasks",
    )
    assigned_agents = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="pf_task_status_created_idx"),
        ]

    def __str__(self) -> str:
        return self.title


class PlatformArtifact(models.Model):
    class Kind(models.TextChoices):
        SOURCE = "source", "원본"
        ANALYSIS = "analysis", "분석"
        DRAFT = "draft", "초안"
        ANSWER = "answer", "답변"
        REPORT = "report", "보고서"
        RESULT = "result", "실행 결과"
        OTHER = "other", "기타"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    series_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    version = models.PositiveIntegerField()
    task = models.ForeignKey(
        PlatformTask,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    title = models.CharField(max_length=250)
    mime_type = models.CharField(max_length=100, default="text/markdown")
    artifact_path = models.CharField(max_length=1024)
    content_sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField()
    created_by = models.ForeignKey(
        PlatformAgent,
        on_delete=models.PROTECT,
        related_name="created_artifacts",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["series_id", "version"]
        constraints = [
            models.UniqueConstraint(
                fields=["series_id", "version"],
                name="unique_platform_artifact_version",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} · v{self.version}"


class PlatformApproval(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "검토 대기"
        APPROVED = "approved", "승인"
        REJECTED = "rejected", "거절"
        REVISION_REQUESTED = "revision_requested", "수정 요청"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        PlatformTask,
        on_delete=models.CASCADE,
        related_name="approvals",
    )
    artifact = models.ForeignKey(
        PlatformArtifact,
        on_delete=models.PROTECT,
        related_name="approvals",
    )
    target_sha256 = models.CharField(max_length=64)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    request_note = models.TextField(blank=True)
    decision_note = models.TextField(blank=True)
    requested_by = models.ForeignKey(
        PlatformAgent,
        on_delete=models.PROTECT,
        related_name="requested_approvals",
    )
    decided_by = models.ForeignKey(
        PlatformAgent,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="decided_approvals",
    )
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["artifact"],
                name="unique_platform_artifact_approval",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.task.title} · {self.get_status_display()}"


class PlatformEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=80, db_index=True)
    entity_type = models.CharField(max_length=40, db_index=True)
    entity_id = models.CharField(max_length=64, db_index=True)
    task = models.ForeignKey(
        PlatformTask,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="events",
    )
    actor = models.ForeignKey(
        PlatformAgent,
        on_delete=models.PROTECT,
        related_name="platform_events",
    )
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["entity_type", "entity_id", "created_at"], name="pf_event_entity_idx"),
        ]

    def __str__(self) -> str:
        return self.event_type


class PlatformIdempotencyRecord(models.Model):
    token = models.ForeignKey(
        PlatformApiToken,
        on_delete=models.PROTECT,
        related_name="idempotency_records",
    )
    key = models.CharField(max_length=128)
    method = models.CharField(max_length=10)
    path = models.CharField(max_length=300)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["token", "key", "method", "path"],
                name="unique_platform_idempotency_key",
            ),
        ]
