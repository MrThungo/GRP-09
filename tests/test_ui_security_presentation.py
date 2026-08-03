import html
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app import create_app
from app.extensions import db
from app.landing_team import TEAM_MEMBER_SPECS, landing_team_picture_filename
from app.models import AuditLog, Patient, Sample, TestRequest, User, UserRole


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
            ADMIN_CONTACT_EMAIL="lab-admin@example.com",
        )
        self.client = self.app.test_client()

        with self.app.app_context():
            super_admin = User.query.filter_by(
                email="superadmin@nmbhlab.com"
            ).one()
            normal_admin = User.query.filter_by(email="admin@nmbhlab.com").one()
            patient = User.query.filter_by(email="patient@nmbhlab.com").one()
            doctor = User.query.filter_by(email="doctor@nmbhlab.com").one()
            patient_record = Patient.query.filter_by(profile_id=patient.id).one()
            barcode_request = TestRequest(
                request_number="REQ-BARCODE-UI-TEST",
                patient_id=patient_record.id,
                doctor_id=doctor.id,
                status="submitted",
                priority="routine",
            )
            barcode_sample = Sample(
                request=barcode_request,
                barcode="REQ-BARCODE-UI-TEST-EDTA-01",
                sample_type="EDTA Blood",
            )
            db.session.add_all([barcode_request, barcode_sample])
            db.session.commit()
            self.super_admin_id = super_admin.id
            self.normal_admin_id = normal_admin.id
            self.patient_id = patient.id
            self.doctor_id = doctor.id
            self.sample_id = barcode_sample.id

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

    def test_landing_uses_project_content_team_cards_and_sender_contact(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        page = html.unescape(response.get_data(as_text=True))
        self.assertIn('id="care-journey"', page)
        self.assertIn("Connected diagnostic journey", page)
        self.assertIn('id="connected-care"', page)
        self.assertIn("Live sample visibility", page)
        self.assertIn("landing-team-picture", page)
        self.assertIn('data-landing-menu-toggle', page)
        self.assertIn('id="landing-mobile-menu"', page)
        self.assertEqual(page.count('class="landing-team-linkedin"'), 4)
        self.assertIn(
            "https://www.linkedin.com/in/anam-thembani-760488351?trk=contact-info",
            page,
        )
        self.assertIn(
            "https://www.linkedin.com/in/papama-xuza-70846b2b3"
            "?utm_source=share&utm_campaign=share_via"
            "&utm_content=profile&utm_medium=android_app",
            page,
        )
        self.assertIn(
            "https://www.linkedin.com/in/ndumiso-thungo-254470164/",
            page,
        )
        self.assertIn(
            "https://linkedin.com/in/nomhle-mncina-b192833a0",
            page,
        )
        self.assertIn("lab-admin@example.com", page)
        self.assertNotIn('href="#security"', page)
        self.assertNotIn('href="#workflow"', page)

    def test_landing_team_pictures_use_one_fluid_aspect_ratio(self):
        stylesheet_path = os.path.join(
            PROJECT_ROOT,
            "app",
            "static",
            "css",
            "app.css",
        )
        with open(stylesheet_path, encoding="utf-8") as stylesheet:
            source = stylesheet.read()
        self.assertIn("aspect-ratio: 2.15 / 1;", source)
        self.assertIn("object-fit: var(--landing-team-fit, cover);", source)
        self.assertIn(
            "object-position: var(--landing-team-focus, 50% 22%);",
            source,
        )
        members = {member["name"]: member for member in TEAM_MEMBER_SPECS}
        positions = {name: member["picture_position"] for name, member in members.items()}
        self.assertEqual(positions["Papama Xuza"], "50% 10%")
        self.assertEqual(positions["Anam Thembani"], "50% 28%")
        self.assertEqual(positions["Ndumiso Thungo"], "50% 50%")
        self.assertEqual(members["Ndumiso Thungo"]["picture_fit"], "contain")
        self.assertEqual(members["Ndumiso Thungo"]["picture_scale"], "1.32")

    def test_team_details_and_admin_user_actions_are_mobile_compact(self):
        stylesheet_path = os.path.join(
            PROJECT_ROOT,
            "app",
            "static",
            "css",
            "app.css",
        )
        with open(stylesheet_path, encoding="utf-8") as stylesheet:
            source = stylesheet.read()

        self.assertIn("min-height: 4.75rem;", source)
        self.assertNotIn("min-height: 6.5rem;", source)
        self.assertIn(".admin-users-table tbody td", source)
        self.assertIn(".admin-user-role-form", source)
        self.assertIn(".admin-user-action-control", source)
        self.assertIn(".landing-mobile-menu", source)
        self.assertIn(".landing-topbar-cta", source)

        self.login_as(self.normal_admin_id)
        response = self.client.get("/admin/users")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('class="admin-users-table ', page)
        self.assertIn('data-label="Manage"', page)
        self.assertIn("admin-user-action-control", page)
        self.assertIn("admin-user-role-form", page)

    def test_release_message_does_not_tell_an_authenticated_user_to_sign_in(self):
        service_path = os.path.join(PROJECT_ROOT, "app", "services.py")
        with open(service_path, encoding="utf-8") as service_file:
            source = service_file.read()
        self.assertNotIn("Please sign in to view the report.", source)
        self.assertNotIn("Download PDF after sign-in:", source)

    def test_scannable_barcode_is_visible_to_the_sample_patient(self):
        self.login_as(self.patient_id)
        barcode = self.client.get(f"/api/samples/{self.sample_id}/barcode.svg")
        self.assertEqual(barcode.status_code, 200)
        self.assertEqual(barcode.mimetype, "image/svg+xml")
        self.assertIn(b"<svg", barcode.data)

        requests_page = self.client.get("/patient/requests")
        self.assertEqual(requests_page.status_code, 200)
        self.assertIn(
            f"/api/samples/{self.sample_id}/barcode.svg",
            requests_page.get_data(as_text=True),
        )

    def test_twilio_credentials_produce_short_lived_turn_configuration(self):
        from app.blueprints.api import _webrtc_ice_server_payload

        fake_token = SimpleNamespace(ice_servers=[
            {"url": "stun:global.stun.twilio.com:3478"},
            {
                "url": "turn:global.turn.twilio.com:3478?transport=udp",
                "username": "temporary-user",
                "credential": "temporary-credential",
            },
            {
                "url": "turns:global.turn.twilio.com:443?transport=tcp",
                "username": "temporary-user",
                "credential": "temporary-credential",
            },
        ])
        with self.app.app_context(), patch("twilio.rest.Client") as client:
            self.app.config.update(
                TWILIO_ACCOUNT_SID="AC00000000000000000000000000000000",
                TWILIO_AUTH_TOKEN="test-auth-token",
            )
            self.app.extensions.pop("webrtc_twilio_token", None)
            client.return_value.tokens.create.return_value = fake_token

            payload = _webrtc_ice_server_payload()

        self.assertTrue(payload["turnConfigured"])
        self.assertEqual(payload["relayProvider"], "twilio")
        self.assertTrue(any(
            str(url).startswith("turn:")
            for server in payload["iceServers"]
            for url in server["urls"]
        ))
        client.return_value.tokens.create.assert_called_once_with(ttl=3600)


if __name__ == "__main__":
    unittest.main()
