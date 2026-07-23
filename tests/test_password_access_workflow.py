import os
import re
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models import AuditLog, User, UserRole


class PasswordAccessWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": "sqlite:///:memory:",
                "SECRET_KEY": "password-access-test-key",
                "DEFAULT_USER_PASSWORD": "ValidPass1!",
                "GREENAPI_ENABLED": "false",
                "SUPER_ADMIN_EMAIL": "superadmin@nmbhlab.com",
            },
            clear=False,
        )
        self.environment.start()
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            SERVER_NAME="localhost",
            _NEAR_LIMIT_REMINDER_LAST_CHECK=datetime.now(),
            _RECORDING_RETENTION_LAST_CHECK=datetime.now(),
        )
        self.client = self.app.test_client()

        with self.app.app_context():
            super_admin = User.query.filter_by(email="superadmin@nmbhlab.com").one()
            normal_admin = User.query.filter_by(email="admin@nmbhlab.com").one()
            self.assertTrue(super_admin.has_role("admin"))
            self.assertTrue(super_admin.has_role("super_admin"))
            self.assertTrue(normal_admin.has_role("admin"))
            self.assertFalse(normal_admin.has_role("super_admin"))
            target = User.query.filter_by(email="patient@nmbhlab.com").one()
            target.set_password("OldPatient1!")
            target.must_change_password = False
            target.temp_password = None

            requester = User(
                email="requesting-admin@example.test",
                full_name="Requesting Admin",
                must_change_password=False,
            )
            requester.set_password("Requester1!")
            db.session.add(requester)
            db.session.flush()
            db.session.add(UserRole(user_id=requester.id, role="admin"))
            db.session.commit()

            self.super_admin_id = super_admin.id
            self.requester_id = requester.id
            self.target_id = target.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.environment.stop()

    def login_as(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = user_id
            session["_fresh"] = True

    def target_password_matches(self, password):
        with self.app.app_context():
            return db.session.get(User, self.target_id).check_password(password)

    def test_dual_control_request_approval_reveal_and_forced_change(self):
        self.login_as(self.requester_id)
        user_list = self.client.get("/admin/users")
        user_detail = self.client.get(f"/admin/users/{self.target_id}")
        self.assertEqual(user_list.status_code, 200)
        self.assertEqual(user_detail.status_code, 200)
        user_detail_html = user_detail.get_data(as_text=True)
        self.assertIn("2F-authentication", user_detail_html)
        self.assertNotIn("Two-person approval", user_detail_html)

        denied_request = self.client.post(
            f"/admin/users/{self.target_id}/password-access/request",
            data={"current_password": "wrong"},
        )
        self.assertEqual(denied_request.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                AuditLog.query.filter_by(action="password_access_requested").count(),
                0,
            )

        requested = self.client.post(
            f"/admin/users/{self.target_id}/password-access/request",
            data={"current_password": "Requester1!"},
        )
        self.assertEqual(requested.status_code, 302)
        self.assertTrue(self.target_password_matches("OldPatient1!"))

        with self.app.app_context():
            request_entry = AuditLog.query.filter_by(
                action="password_access_requested",
                actor_id=self.requester_id,
                entity_id=self.target_id,
            ).one()
            request_id = request_entry.id

        self.assertEqual(self.client.get("/admin/password-access").status_code, 403)
        self.assertEqual(
            self.client.post(
                f"/admin/users/{self.target_id}/reset-password",
            ).status_code,
            302,
        )
        self.assertTrue(self.target_password_matches("OldPatient1!"))

        self.login_as(self.super_admin_id)
        approval_page = self.client.get("/admin/password-access")
        self.assertEqual(approval_page.status_code, 200)
        self.assertIn("Requesting Admin", approval_page.get_data(as_text=True))

        wrong_approval = self.client.post(
            f"/admin/password-access/{request_id}/approve",
            data={"current_password": "wrong"},
        )
        self.assertEqual(wrong_approval.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                AuditLog.query.filter_by(action="password_access_approved").count(),
                0,
            )

        approved = self.client.post(
            f"/admin/password-access/{request_id}/approve",
            data={"current_password": "ValidPass1!"},
        )
        self.assertEqual(approved.status_code, 302)
        self.assertTrue(self.target_password_matches("OldPatient1!"))

        self.login_as(self.requester_id)
        wrong_reveal = self.client.post(
            f"/admin/password-access/{request_id}/reveal",
            data={"current_password": "wrong"},
        )
        self.assertEqual(wrong_reveal.status_code, 302)
        self.assertTrue(self.target_password_matches("OldPatient1!"))

        with patch("app.blueprints.admin.send_email", return_value=True):
            revealed = self.client.post(
                f"/admin/password-access/{request_id}/reveal",
                data={"current_password": "Requester1!"},
            )
        self.assertEqual(revealed.status_code, 200)
        self.assertIn("no-store", revealed.headers["Cache-Control"])
        match = re.search(
            r'id="temporary_password"[^>]*value="([^"]+)"',
            revealed.get_data(as_text=True),
        )
        self.assertIsNotNone(match)
        temporary_password = match.group(1)
        self.assertTrue(self.target_password_matches(temporary_password))
        self.assertFalse(self.target_password_matches("OldPatient1!"))

        with self.app.app_context():
            target = db.session.get(User, self.target_id)
            self.assertTrue(target.must_change_password)
            self.assertEqual(target.temp_password, temporary_password)
            for entry in AuditLog.query.all():
                self.assertNotIn(temporary_password, entry.details or "")

        with self.client.session_transaction() as session:
            session.clear()
        logged_in = self.client.post(
            "/auth/login",
            data={
                "email": "patient@nmbhlab.com",
                "password": temporary_password,
            },
            follow_redirects=True,
        )
        self.assertEqual(logged_in.status_code, 200)
        self.assertIn(
            "Change your password to continue",
            logged_in.get_data(as_text=True),
        )

        changed = self.client.post(
            "/auth/change-password",
            data={
                "current_password": temporary_password,
                "new_password": "ChangedPass2!",
                "confirm_password": "ChangedPass2!",
            },
        )
        self.assertEqual(changed.status_code, 302)
        with self.app.app_context():
            target = db.session.get(User, self.target_id)
            self.assertFalse(target.must_change_password)
            self.assertIsNone(target.temp_password)
            self.assertTrue(target.check_password("ChangedPass2!"))

        self.login_as(self.requester_id)
        consumed = self.client.post(
            f"/admin/password-access/{request_id}/reveal",
            data={"current_password": "Requester1!"},
        )
        self.assertEqual(consumed.status_code, 302)
        self.assertTrue(self.target_password_matches("ChangedPass2!"))

        privilege_escalation = self.client.post(
            f"/admin/users/{self.requester_id}/role",
            data={"role": "super_admin"},
        )
        self.assertEqual(privilege_escalation.status_code, 403)

    def test_requester_cannot_self_approve_and_expired_request_cannot_be_approved(self):
        with self.app.app_context():
            db.session.add(UserRole(user_id=self.requester_id, role="super_admin"))
            db.session.commit()

        self.login_as(self.requester_id)
        requested = self.client.post(
            f"/admin/users/{self.target_id}/password-access/request",
            data={"current_password": "Requester1!"},
        )
        self.assertEqual(requested.status_code, 302)
        with self.app.app_context():
            request_entry = AuditLog.query.filter_by(
                action="password_access_requested",
                actor_id=self.requester_id,
                entity_id=self.target_id,
            ).one()
            request_id = request_entry.id

        self_approval = self.client.post(
            f"/admin/password-access/{request_id}/approve",
            data={"current_password": "Requester1!"},
        )
        self.assertEqual(self_approval.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                AuditLog.query.filter_by(action="password_access_approved").count(),
                0,
            )
            request_entry = db.session.get(AuditLog, request_id)
            request_entry.created_at = datetime.now() - timedelta(hours=25)
            db.session.commit()

        self.login_as(self.super_admin_id)
        expired_approval = self.client.post(
            f"/admin/password-access/{request_id}/approve",
            data={"current_password": "ValidPass1!"},
        )
        self.assertEqual(expired_approval.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                AuditLog.query.filter_by(action="password_access_approved").count(),
                0,
            )
        self.assertTrue(self.target_password_matches("OldPatient1!"))


if __name__ == "__main__":
    unittest.main()
