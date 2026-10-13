import sqlite3
import tempfile
import unittest
import csv
from pathlib import Path
from unittest.mock import patch

import app
import import_budget


# Each case runs against an isolated SQLite database and a small fixture set so assertions stay independent from the bundled demo data.

class BudgetDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_directory.name) / "budget.db"
        self.database_path_patch = patch.object(app, "DB_PATH", self.database_path)
        self.database_path_patch.start()
        with patch.object(app, "seed_demo_web_accounts"):
            app.initialize_database()
        self.data_entry = import_budget.Principal("data-entry", "data_entry")
        self.reviewer = import_budget.Principal("reviewer", "reviewer")
        self.admin = import_budget.Principal("admin-user", "admin")
        self.central_admin = import_budget.Principal("central-admin-user", "central_admin")
        with app.database_connection() as connection:
            for principal in (self.data_entry, self.reviewer, self.admin, self.central_admin):
                salt = principal.username.encode("utf-8")
                connection.execute(
                    """
                    INSERT INTO admin_users (
                        username, password_salt, password_hash, password_iterations,
                        role, created_at, created_by
                    ) VALUES (?, ?, ?, 10, ?, '2026-01-01T00:00:00+00:00', 'test')
                    """,
                    (
                        principal.username,
                        salt,
                        import_budget._password_digest("test-password-123", salt, 10),
                        principal.role,
                    ),
                )

    def tearDown(self):
        self.database_path_patch.stop()
        self.temp_directory.cleanup()

    def test_catalog_includes_union_states_and_union_territories(self):
        result = app.query_coverage()

        self.assertEqual(result["counts_by_type"]["union"], 1)
        self.assertEqual(result["counts_by_type"]["state"], 28)
        self.assertEqual(result["counts_by_type"]["union_territory"], 8)
        self.assertEqual(result["counts_by_type"]["district"], 735)
        self.assertEqual(result["jurisdiction_count"], 772)
        self.assertEqual(result["districts_with_figures"], 34)
        self.assertEqual(result["districts_with_illustrative_data"], 699)
        union = result["jurisdictions"][0]
        self.assertEqual(union["data_status"], "source_and_illustrative_descendant_data")
        maharashtra = next(child for child in union["children"] if child["code"] == "IN-MH")
        pune = next(child for child in maharashtra["children"] if child["code"] == "IN-MH-PUNE")
        self.assertEqual(maharashtra["data_status"], "source_and_child_data")
        self.assertEqual(pune["data_status"], "source_data")
        bihar = next(child for child in union["children"] if child["code"] == "IN-BR")
        patna = next(child for child in bihar["children"] if child["name"] == "Patna")
        self.assertEqual(bihar["data_status"], "illustrative_descendant_data")
        self.assertEqual(patna["data_status"], "illustrative_data")
        self.assertEqual(result["figure_count"], 5664)
        self.assertEqual(result["source_figure_count"], 72)
        self.assertEqual(result["illustrative_figure_count"], 5592)

    def test_statewide_query_keeps_stages_separate(self):
        result = app.query_budget_records("2025-26")

        self.assertEqual(result["figure_count"], 4)
        self.assertEqual(
            {item["budget_stage"]: item["amount"] for item in result["totals_by_stage"]},
            {"actual": 113000, "budget_estimate": 120000},
        )
        self.assertEqual(len(result["sectors"]), 4)

    def test_district_query_does_not_infer_statewide_values(self):
        result = app.query_budget_records("2025-26", district="Pune")
        state_scope = app.query_budget_records("2025-26")

        self.assertEqual(result["figure_count"], 2)
        self.assertEqual({row["jurisdiction"] for row in result["records"]}, {"Pune"})
        self.assertEqual({row["jurisdiction"] for row in state_scope["records"]}, {"Maharashtra", "Pune"})
        self.assertEqual(
            {row["budget_stage"] for row in result["records"]},
            {"actual", "budget_estimate"},
        )

    def test_illustrative_state_rows_have_labeled_sample_spent_amounts(self):
        result = app.query_budget_records(
            "2026-27", jurisdiction_code="IN-BR", budget_stage="actual"
        )

        self.assertTrue(result["records"])
        self.assertTrue(
            all(
                record["spent_amount"] == round(record["amount"] * 0.60, 2)
                for record in result["records"]
            )
        )
        self.assertTrue(
            all(
                record["source"] == "Illustrative demo dataset (synthetic, not official)"
                for record in result["records"]
            )
        )

        source_backed = app.query_budget_records(
            "2025-26", jurisdiction_code="IN-MH"
        )
        self.assertTrue(source_backed["records"])
        self.assertTrue(
            all(record["spent_amount"] is None for record in source_backed["records"])
        )

    def test_union_budget_scope_does_not_sum_state_budgets(self):
        result = app.query_budget_records("2025-26", jurisdiction_code="IN-UNION")

        self.assertEqual(result["figure_count"], 0)
        self.assertIn("No source-backed", result["message"])

    def test_original_names_category_and_source_are_preserved(self):
        result = app.query_budget_records(
            "2025-26",
            budget_stage="budget_estimate",
            search="education",
        )
        record = result["records"][0]

        self.assertEqual(record["sector_name_original"], "Education")
        self.assertEqual(record["sector_category_slug"], "education")
        self.assertIsNone(record["department_name_original"])
        self.assertEqual(record["amount"], 75000)
        self.assertEqual(record["currency_code"], "INR")
        self.assertEqual(record["amount_unit"], "crore")
        self.assertEqual(record["source_locator"], "Slide 5 chart: Maharashtra Education budget estimate")

    def test_sector_and_department_filters_are_applied(self):
        education = app.query_budget_records(
            "2025-26", jurisdiction_code="IN-MH", sector="education"
        )
        department = app.query_budget_records(
            "2026-27", jurisdiction_code="IN-BR", department="Education"
        )

        self.assertEqual(education["figure_count"], 2)
        self.assertTrue(all(row["sector_category_slug"] == "education" for row in education["records"]))
        self.assertGreater(department["figure_count"], 0)
        self.assertTrue(
            all("education" in row["department_name_original"].casefold() for row in department["records"])
        )

    def test_comparison_groups_selected_states_and_years_without_mixing_stages(self):
        result = app.query_budget_comparison(
            ["IN-MH", "IN-BR"],
            ["2025-26", "2026-27"],
            sector="education",
        )

        self.assertGreater(result["row_count"], 0)
        self.assertEqual(
            {row["jurisdiction"] for row in result["rows"]},
            {"Maharashtra", "Bihar"},
        )
        self.assertEqual(
            {row["fiscal_year"] for row in result["rows"]},
            {"2025-26", "2026-27"},
        )
        self.assertTrue(
            all(row["budget_stage"] in app.BUDGET_STAGES for row in result["rows"])
        )
        self.assertIn("may overlap", result["note"])

    def test_comparison_includes_reported_spent_totals_and_preserves_missing_values(self):
        illustrative = app.query_budget_comparison(
            ["IN-BR"], ["2026-27"], sectors=["education"]
        )
        actual_row = next(
            row for row in illustrative["rows"] if row["budget_stage"] == "actual"
        )
        records = app.query_budget_records(
            "2026-27", jurisdiction_code="IN-BR", budget_stage="actual",
            sector="education",
        )
        self.assertEqual(
            actual_row["spent_amount"],
            sum(record["spent_amount"] for record in records["records"]),
        )

        source_backed = app.query_budget_comparison(
            ["IN-MH"], ["2025-26"], sectors=["education"]
        )
        self.assertTrue(source_backed["rows"])
        self.assertTrue(all(row["spent_amount"] is None for row in source_backed["rows"]))

    def test_comparison_keeps_selected_sectors_as_separate_rows(self):
        result = app.query_budget_comparison(
            ["IN-BR"],
            ["2026-27"],
            sectors=["education", "agriculture", "roadways_transport", "healthcare"],
        )

        self.assertEqual(
            {row["sector_slug"] for row in result["rows"]},
            {"education", "agriculture", "roadways_transport", "healthcare"},
        )
        self.assertTrue(
            all(
                row["sector"] in {
                    "Education", "Agriculture", "Roadways & Transport", "Healthcare"
                }
                for row in result["rows"]
            )
        )
        self.assertEqual(
            len({
                (
                    row["sector_slug"],
                    row["budget_stage"],
                    row["currency_code"],
                    row["amount_unit"],
                    row["is_illustrative"],
                )
                for row in result["rows"]
            }),
            len(result["rows"]),
        )

        selected = app.query_budget_comparison(
            ["IN-BR"], ["2026-27"], sectors=["education"]
        )
        self.assertTrue(
            all(row["sector_slug"] == "education" for row in selected["rows"])
        )

    def test_comparison_rejects_empty_or_unknown_sector_selection(self):
        with self.assertRaisesRegex(ValueError, "valid sectors"):
            app.query_budget_comparison(["IN-BR"], ["2026-27"], sectors=[])
        with self.assertRaisesRegex(ValueError, "valid sectors"):
            app.query_budget_comparison(["IN-BR"], ["2026-27"], sectors=["unknown"])

    def test_comparison_rejects_districts_and_unknown_years(self):
        with self.assertRaises(ValueError):
            app.query_budget_comparison(["IN-MH-PUNE"], ["2025-26"])
        with self.assertRaises(ValueError):
            app.query_budget_comparison(["IN-MH"], ["2099-00"])

    def test_comparison_keeps_different_units_separate(self):
        result = app.query_budget_comparison(["IN-MH"], ["2025-26", "2026-27"])
        unit_by_year = {
            (row["fiscal_year"], row["amount_unit"])
            for row in result["rows"]
        }

        self.assertIn(("2025-26", "crore"), unit_by_year)
        self.assertIn(("2026-27", "source_unit_not_stated"), unit_by_year)

    def test_search_and_missing_year_are_explicit(self):
        found = app.query_budget_records("2025-26", jurisdiction_code="IN-MH-PUNE", search="healthcare")
        missing = app.query_budget_records("2024-25")

        self.assertEqual(found["figure_count"], 2)
        self.assertEqual({row["jurisdiction"] for row in found["records"]}, {"Pune"})
        self.assertEqual(missing["figure_count"], 0)
        self.assertIn("No source-backed 2024-25", missing["message"])

    def test_seed_import_is_idempotent(self):
        app.initialize_database()
        with app.database_connection() as connection:
            result = connection.execute("SELECT COUNT(*) FROM budget_figures").fetchone()[0]

        self.assertEqual(result, 5664)

    def test_initialize_replaces_previous_demo_rows(self):
        with app.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO budget_figures (
                    jurisdiction_code, fiscal_year, budget_stage,
                    department_name_original, sector_name_original,
                    sector_category_slug, amount, currency_code, amount_unit,
                    source, source_locator
                ) VALUES (
                    'IN-BR-D-PATNA', '2026-27', 'funds_received',
                    'Education', NULL, NULL, 9.34, 'INR', 'crore',
                    'Illustrative demo dataset (synthetic, not official)',
                    'Generated demonstration value for Patna: Budget Received; not an official figure.'
                )
                """
            )

        app.initialize_database()
        result = app.query_budget_records("2026-27", jurisdiction_code="IN-BR", district="Patna")

        self.assertEqual(result["figure_count"], 8)
        self.assertEqual(
            sum(
                row["amount"] for row in result["records"]
                if row["budget_stage"] == "funds_received"
            ),
            9.34,
        )

    def test_official_district_figures_are_kept_unclassified_and_unconverted(self):
        result = app.query_budget_records("2026-27", jurisdiction_code="IN-MH")
        received = app.query_budget_records(
            "2026-27", jurisdiction_code="IN-MH", budget_stage="funds_received"
        )

        self.assertEqual(result["figure_count"], 68)
        self.assertEqual(received["figure_count"], 34)
        self.assertEqual({row["budget_stage"] for row in received["records"]}, {"funds_received"})
        self.assertEqual(result["unclassified_figure_count"], 68)
        self.assertEqual(
            {row["budget_stage"] for row in result["records"]},
            {"actual", "funds_received"},
        )
        self.assertEqual(
            {row["budget_stage"]: row["figure_count"] for row in result["totals_by_stage"]},
            {"actual": 34, "funds_received": 34},
        )
        self.assertEqual(
            {row["jurisdiction"] for row in result["records"]},
            {name for code, name, kind, _ in app.JURISDICTIONS if code.startswith("IN-MH-") and kind == "district"} - {"Mumbai City", "Mumbai Suburban"},
        )
        self.assertTrue(all(row["amount_unit"] == "source_unit_not_stated" for row in result["records"]))
        self.assertTrue(all(row["department_name_original"] is None for row in result["records"]))
        self.assertTrue(all(row["sector_name_original"] is None for row in result["records"]))
        self.assertTrue(all(row["sector_category_slug"] is None for row in result["records"]))
        self.assertTrue(all(row["source"].startswith("https://beams.mahakosh.gov.in/") for row in result["records"]))
        self.assertIn("does not state the numeric unit", result["source_note"])

    def test_map_aliases_and_unreported_districts_do_not_invent_data(self):
        ahilyanagar = app.query_budget_records("2026-27", district="Ahmednagar")
        mumbai = app.query_budget_records("2026-27", district="Mumbai Suburban")

        self.assertEqual({row["jurisdiction"] for row in ahilyanagar["records"]}, {"Ahilyanagar"})
        self.assertEqual(ahilyanagar["figure_count"], 2)
        self.assertEqual(mumbai["figure_count"], 0)
        self.assertIn("no values are inferred", mumbai["message"])

    def test_other_state_districts_have_explicitly_illustrative_demo_rows(self):
        result = app.query_budget_records(
            "2026-27", jurisdiction_code="IN-BR", district="Patna"
        )

        self.assertEqual(result["figure_count"], 8)
        self.assertEqual(
            {row["department_name_original"] for row in result["records"]},
            {"Education", "Agriculture", "Healthcare", "Transport"},
        )
        expected_sectors = {
            "Education": ("Education", "education"),
            "Agriculture": ("Crop Husbandry", "agriculture"),
            "Healthcare": ("Public Health", "healthcare"),
            "Transport": ("Roads and Bridges", "roadways_transport"),
        }
        self.assertEqual(
            {
                row["department_name_original"]: (
                    row["sector_name_original"],
                    row["sector_category_slug"],
                )
                for row in result["records"]
            },
            expected_sectors,
        )
        self.assertEqual(
            {row["budget_stage"] for row in result["records"]},
            {"funds_received", "actual"},
        )
        self.assertTrue(
            all(
                row["source"] == "Illustrative demo dataset (synthetic, not official)"
                for row in result["records"]
            )
        )
        received = [
            row for row in result["records"]
            if row["budget_stage"] == "funds_received"
        ]
        expenditure = [
            row for row in result["records"]
            if row["budget_stage"] == "actual"
        ]
        self.assertEqual(sum(row["amount"] for row in received), 9.34)
        self.assertEqual(sum(row["amount"] for row in expenditure), 8.18)
        for allocations in (received, expenditure):
            self.assertLessEqual(
                max(row["amount"] for row in allocations)
                - min(row["amount"] for row in allocations),
                0.01,
            )
        self.assertIn("not official government data", result["source_note"])
        self.assertIn("illustrative department and sector labels", result["source_note"])
        self.assertEqual(result["illustrative_figure_count"], 8)

    def write_import_csv(self, rows, path=None):
        path = path or Path(self.temp_directory.name) / "provided-budget.csv"
        columns = [
            "jurisdiction_code", "jurisdiction_name", "fiscal_year",
            "budget_stage", "department_name_original", "sector_name_original",
            "sector_category_slug", "amount", "spent_amount", "currency_code", "amount_unit",
            "source_page_or_table", "source_locator", "correction_notes",
        ]
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def import_row(self, **overrides):
        row = {
            "jurisdiction_code": "IN-BR-D-PATNA",
            "jurisdiction_name": "Patna",
            "fiscal_year": "2026-27",
            "budget_stage": "funds_received",
            "department_name_original": "Education Department",
            "sector_name_original": "Education",
            "sector_category_slug": "education",
            "amount": "123.45",
            "spent_amount": "45.67",
            "currency_code": "INR",
            "amount_unit": "crore",
            "source_page_or_table": "Table 4, page 12",
            "source_locator": "Row: Patna / Education",
            "correction_notes": "Corrected transcription against source table.",
        }
        row.update(overrides)
        return row

    def test_import_requires_staging_independent_review_and_explicit_publish(self):
        source_file = self.write_import_csv([self.import_row()])
        batch_id, count = import_budget.stage_import(
            source_file,
            source_document="User-supplied budget workbook.xlsx",
            source_url="https://example.test/budget.xlsx",
            actor=self.data_entry,
            corrections="Transcribed from supplied workbook.",
        )
        self.assertEqual(count, 1)
        staged = app.query_budget_records(
            "2026-27", jurisdiction_code="IN-BR", district="Patna"
        )
        self.assertFalse(
            any(record["source"].startswith("User-provided dataset") for record in staged["records"])
        )
        same_person_batch, _ = import_budget.stage_import(
            self.write_import_csv([self.import_row(amount="124.56")]),
            source_document="same-person-test.csv",
            actor=self.admin,
        )
        with self.assertRaisesRegex(ValueError, "different from the person"):
            import_budget.review_import(
                same_person_batch, actor=self.admin, review_notes="Checked source."
            )
        import_budget.review_import(
            batch_id, actor=self.reviewer, review_notes="Compared values to the cited table."
        )
        with self.assertRaisesRegex(ValueError, "exact batch ID"):
            import_budget.publish_import(
                batch_id, actor=self.reviewer, confirm_batch="wrong"
            )

        with self.assertRaisesRegex(PermissionError, "not permitted to publish"):
            import_budget.publish_import(
                batch_id, actor=self.reviewer, confirm_batch=batch_id
            )
        with self.assertRaisesRegex(PermissionError, "not permitted to publish"):
            import_budget.publish_import(
                batch_id, actor=self.admin, confirm_batch=batch_id
            )
        self.assertEqual(
            import_budget.publish_import(
                batch_id, actor=self.central_admin, confirm_batch=batch_id
            ),
            1,
        )
        published = app.query_budget_records(
            "2026-27", jurisdiction_code="IN-BR", district="Patna",
            budget_stage="funds_received",
        )
        self.assertEqual(published["figure_count"], 1)
        record = published["records"][0]
        self.assertEqual(record["amount"], 123.45)
        self.assertEqual(record["spent_amount"], 45.67)
        self.assertEqual(record["source_document"], "User-supplied budget workbook.xlsx")
        self.assertEqual(record["source_url"], "https://example.test/budget.xlsx")
        self.assertEqual(record["source_page_or_table"], "Table 4, page 12")
        self.assertTrue(record["imported_at"])
        self.assertEqual(record["correction_notes"], "Corrected transcription against source table.")
        self.assertEqual(record["reviewed_by"], "reviewer")
        self.assertIn("does not independently verify", published["source_note"])
        self.assertTrue(published["totals_by_stage"][0]["is_user_provided"])
        coverage = app.query_coverage()
        self.assertEqual(coverage["user_provided_figure_count"], 1)
        self.assertEqual(coverage["districts_with_user_provided_data"], 1)
        bihar = next(
            entry for entry in coverage["jurisdictions"][0]["children"]
            if entry["code"] == "IN-BR"
        )
        patna = next(entry for entry in bihar["children"] if entry["name"] == "Patna")
        self.assertEqual(patna["data_status"], "user_provided_data")

    def test_admin_roles_gate_import_actions_and_authentication_is_hashed(self):
        principal = import_budget.authenticate_user("data-entry", "test-password-123")
        self.assertEqual(principal, self.data_entry)
        with self.assertRaises(PermissionError):
            import_budget.authenticate_user("data-entry", "wrong-password")
        with self.assertRaises(PermissionError):
            import_budget.list_users(actor=self.data_entry)
        with self.assertRaises(PermissionError):
            import_budget.stage_import(
                self.write_import_csv([self.import_row()]),
                source_document="budget.csv",
                actor=self.reviewer,
            )
        with app.database_connection() as connection:
            failed_login = connection.execute(
                """
                SELECT action FROM audit_events
                WHERE action = 'admin_authentication_failed'
                """
            ).fetchone()
        self.assertIsNotNone(failed_login)
        self.assertEqual(
            {user["username"] for user in import_budget.list_users(actor=self.admin)},
            {"data-entry", "reviewer", "admin-user", "central-admin-user"},
        )

    def test_demo_admin_logins_are_seeded_for_each_state_and_scoped(self):
        with patch.object(app, "WEB_PASSWORD_ITERATIONS", 100):
            with app.database_connection() as connection:
                app.seed_demo_web_accounts(connection)
        central_admin = app.authenticate_web_user("centraladminUN", "india@")
        maharashtra = app.authenticate_web_user("stadminmh", "india@")
        bihar = app.authenticate_web_user("stadminbr", "india@")
        self.assertEqual(central_admin["role"], "central_admin")
        self.assertEqual(central_admin["jurisdiction_code"], "IN-UNION")
        self.assertEqual(maharashtra["role"], "state_auditor")
        self.assertEqual(maharashtra["jurisdiction_code"], "IN-MH")
        self.assertEqual(bihar["jurisdiction_code"], "IN-BR")
        self.assertTrue(app.jurisdiction_is_in_scope("IN-MH-PUNE", "IN-MH"))
        self.assertFalse(app.jurisdiction_is_in_scope("IN-BR", "IN-MH"))
        auditor = import_budget.authenticate_user("stadminmh", "india@")
        self.assertEqual(auditor.jurisdiction_code, "IN-MH")
        out_of_scope_batch, _ = import_budget.stage_import(
            self.write_import_csv([self.import_row(jurisdiction_code="IN-BR-D-PATNA")]),
            source_document="out-of-scope.csv",
            actor=self.data_entry,
        )
        with self.assertRaises(PermissionError):
            import_budget.stage_import(
                self.write_import_csv([self.import_row(jurisdiction_code="IN-BR-D-PATNA")]),
                source_document="out-of-scope.csv",
                actor=auditor,
            )
        with self.assertRaises(PermissionError):
            import_budget.correct_import_row(
                out_of_scope_batch,
                2,
                changes={"amount": "999"},
                correction_note="Out-of-scope correction attempt.",
                actor=auditor,
            )

    def test_sample_budget_import_csv_matches_the_import_schema(self):
        sample_path = Path(app.APP_ROOT) / "sample-budget-import.csv"
        figures, _ = import_budget.read_import_csv(sample_path)

        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0]["jurisdiction_code"], "IN-MH")
        self.assertEqual(figures[0]["spent_amount"], "0.60")
        self.assertIn("SAMPLE ONLY", figures[0]["correction_notes"])

    def test_import_accepts_legacy_csv_without_optional_spent_amount(self):
        legacy_path = Path(self.temp_directory.name) / "legacy-budget.csv"
        row = self.import_row()
        row.pop("spent_amount")
        columns = [
            "jurisdiction_code", "jurisdiction_name", "fiscal_year",
            "budget_stage", "department_name_original", "sector_name_original",
            "sector_category_slug", "amount", "currency_code", "amount_unit",
            "source_page_or_table", "source_locator", "correction_notes",
        ]
        with legacy_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerow(row)

        figures, _ = import_budget.read_import_csv(legacy_path)

        self.assertIsNone(figures[0]["spent_amount"])

    def test_citizen_registration_and_single_use_server_captcha(self):
        captcha_id = app.issue_captcha()
        image = app.captcha_svg(captcha_id)
        self.assertTrue(image and image.startswith("<svg"))
        with app.SESSION_LOCK:
            expected_answer = app.CAPTCHA_CHALLENGES[captcha_id][0]
        self.assertTrue(app.consume_captcha(captcha_id, expected_answer.lower()))
        self.assertFalse(app.consume_captcha(captcha_id, expected_answer))
        citizen = app.register_citizen("Budget.User@example.test", "citizen-password")
        self.assertEqual(citizen["role"], "citizen")
        self.assertEqual(citizen["username"], "budget.user@example.test")
        self.assertEqual(
            app.authenticate_web_user("budget.user@example.test", "citizen-password")["role"],
            "citizen",
        )
        with self.assertRaisesRegex(ValueError, "already exists"):
            app.register_citizen("budget.user@example.test", "citizen-password")
        with self.assertRaisesRegex(ValueError, "8 and 128"):
            app.register_citizen("another@example.test", "short")

    def test_staged_row_corrections_are_validated_and_audited(self):
        batch_id, _ = import_budget.stage_import(
            self.write_import_csv([self.import_row()]),
            source_document="source.csv",
            actor=self.data_entry,
        )
        import_budget.correct_import_row(
            batch_id,
            2,
            changes={"amount": "234.56", "spent_amount": "12.34"},
            correction_note="Corrected transcription against the cited table.",
            actor=self.data_entry,
        )
        staged = import_budget.get_batch(batch_id)
        self.assertEqual(staged["status"], "staged")
        with app.database_connection() as connection:
            row = connection.execute(
                "SELECT amount, spent_amount, correction_notes FROM staged_budget_figures WHERE import_batch_id = ?",
                (batch_id,),
            ).fetchone()
            correction_event = connection.execute(
                """
                SELECT actor, action, details_json FROM audit_events
                WHERE entity_id = ?
                """,
                (f"{batch_id}:2",),
            ).fetchone()
        self.assertEqual(float(row["amount"]), 234.56)
        self.assertEqual(float(row["spent_amount"]), 12.34)
        self.assertIn("Corrected transcription", row["correction_notes"])
        import_budget.correct_import_row(
            batch_id,
            2,
            changes={"spent_amount": ""},
            correction_note="The source does not provide a spent amount.",
            actor=self.data_entry,
        )
        with app.database_connection() as connection:
            row = connection.execute(
                "SELECT spent_amount FROM staged_budget_figures WHERE import_batch_id = ?",
                (batch_id,),
            ).fetchone()
        self.assertIsNone(row["spent_amount"])
        self.assertEqual(correction_event["actor"], "data-entry")
        self.assertEqual(correction_event["action"], "budget_staged_row_corrected")
        with self.assertRaisesRegex(ValueError, "finite non-negative"):
            import_budget.correct_import_row(
                batch_id,
                2,
                changes={"amount": "-1"},
                correction_note="Bad value",
                actor=self.data_entry,
            )

    def test_audit_log_is_hash_chained_and_immutable(self):
        with app.database_connection() as connection:
            app.append_audit_event(
                connection,
                actor="test",
                action="test_event",
                entity_type="test",
                entity_id="1",
                details={"value": 1},
            )
        self.assertTrue(app.verify_audit_log()["valid"])
        with app.database_connection() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("UPDATE audit_events SET action = 'tampered' WHERE id = 1")
        self.assertTrue(app.verify_audit_log()["valid"])

    def test_database_backup_is_integrity_checked_and_restorable(self):
        backup_path = Path(self.temp_directory.name) / "backups" / "manual.sqlite3"
        result = app.create_database_backup(backup_path, actor="admin-user")
        self.assertEqual(result, backup_path)
        connection = sqlite3.connect(backup_path)
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertGreater(
                connection.execute("SELECT COUNT(*) FROM budget_figures").fetchone()[0],
                0,
            )
        finally:
            connection.close()
        with patch.object(app, "DB_PATH", backup_path):
            self.assertTrue(app.verify_audit_log()["valid"])

    def test_admin_can_manage_roles_and_disable_accounts(self):
        import_budget.create_admin_user(
            "new-reviewer",
            "a-long-example-password",
            role="data_entry",
            actor=self.admin,
        )
        import_budget.set_user_role("new-reviewer", "reviewer", actor=self.admin)
        self.assertEqual(
            import_budget.authenticate_user("new-reviewer", "a-long-example-password").role,
            "reviewer",
        )
        import_budget.disable_user("new-reviewer", actor=self.admin)
        with self.assertRaises(PermissionError):
            import_budget.authenticate_user("new-reviewer", "a-long-example-password")
        with self.assertRaisesRegex(ValueError, "cannot disable their own"):
            import_budget.disable_user("admin-user", actor=self.admin)
        with self.assertRaisesRegex(ValueError, "last active admin"):
            import_budget.set_user_role("admin-user", "reviewer", actor=self.admin)

    def test_import_rejects_mismatched_names_negative_amounts_and_duplicate_rows(self):
        wrong_name = self.write_import_csv(
            [self.import_row(jurisdiction_name="Gaya")],
            Path(self.temp_directory.name) / "wrong-name.csv",
        )
        with self.assertRaisesRegex(ValueError, "name/code mismatch"):
            import_budget.read_import_csv(wrong_name)

        negative = self.write_import_csv(
            [self.import_row(amount="-1")],
            Path(self.temp_directory.name) / "negative.csv",
        )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            import_budget.read_import_csv(negative)

        negative_spent = self.write_import_csv(
            [self.import_row(spent_amount="-1")],
            Path(self.temp_directory.name) / "negative-spent.csv",
        )
        with self.assertRaisesRegex(ValueError, "Spent amount must be finite and non-negative"):
            import_budget.read_import_csv(negative_spent)

        duplicate = self.write_import_csv(
            [self.import_row(), self.import_row()],
            Path(self.temp_directory.name) / "duplicate.csv",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate figure"):
            import_budget.read_import_csv(duplicate)

        unsupported_year = self.write_import_csv(
            [self.import_row(fiscal_year="2027-28")],
            Path(self.temp_directory.name) / "unsupported-year.csv",
        )
        with self.assertRaisesRegex(ValueError, "Unsupported fiscal_year"):
            import_budget.read_import_csv(unsupported_year)

    def test_migration_preserves_existing_normalized_budget_figures(self):
        migration_path = Path(self.temp_directory.name) / "legacy.db"
        with patch.object(app, "DB_PATH", migration_path):
            connection = sqlite3.connect(migration_path)
            try:
                with connection:
                    connection.execute(
                        """
                        CREATE TABLE budget_figures (
                            id INTEGER PRIMARY KEY, jurisdiction_code TEXT,
                            fiscal_year TEXT, budget_stage TEXT,
                            department_name_original TEXT, sector_name_original TEXT,
                            sector_category_slug TEXT, amount NUMERIC,
                            currency_code TEXT, amount_unit TEXT, source TEXT,
                            source_locator TEXT
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO budget_figures VALUES (
                            1, 'IN-MH', '2025-26', 'budget_estimate', 'Legacy department',
                            'Education', 'education', 123, 'INR', 'crore',
                            'Legacy source', 'Legacy row 1'
                        )
                        """
                    )
            finally:
                connection.close()
            app.initialize_database()
            migrated = app.query_budget_records(
                "2025-26", jurisdiction_code="IN-MH", search="Legacy department"
            )

        self.assertEqual(migrated["figure_count"], 1)
        self.assertEqual(migrated["records"][0]["amount"], 123)
        self.assertEqual(migrated["records"][0]["source_locator"], "Legacy row 1")

    def test_rejects_unsupported_filter_values(self):
        with self.assertRaises(ValueError):
            app.query_budget_records("2027-28")
        with self.assertRaises(ValueError):
            app.query_budget_records("2025-26", budget_stage="forecast")
        with self.assertRaises(ValueError):
            app.query_budget_records("2025-26", jurisdiction_code="unknown")


if __name__ == "__main__":
    unittest.main()
