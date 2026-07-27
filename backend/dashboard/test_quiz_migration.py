from django.db import connection
from django.test import TestCase


class QuizMigrationTests(TestCase):
    def test_quiz_tables_and_portable_constraints_exist(self):
        table_names = set(connection.introspection.table_names())
        expected_tables = {
            "dashboard_quizgenerationbatch",
            "dashboard_quizquestion",
            "dashboard_quizsession",
            "dashboard_quizsessionitem",
            "dashboard_quizprogress",
        }
        self.assertTrue(expected_tables.issubset(table_names))

        constraints = connection.introspection.get_constraints(
            connection.cursor(),
            "dashboard_quizsessionitem",
        )
        self.assertIn("quiz_session_item_unique_position", constraints)
        self.assertTrue(constraints["quiz_session_item_unique_position"]["unique"])
        self.assertIn("quiz_session_item_unique_question", constraints)
        self.assertTrue(constraints["quiz_session_item_unique_question"]["unique"])
        self.assertIn("quiz_session_item_position_1_10", constraints)
        self.assertTrue(constraints["quiz_session_item_position_1_10"]["check"])

    def test_quiz_progress_is_one_row_per_question(self):
        constraints = connection.introspection.get_constraints(
            connection.cursor(),
            "dashboard_quizprogress",
        )
        unique_columns = {
            tuple(details["columns"])
            for details in constraints.values()
            if details.get("unique")
        }
        self.assertIn(("question_id",), unique_columns)

