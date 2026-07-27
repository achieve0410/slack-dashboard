from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_date

from .knowledge_tags import normalize_filter_label
from .models import Category, KnowledgeItem


FILTER_KEYS = (
    "q",
    "tag",
    "category",
    "status",
    "source_type",
    "read",
    "bookmarked",
    "completed",
    "archived",
    "period",
    "from",
    "to",
    "sort",
)
PAGINATION_KEYS = ("limit", "offset")


class KnowledgeFilterError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_filter", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ParsedKnowledgeFilters:
    values: dict

    @property
    def sort(self) -> str:
        return self.values.get("sort", "newest")

    @property
    def filters(self) -> dict:
        return {key: value for key, value in self.values.items() if key != "sort"}

    @property
    def canonical_query(self) -> str:
        return urlencode([(key, self.values[key]) for key in FILTER_KEYS if key in self.values])


def _single_value(params, key: str):
    if hasattr(params, "getlist"):
        values = params.getlist(key)
        if len(values) > 1:
            raise KnowledgeFilterError(f"{key}는 한 번만 지정할 수 있습니다.")
        return values[0] if values else None
    value = params.get(key)
    if isinstance(value, (dict, list, tuple)):
        raise KnowledgeFilterError(f"{key}는 단일 값이어야 합니다.")
    return value


def _keys(params) -> set[str]:
    return {str(key) for key in params.keys()}


def _parse_iso_date(value, field: str) -> date:
    parsed = parse_date(str(value))
    if parsed is None:
        raise KnowledgeFilterError(f"{field}는 YYYY-MM-DD 형식이어야 합니다.")
    return parsed


def parse_knowledge_filters(
    params,
    *,
    required_query: bool = False,
    allow_pagination: bool = False,
    stale_category: bool = False,
) -> ParsedKnowledgeFilters:
    allowed = set(FILTER_KEYS)
    if allow_pagination:
        allowed.update(PAGINATION_KEYS)
    unknown = sorted(_keys(params) - allowed)
    if unknown:
        raise KnowledgeFilterError(f"지원하지 않는 필터입니다: {', '.join(unknown)}")

    values = {}
    query = _single_value(params, "q")
    if query is not None:
        query = str(query).strip()
        if len(query) > 200:
            raise KnowledgeFilterError("검색어는 200자 이하여야 합니다.")
        if query:
            values["q"] = query
    if required_query and "q" not in values:
        raise KnowledgeFilterError("검색어를 입력해주세요.", code="query_required")

    tag = _single_value(params, "tag")
    if tag not in (None, ""):
        try:
            values["tag"] = normalize_filter_label(str(tag))
        except Exception as error:
            raise KnowledgeFilterError("올바른 tag가 아닙니다.") from error

    category = _single_value(params, "category")
    if category not in (None, ""):
        try:
            category_id = int(category)
        except (TypeError, ValueError) as error:
            raise KnowledgeFilterError("category는 정수여야 합니다.") from error
        if category_id not in Category.active_tree_ids():
            if stale_category:
                raise KnowledgeFilterError(
                    "저장된 보기의 카테고리가 더 이상 활성 상태가 아닙니다.",
                    code="stale_category",
                )
            raise KnowledgeFilterError(
                "카테고리를 찾을 수 없습니다.",
                code="category_not_found",
                status=404,
            )
        values["category"] = category_id

    status = _single_value(params, "status")
    if status not in (None, ""):
        if status not in KnowledgeItem.Status.values:
            raise KnowledgeFilterError("올바른 status가 아닙니다.")
        values["status"] = status

    source_type = _single_value(params, "source_type")
    if source_type not in (None, ""):
        if source_type not in KnowledgeItem.SourceType.values:
            raise KnowledgeFilterError("올바른 source_type이 아닙니다.")
        values["source_type"] = source_type

    read = _single_value(params, "read")
    if read not in (None, ""):
        if read not in ("read", "unread"):
            raise KnowledgeFilterError("read는 read 또는 unread여야 합니다.")
        values["read"] = read

    for field in ("bookmarked", "completed"):
        value = _single_value(params, field)
        if value not in (None, ""):
            if str(value) != "1":
                raise KnowledgeFilterError(f"{field}는 1이어야 합니다.")
            values[field] = "1"

    archived = _single_value(params, "archived")
    if archived not in (None, ""):
        if archived not in ("exclude", "include", "only"):
            raise KnowledgeFilterError("archived는 exclude, include 또는 only여야 합니다.")
        if archived != "exclude":
            values["archived"] = archived

    period = _single_value(params, "period")
    from_value = _single_value(params, "from")
    to_value = _single_value(params, "to")
    if period not in (None, ""):
        if period not in ("today", "7d", "30d", "custom"):
            raise KnowledgeFilterError("올바른 period가 아닙니다.")
        values["period"] = period
    if period == "custom":
        if from_value in (None, "") or to_value in (None, ""):
            raise KnowledgeFilterError("custom 기간에는 from과 to가 필요합니다.")
        from_date = _parse_iso_date(from_value, "from")
        to_date = _parse_iso_date(to_value, "to")
        if from_date > to_date:
            raise KnowledgeFilterError("from은 to보다 늦을 수 없습니다.")
        values["from"] = from_date.isoformat()
        values["to"] = to_date.isoformat()
    elif from_value not in (None, "") or to_value not in (None, ""):
        raise KnowledgeFilterError("from과 to는 period=custom에서만 사용할 수 있습니다.")

    sort = _single_value(params, "sort")
    if sort not in (None, ""):
        if sort not in ("newest", "oldest"):
            raise KnowledgeFilterError("sort는 newest 또는 oldest여야 합니다.")
        if sort != "newest":
            values["sort"] = sort

    return ParsedKnowledgeFilters(
        {key: values[key] for key in FILTER_KEYS if key in values}
    )


def parse_saved_filter_values(
    filters,
    *,
    sort: str = "newest",
    stale_category: bool = False,
) -> ParsedKnowledgeFilters:
    if not isinstance(filters, dict):
        raise KnowledgeFilterError("filters는 객체여야 합니다.")
    values = dict(filters)
    values["sort"] = sort
    return parse_knowledge_filters(values, stale_category=stale_category)


def _date_bounds(values: dict) -> tuple[datetime, datetime] | None:
    period = values.get("period")
    if not period:
        return None
    today = timezone.localdate()
    if period == "today":
        start_date = today
        end_date = today
    elif period == "7d":
        start_date = today - timedelta(days=6)
        end_date = today
    elif period == "30d":
        start_date = today - timedelta(days=29)
        end_date = today
    else:
        start_date = date.fromisoformat(values["from"])
        end_date = date.fromisoformat(values["to"])
    current_timezone = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(start_date, time.min), current_timezone)
    end = timezone.make_aware(
        datetime.combine(end_date + timedelta(days=1), time.min),
        current_timezone,
    )
    return start, end


def apply_knowledge_filters(
    queryset: QuerySet,
    parsed: ParsedKnowledgeFilters,
    *,
    tag_snapshot_id: int | None,
) -> QuerySet:
    values = parsed.values
    archived = values.get("archived", "exclude")
    if archived == "exclude":
        queryset = queryset.filter(consumption_state__archived_at__isnull=True)
    elif archived == "only":
        queryset = queryset.filter(consumption_state__archived_at__isnull=False)

    if "status" in values:
        queryset = queryset.filter(status=values["status"])
    if "source_type" in values:
        queryset = queryset.filter(source_type=values["source_type"])
    if values.get("read") == "read":
        queryset = queryset.filter(consumption_state__read_at__isnull=False)
    elif values.get("read") == "unread":
        queryset = queryset.filter(consumption_state__read_at__isnull=True)
    if values.get("bookmarked") == "1":
        queryset = queryset.filter(consumption_state__bookmarked_at__isnull=False)
    if values.get("completed") == "1":
        queryset = queryset.filter(consumption_state__completed_at__isnull=False)

    query = values.get("q")
    if query:
        tag_query = Q()
        if tag_snapshot_id:
            tag_query = Q(
                tag_assignments__snapshot_id=tag_snapshot_id,
                tag_assignments__tag__label__icontains=query,
            )
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(summary__icontains=query)
            | Q(question__icontains=query)
            | Q(answer__icontains=query)
            | Q(content_run__body__icontains=query)
            | Q(category__path__icontains=query)
            | tag_query
        ).distinct()

    tag = values.get("tag")
    if tag:
        if not tag_snapshot_id:
            queryset = queryset.none()
        else:
            queryset = queryset.filter(
                tag_assignments__snapshot_id=tag_snapshot_id,
                tag_assignments__tag__normalized_label=tag,
            ).distinct()

    category_id = values.get("category")
    if category_id:
        active_categories = list(
            Category.objects.filter(pk__in=Category.active_tree_ids()).only(
                "id", "path"
            )
        )
        category = next(row for row in active_categories if row.pk == category_id)
        path_prefix = f"{category.path}/"
        descendant_ids = [
            row.pk
            for row in active_categories
            if row.path == category.path or row.path.startswith(path_prefix)
        ]
        queryset = queryset.filter(category_id__in=descendant_ids)

    bounds = _date_bounds(values)
    if bounds:
        start, end = bounds
        queryset = queryset.filter(generated_at__gte=start, generated_at__lt=end)

    if parsed.sort == "oldest":
        return queryset.order_by("generated_at", "id")
    return queryset.order_by("-generated_at", "-id")
