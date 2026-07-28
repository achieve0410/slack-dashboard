import json
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from .models import ScheduleCategory, ScheduleEvent


class ScheduleApiTests(TestCase):
    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.now = timezone.now().replace(second=0, microsecond=0)

    def csrf_token(self) -> str:
        self.client.get("/api/csrf/")
        return self.client.cookies["csrftoken"].value

    def request(self, method: str, url: str, payload: dict | None = None):
        token = self.csrf_token()
        return getattr(self.client, method)(
            url,
            data=json.dumps(payload or {}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )

    def test_schedule_crud_and_date_filter(self):
        created = self.request(
            "post",
            "/api/schedule/",
            {
                "title": "대시보드 검토",
                "starts_at": self.now.isoformat(),
                "ends_at": (self.now + timedelta(hours=1)).isoformat(),
                "notes": "보완 사항 정리",
                "all_day": False,
            },
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["source_type"], ScheduleEvent.SourceType.MANUAL)
        self.assertEqual(created.json()["source_label"], "웹")
        event_id = created.json()["id"]
        day = timezone.localdate(self.now).isoformat()
        listed = self.client.get(f"/api/schedule/?from={day}&to={day}")
        self.assertEqual([item["id"] for item in listed.json()["results"]], [event_id])

        updated = self.request(
            "patch",
            f"/api/schedule/{event_id}/",
            {"title": "대시보드 최종 검토", "completed": True},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["title"], "대시보드 최종 검토")
        self.assertTrue(updated.json()["completed"])

        deleted = self.request("delete", f"/api/schedule/{event_id}/")
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(ScheduleEvent.objects.filter(pk=event_id).exists())

    def test_schedule_rejects_invalid_time_range_and_empty_title(self):
        invalid_range = self.request(
            "post",
            "/api/schedule/",
            {
                "title": "잘못된 일정",
                "starts_at": self.now.isoformat(),
                "ends_at": (self.now - timedelta(hours=1)).isoformat(),
            },
        )
        empty_title = self.request(
            "post",
            "/api/schedule/",
            {"title": " ", "starts_at": self.now.isoformat()},
        )

        self.assertEqual(invalid_range.status_code, 400)
        self.assertEqual(empty_title.status_code, 400)
        self.assertEqual(ScheduleEvent.objects.count(), 0)

    def test_schedule_invalid_json_returns_only_the_public_validation_message(self):
        response = self.client.post(
            "/api/schedule/",
            data='{"title":',
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"error": "올바른 JSON 요청이 아닙니다."},
        )

    def test_slack_schedule_allows_only_completion_updates(self):
        event = ScheduleEvent.objects.create(
            title="Slack 일정",
            starts_at=self.now,
            source_type=ScheduleEvent.SourceType.SLACK,
            slack_channel_id="C1234567890",
            slack_message_ts="100.100",
        )

        completed = self.request(
            "patch",
            f"/api/schedule/{event.id}/",
            {"completed": True},
        )
        edited = self.request(
            "patch",
            f"/api/schedule/{event.id}/",
            {"title": "웹에서 수정"},
        )
        deleted = self.request("delete", f"/api/schedule/{event.id}/")

        self.assertEqual(completed.status_code, 200)
        self.assertTrue(completed.json()["completed"])
        self.assertEqual(completed.json()["source_type"], ScheduleEvent.SourceType.SLACK)
        self.assertEqual(completed.json()["slack_channel_id"], "C1234567890")
        self.assertEqual(edited.status_code, 409)
        self.assertEqual(deleted.status_code, 409)
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertEqual(event.title, "Slack 일정")

    def test_creates_and_recategorizes_todos(self):
        created = self.request(
            "post",
            "/api/schedule/",
            {
                "item_type": "todo",
                "title": "자료 검토하기",
                "starts_at": None,
                "ends_at": None,
                "notes": "금요일 전 확인",
                "all_day": False,
            },
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["item_type"], ScheduleEvent.ItemType.TODO)
        self.assertEqual(
            created.json()["todo_category_label"],
            "업무",
        )
        self.assertIsNone(created.json()["starts_at"])

        updated = self.request(
            "patch",
            f"/api/schedule/{created.json()['id']}/",
            {"title": "영어 시험 공부"},
        )

        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            updated.json()["todo_category_label"],
            "학습",
        )

    def test_date_filter_includes_dated_todo_and_excludes_undated_todo(self):
        day = timezone.localdate(self.now).isoformat()
        undated = self.request(
            "post",
            "/api/schedule/",
            {"item_type": "todo", "title": "기한 없는 할 일", "starts_at": None},
        ).json()
        dated = self.request(
            "post",
            "/api/schedule/",
            {
                "item_type": "todo",
                "title": "오늘 할 일",
                "starts_at": self.now.isoformat(),
                "all_day": True,
            },
        ).json()

        response = self.client.get(f"/api/schedule/?from={day}&to={day}")

        self.assertEqual([item["id"] for item in response.json()["results"]], [dated["id"]])
        self.assertNotEqual(undated["id"], dated["id"])

    def test_manages_categories_and_reclassifies_automatic_todos(self):
        todo = self.request(
            "post",
            "/api/schedule/",
            {"item_type": "todo", "title": "캠핑 준비", "starts_at": None},
        ).json()
        self.assertEqual(todo["todo_category_label"], "기타")

        created = self.request(
            "post",
            "/api/schedule/categories/",
            {"name": "야외활동", "keywords": ["캠핑", "등산"]},
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["name"], "야외활동")
        refreshed = self.client.get("/api/schedule/").json()["results"][0]
        self.assertEqual(refreshed["todo_category_label"], "야외활동")

        updated = self.request(
            "patch",
            f"/api/schedule/categories/{created.json()['id']}/",
            {"name": "아웃도어", "keywords": ["캠핑", "등산", "트레킹"]},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["name"], "아웃도어")
        self.assertEqual(updated.json()["keywords"], ["캠핑", "등산", "트레킹"])

        in_use = self.request(
            "delete",
            f"/api/schedule/categories/{created.json()['id']}/",
        )
        self.assertEqual(in_use.status_code, 409)

    def test_manually_assigns_and_resets_todo_category(self):
        event = self.request(
            "post",
            "/api/schedule/",
            {"item_type": "todo", "title": "자료 검토", "starts_at": None},
        ).json()
        travel = ScheduleCategory.objects.get(name="여행")

        assigned = self.request(
            "patch",
            f"/api/schedule/{event['id']}/",
            {"todo_category_id": travel.pk},
        )
        reset = self.request(
            "patch",
            f"/api/schedule/{event['id']}/",
            {"todo_category_id": None},
        )

        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.json()["todo_category_label"], "여행")
        self.assertTrue(assigned.json()["todo_category_manual"])
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["todo_category_label"], "업무")
        self.assertFalse(reset.json()["todo_category_manual"])
