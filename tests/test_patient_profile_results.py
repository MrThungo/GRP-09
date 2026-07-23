import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models import (
    ConsentGrant,
    Patient,
    TestCatalog,
    TestRequest,
    TestRequestItem,
    User,
)


class PatientProfileAndResultsTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                "DATABASE_URL": "sqlite:///:memory:",
                "SECRET_KEY": "patient-profile-results-test-key",
                "DEFAULT_USER_PASSWORD": "ValidPass1!",
                "GREENAPI_ENABLED": "false",
                "SUPER_ADMIN_EMAIL": "superadmin@nmbhlab.com",
            },
            clear=False,
        )
        self.environment.start()
        self.app = create_app()
        self.app.config.update(TESTING=True, SERVER_NAME="localhost")
        self.client = self.app.test_client()

        with self.app.app_context():
            user = User.query.filter_by(email="patient@nmbhlab.com").one()
            patient = Patient.query.filter_by(profile_id=user.id).one()
            manager = User.query.filter_by(email="manager@nmbhlab.com").one()
            doctor = User.query.filter_by(email="doctor@nmbhlab.com").one()
            test = TestCatalog.query.order_by(TestCatalog.code).first()
            now = datetime.now()
            for index in range(12):
                result_request = TestRequest(
                    request_number=f"PAGE-{index:02d}",
                    patient_id=patient.id,
                    status="released",
                    released_at=now - timedelta(minutes=index),
                )
                db.session.add(result_request)
                db.session.flush()
                db.session.add(
                    TestRequestItem(
                        request_id=result_request.id,
                        test_id=test.id,
                        status="verified",
                        result_text=f"Result {index}",
                    )
                )
            db.session.commit()
            self.user_id = user.id
            self.patient_id = patient.id
            self.manager_id = manager.id
            self.doctor_id = doctor.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        self.environment.stop()

    def login_as_patient(self):
        self.login_as(self.user_id)

    def login_as(self, user_id):
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = user_id
            session["_fresh"] = True

    def test_my_results_are_paginated_ten_per_page(self):
        self.login_as_patient()

        first_page = self.client.get("/patient/results?page=1")
        self.assertEqual(first_page.status_code, 200)
        first_html = first_page.get_data(as_text=True)
        self.assertIn("Showing 1-10 of 12", first_html)
        self.assertIn("PAGE-00", first_html)
        self.assertIn("PAGE-09", first_html)
        self.assertNotIn("PAGE-10", first_html)
        self.assertIn("My results pagination", first_html)

        second_page = self.client.get("/patient/results?page=2")
        self.assertEqual(second_page.status_code, 200)
        second_html = second_page.get_data(as_text=True)
        self.assertIn("Showing 11-12 of 12", second_html)
        self.assertIn("PAGE-10", second_html)
        self.assertIn("PAGE-11", second_html)
        self.assertNotIn("PAGE-00", second_html)

    def test_patient_can_update_first_name_and_surname(self):
        self.login_as_patient()

        response = self.client.post(
            "/patient/profile",
            data={
                "full_name": "Lerato",
                "surname": "Mokoena",
                "title": "",
                "gender": "",
                "id_number": "",
                "date_of_birth": "",
                "phone": "0712345678",
                "address": "12 Main Road",
                "blood_type": "O+",
            },
        )
        self.assertEqual(response.status_code, 302)

        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            patient = db.session.get(Patient, self.patient_id)
            self.assertEqual(user.full_name, "Lerato")
            self.assertEqual(user.surname, "Mokoena")
            self.assertEqual(patient.full_name, "Lerato Mokoena")
            self.assertEqual(patient.surname, "Mokoena")

    def test_consent_history_cleans_internal_and_nb_notes(self):
        with self.app.app_context():
            db.session.add_all(
                [
                    ConsentGrant(
                        patient_id=self.patient_id,
                        doctor_id=self.doctor_id,
                        note="NB: Please review this result.",
                    ),
                    ConsentGrant(
                        patient_id=self.patient_id,
                        doctor_id=self.doctor_id,
                        note="Auto-granted via access request internal-id",
                    ),
                ]
            )
            db.session.commit()

        self.login_as_patient()
        response = self.client.get("/patient/consent")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Please review this result.", html)
        self.assertNotIn("NB:", html)
        self.assertNotIn("Auto-granted via access request", html)

    def test_add_technician_test_types_are_filterable(self):
        self.login_as(self.manager_id)
        response = self.client.get("/manager/technicians/new")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("data-technician-test-filter", html)
        self.assertIn("Filter by category, code, name or sample type", html)
        self.assertIn("data-technician-test-option", html)


if __name__ == "__main__":
    unittest.main()
