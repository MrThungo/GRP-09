import html
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from app import create_app
from app.extensions import db
from app.landing_team import landing_team_picture_filename
from app.models import AuditLog, User, UserRole


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class UiSecurityPresentationTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": "sqlite:///:memory:",
                "SECRET_KEY": "ui-security-presentation-test-key",
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
            super_admin = User.query.filter_by(
                email="superadmin@nmbhlab.com"
            ).one()
            normal_admin = User.query.filter_by(email="admin@nmbhlab.com").one()
            patient = User.query.filter_by(email="patient@nmbhlab.com").one()
            self.super_admin_id = super_admin.id
            self.normal_admin_id = normal_admin.id
            self.patient_id = patient.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.avatar_directory.cleanup()
        self.environment.stop()

    def login_as(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = user_id
            session["_fresh"] = True

    def test_password_access_copy_is_simplified(self):
        self.login_as(self.normal_admin_id)
        detail = self.client.get(f"/admin/users/{self.patient_id}")
        self.assertEqual(detail.status_code, 200)
        detail_html = detail.get_data(as_text=True)
        self.assertIn("2F-authentication", detail_html)
        self.assertNotIn("Two-person approval", detail_html)
        self.assertNotIn(
            "Existing passwords are securely hashed and cannot be decrypted",
            detail_html,
        )

        self.login_as(self.super_admin_id)
        approvals = self.client.get("/admin/password-access")
        self.assertEqual(approvals.status_code, 200)
        approvals_html = approvals.get_data(as_text=True)
        self.assertNotIn(
            "Review administrator requests for temporary user passwords",
            approvals_html,
        )
        self.assertNotIn(
            "Your own password is required for every decision",
            approvals_html,
        )

    def test_message_refresh_timestamp_is_not_displayed(self):
        javascript_path = os.path.join(
            PROJECT_ROOT,
            "app",
            "static",
            "js",
            "app.js",
        )
        with open(javascript_path, encoding="utf-8") as javascript:
            self.assertNotIn("Updated just now", javascript.read())

    def test_audit_details_render_as_labels_instead_of_json(self):
        with self.app.app_context():
            db.session.add(
                AuditLog(
                    actor_id=self.super_admin_id,
                    action="assign_role",
                    entity_type="user",
                    entity_id=self.patient_id,
                    details=json.dumps({"role": "patient"}),
                )
            )
            db.session.commit()

        self.login_as(self.super_admin_id)
        response = self.client.get("/admin/audit")
        self.assertEqual(response.status_code, 200)
        page = html.unescape(response.get_data(as_text=True))
        self.assertIn("Role:", page)
        self.assertIn("Patient", page)
        self.assertNotIn('{"role": "patient"}', page)

    def test_only_super_admin_can_upload_landing_team_pictures(self):
        self.login_as(self.normal_admin_id)
        self.assertEqual(self.client.get("/admin/landing-team").status_code, 403)

        self.login_as(self.super_admin_id)
        page = self.client.get("/admin/landing-team")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Landing-page team pictures", page.get_data(as_text=True))

        picture = io.BytesIO()
        Image.new("RGB", (40, 40), "#0ea5e9").save(picture, format="PNG")
        picture.seek(0)
        response = self.client.post(
            "/admin/landing-team",
            data={
                "student_number": "224497847",
                "picture": (picture, "team-picture.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)

        expected_filename = landing_team_picture_filename("224497847")
        expected_path = os.path.join(self.avatar_directory.name, expected_filename)
        self.assertTrue(os.path.isfile(expected_path))

        with self.client.session_transaction() as session:
            session.clear()

        landing = self.client.get("/")
        self.assertEqual(landing.status_code, 200)
        landing_html = landing.get_data(as_text=True)
        self.assertIn(f"avatars/{expected_filename}", landing_html)
        self.assertIn("body-copy", landing_html)
        self.assertNotIn("Manage team pictures", landing_html)

        with self.app.app_context():
            self.assertEqual(
                AuditLog.query.filter_by(
                    action="update_landing_team_picture"
                ).count(),
                1,
            )


if __name__ == "__main__":
    unittest.main()
