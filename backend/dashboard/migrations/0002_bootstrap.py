"""Seed the baseline rows a fresh installation needs.

- Default schedule/TODO categories (schedule_sync.py falls back to the one
  marked is_fallback=True when no keyword matches).
- The knowledge-tag singleton rows (active snapshot pointer + mutation lock).
- The knowledge-tag corpus revision counter, plus the DB triggers that bump
  it whenever a knowledge item, content run, or category changes (MySQL and
  SQLite only; other backends get no triggers and the tagging pipeline
  falls back to treating the corpus as always-fresh).
"""

from django.db import migrations
from django.utils import timezone


CATEGORY_SEEDS = (
    (
        "업무",
        ["업무", "회의", "보고", "검토", "프로젝트", "문서", "자료", "고객", "메일", "발표", "계약", "배포", "개발"],
        10,
        False,
    ),
    (
        "학습",
        ["학습", "공부", "시험", "자격증", "강의", "수업", "과제", "복습", "영어", "일본어", "aws", "책", "읽기"],
        20,
        False,
    ),
    (
        "건강",
        ["병원", "운동", "건강", "검진", "약", "복용", "치과", "진료", "예방접종"],
        30,
        False,
    ),
    (
        "재무",
        ["결제", "납부", "송금", "예산", "세금", "보험", "은행", "카드", "투자", "주식"],
        40,
        False,
    ),
    (
        "여행",
        ["여행", "비행기", "항공", "숙소", "호텔", "펜션", "여권", "출국", "입국"],
        50,
        False,
    ),
    (
        "생활",
        ["구매", "장보기", "청소", "세탁", "예약", "정리", "가족", "차량", "자동차", "택배"],
        60,
        False,
    ),
    ("기타", [], 999, True),
)

TRIGGER_NAMES = (
    "dashboard_kti_insert_rev",
    "dashboard_kti_update_rev",
    "dashboard_kti_delete_rev",
    "dashboard_ktr_content_update_rev",
    "dashboard_ktr_content_delete_rev",
    "dashboard_ktr_category_update_rev",
    "dashboard_ktr_category_delete_rev",
)


def seed_schedule_categories(apps, _schema_editor):
    ScheduleCategory = apps.get_model("dashboard", "ScheduleCategory")
    for name, keywords, sort_order, is_fallback in CATEGORY_SEEDS:
        ScheduleCategory.objects.create(
            name=name,
            keywords=keywords,
            sort_order=sort_order,
            is_fallback=is_fallback,
        )


def bootstrap_tag_snapshot(apps, _schema_editor):
    KnowledgeTagSnapshot = apps.get_model("dashboard", "KnowledgeTagSnapshot")
    KnowledgeTagActiveSnapshot = apps.get_model("dashboard", "KnowledgeTagActiveSnapshot")
    KnowledgeTagMutationLock = apps.get_model("dashboard", "KnowledgeTagMutationLock")

    snapshot = KnowledgeTagSnapshot.objects.create(
        status="active",
        inventory_digest="",
        artifact_manifest={},
        item_count=0,
        tag_count=0,
        assignment_count=0,
        published_at=timezone.now(),
    )
    KnowledgeTagActiveSnapshot.objects.create(singleton_key=1, snapshot=snapshot)
    KnowledgeTagMutationLock.objects.create(singleton_key=1)


def bootstrap_corpus_revision(apps, _schema_editor):
    KnowledgeTagCorpusRevision = apps.get_model("dashboard", "KnowledgeTagCorpusRevision")
    KnowledgeTagCorpusRevision.objects.get_or_create(
        singleton_key=1,
        defaults={"revision": 1},
    )


def install_triggers(_apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "mysql":
        statements = _mysql_triggers()
    elif vendor == "sqlite":
        statements = _sqlite_triggers()
    else:
        statements = ()
    with schema_editor.connection.cursor() as cursor:
        for name in TRIGGER_NAMES:
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")
        for statement in statements:
            cursor.execute(statement)


def drop_triggers(_apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for name in TRIGGER_NAMES:
            cursor.execute(f"DROP TRIGGER IF EXISTS {name}")


def _mysql_touch_statement() -> str:
    return (
        "UPDATE dashboard_knowledgetagcorpusrevision "
        "SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP(6) "
        "WHERE singleton_key = 1"
    )


def _mysql_triggers() -> tuple[str, ...]:
    touch = _mysql_touch_statement()
    return (
        f"""
        CREATE TRIGGER dashboard_kti_insert_rev
        AFTER INSERT ON dashboard_knowledgeitem
        FOR EACH ROW
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_kti_update_rev
        AFTER UPDATE ON dashboard_knowledgeitem
        FOR EACH ROW
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_kti_delete_rev
        AFTER DELETE ON dashboard_knowledgeitem
        FOR EACH ROW
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_ktr_content_update_rev
        AFTER UPDATE ON dashboard_contentrun
        FOR EACH ROW
        BEGIN
            IF EXISTS (
                SELECT 1 FROM dashboard_knowledgeitem
                WHERE content_run_id = NEW.id
                LIMIT 1
            ) THEN
                {touch};
            END IF;
        END
        """,
        f"""
        CREATE TRIGGER dashboard_ktr_content_delete_rev
        BEFORE DELETE ON dashboard_contentrun
        FOR EACH ROW
        BEGIN
            IF EXISTS (
                SELECT 1 FROM dashboard_knowledgeitem
                WHERE content_run_id = OLD.id
                LIMIT 1
            ) THEN
                {touch};
            END IF;
        END
        """,
        f"""
        CREATE TRIGGER dashboard_ktr_category_update_rev
        AFTER UPDATE ON dashboard_category
        FOR EACH ROW
        BEGIN
            IF EXISTS (
                SELECT 1 FROM dashboard_knowledgeitem
                WHERE category_id = NEW.id
                LIMIT 1
            ) THEN
                {touch};
            END IF;
        END
        """,
        f"""
        CREATE TRIGGER dashboard_ktr_category_delete_rev
        AFTER DELETE ON dashboard_category
        FOR EACH ROW
        BEGIN
            IF EXISTS (
                SELECT 1 FROM dashboard_knowledgeitem
                WHERE category_id = OLD.id
                LIMIT 1
            ) THEN
                {touch};
            END IF;
        END
        """,
    )


def _sqlite_touch_statement() -> str:
    return (
        "UPDATE dashboard_knowledgetagcorpusrevision "
        "SET revision = revision + 1, updated_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW') "
        "WHERE singleton_key = 1"
    )


def _sqlite_triggers() -> tuple[str, ...]:
    touch = _sqlite_touch_statement()
    return (
        f"""
        CREATE TRIGGER dashboard_kti_insert_rev
        AFTER INSERT ON dashboard_knowledgeitem
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_kti_update_rev
        AFTER UPDATE ON dashboard_knowledgeitem
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_kti_delete_rev
        AFTER DELETE ON dashboard_knowledgeitem
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_ktr_content_update_rev
        AFTER UPDATE ON dashboard_contentrun
        WHEN EXISTS (
            SELECT 1 FROM dashboard_knowledgeitem
            WHERE content_run_id = NEW.id
            LIMIT 1
        )
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_ktr_content_delete_rev
        BEFORE DELETE ON dashboard_contentrun
        WHEN EXISTS (
            SELECT 1 FROM dashboard_knowledgeitem
            WHERE content_run_id = OLD.id
            LIMIT 1
        )
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_ktr_category_update_rev
        AFTER UPDATE ON dashboard_category
        WHEN EXISTS (
            SELECT 1 FROM dashboard_knowledgeitem
            WHERE category_id = NEW.id
            LIMIT 1
        )
        BEGIN
            {touch};
        END
        """,
        f"""
        CREATE TRIGGER dashboard_ktr_category_delete_rev
        AFTER DELETE ON dashboard_category
        WHEN EXISTS (
            SELECT 1 FROM dashboard_knowledgeitem
            WHERE category_id = OLD.id
            LIMIT 1
        )
        BEGIN
            {touch};
        END
        """,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_schedule_categories, migrations.RunPython.noop),
        migrations.RunPython(bootstrap_tag_snapshot, migrations.RunPython.noop),
        migrations.RunPython(bootstrap_corpus_revision, migrations.RunPython.noop),
        migrations.RunPython(install_triggers, drop_triggers),
    ]
