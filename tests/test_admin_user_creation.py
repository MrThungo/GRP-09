import os
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models import AuditLog, User


class AdminUserCreationTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": "sqlite:///:memory:",
                "SECRET_KEY": "admin-user-creation-test-key",
                "DEFAULT_USER_PASSWORD": "ValidPass1!",
                "GREENAPI_ENABLED": "false",
                "SUPER_ADMIN_EMAIL": "superadmin@nmbhlab.com",
            },
            clear=False,
        )
        self.environment.start()
        self.avatar_directory = tempfile.TemporaryDirectory()
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            SERVER_NAME="localhost",
            AVATAR_UPLOAD_DIR=self.avatar_directory.name,
        )
        self.client = self.app.test_client()

        with self.app.app_context():
            self.admin_id = User.query.filter_by(email="admin@nmbhlab.com").one().id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.avatar_directory.cleanup()
        self.environment.stop()

    def login_as_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = self.admin_id
            session["_fresh"] = True

    def test_normal_admin_can_create_user_and_assign_role(self):
        self.login_as_admin()
        page = self.client.get("/admin/users/new")
        self.assertEqual(page.status_code, 200)
        page_html = page.get_data(as_text=True)
        self.assertIn("Create user and assign role", page_html)
        self.assertIn('option value="lab_manager"', page_html)
        self.assertNotIn('option value="super_admin"', page_html)
        self.assertIn("data-user-role-form", page_html)
        self.assertIn('data-role-visible-for="doctor"', page_html)
        self.assertIn('data-role-visible-for="patient"', page_html)
        self.assertNotIn("data-technician-test-filter", page_html)
        self.assertNotIn("Assigned test types", page_html)
        self.assertNotIn("the configured email or WhatsApp service", page_html)
        self.assertNotIn(
            "Used in the patient record when the Patient role is selected.",
            page_html,
        )

        with (
            patch("app.blueprints.admin.send_email", return_value=True),
            patch(
                "app.blueprints.admin.send_account_welcome_whatsapp",
                return_value=False,
            ),
        ):
            response = self.client.post(
                "/admin/users/new",
                data={
                    "full_name": "New",
                    "surname": "Manager",
                    "email": "new.manager@example.com",
                    "phone": "0712345678",
                    "employee_number": "EMP-NEW-001",
                    "role": "lab_manager",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Lab Manager account created",
            response.get_data(as_text=True),
        )
        with self.app.app_context():
            user = User.query.filter_by(email="new.manager@example.com").one()
            self.assertTrue(user.has_role("lab_manager"))
            self.assertTrue(user.must_change_password)
            self.assertTrue(user.temp_password)
            self.assertTrue(user.check_password(user.temp_password))
            self.assertEqual(
                AuditLog.query.filter_by(
                    action="create_user",
                    entity_id=user.id,
                ).count(),
                1,
            )

    def test_admin_cannot_assign_super_admin_during_creation(self):
        self.login_as_admin()
        response = self.client.post(
            "/admin/users/new",
            data={
                "full_name": "Invalid",
                "surname": "Promotion",
                "email": "invalid.promotion@example.com",
                "role": "super_admin",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Select a valid role.", response.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNone(
                User.query.filter_by(email="invalid.promotion@example.com").first()
            )

    def test_doctor_requires_doctor_registration_fields(self):
        self.login_as_admin()
        response = self.client.post(
            "/admin/users/new",
            data={
                "full_name": "New",
                "surname": "Doctor",
                "email": "new.doctor@example.com",
                "phone": "0712345678",
                "employee_number": "DOC-NEW-001",
                "role": "doctor",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "HPCSA number is required for doctors.",
            response.get_data(as_text=True),
        )
        with self.app.app_context():
            self.assertIsNone(
                User.query.filter_by(email="new.doctor@example.com").first()
            )

    def test_admin_creates_technician_without_test_type_assignment(self):
        self.login_as_admin()
        with (
            patch("app.blueprints.admin.send_email", return_value=True),
            patch(
                "app.blueprints.admin.send_account_welcome_whatsapp",
                return_value=True,
            ),
        ):
            response = self.client.post(
                "/admin/users/new",
                data={
                    "full_name": "New",
                    "surname": "Technician",
                    "email": "new.technician@example.com",
                    "phone": "0712345678",
                    "employee_number": "TECH-NEW-001",
                    "hpcsa_number": "NOT-A-TECHNICIAN-FIELD",
                    "address": "Not a technician field",
                    "role": "lab_technician",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Lab Technician account created",
            response.get_data(as_text=True),
        )
        with self.app.app_context():
            technician = User.query.filter_by(
                email="new.technician@example.com"
            ).one()
            self.assertTrue(technician.has_role("lab_technician"))
            self.assertIsNone(technician.hpcsa_number)
            self.assertIsNone(technician.patient_record)
            self.assertEqual(technician.technician_assignments, [])

    def test_patient_requires_patient_information(self):
        self.login_as_admin()
        response = self.client.post(
            "/admin/users/new",
            data={
                "full_name": "New",
                "surname": "Patient",
                "email": "new.patient@example.com",
                "phone": "0712345678",
                "role": "patient",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(
            "South African ID number is required for patients.",
            html,
        )
        self.assertIn("Home address is required for patients.", html)


if __name__ == "__main__":
    unittest.main()
