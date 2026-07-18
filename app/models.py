"""SQLAlchemy models."""
from __future__ import annotations
import random
import uuid
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from sqlalchemy import (
    Column, String, Boolean, DateTime, Date, Integer, Numeric, ForeignKey, Text, Index,
)
from sqlalchemy.orm import relationship

from .extensions import db


def _uuid():
    return str(uuid.uuid4())


def _room_token():
    return uuid.uuid4().hex + uuid.uuid4().hex


ROLES = ("admin", "lab_manager", "doctor", "lab_technician", "patient")
ROLE_LABELS = {
    "admin": "Administrator",
    "lab_manager": "Lab Manager",
    "doctor": "Doctor",
    "lab_technician": "Lab Technician",
    "patient": "Patient",
}
TITLE_OPTIONS = ("Mr", "Mrs", "Ms", "Miss", "Prof")
GENDER_OPTIONS = ("Female", "Male", "Other", "Prefer not to say")

REQUEST_STATUSES = (
    "submitted",
    "samples_received",
    "in_progress",
    "completed",
    "verified",
    "released",
    "cancelled",
)


PRIORITIES = ("routine", "urgent", "stat")
ITEM_STATUSES = ("submitted", "in_progress", "completed", "verified", "to_be_reviewed", "cancelled")
STOCK_MOVEMENT_TYPES = ("in", "out", "adjustment")
CONSULTATION_STATUSES = (
    "offered",
    "online_requested",
    "in_person_requested",
    "in_person_booked",
    "invited",
    "accepted",
    "declined",
    "started",
    "completed",
    "cancelled",
)
CONSULTATION_PREFERENCES = ("online", "in_person")
CONSULTATION_RESPONSES = ("accepted", "declined")

# ---------------------------------------------------------------------------
# Medical-history catalogues (admin-managed, categorized)
# ---------------------------------------------------------------------------


class Condition(db.Model):
    __tablename__ = "conditions"
    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(120), nullable=False, unique=True)
    category = Column(String(64), nullable=False, default="General")
    description = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(36))
    created_at = Column(DateTime, default=datetime.now, nullable=False)


class Allergy(db.Model):
    __tablename__ = "allergies"
    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(120), nullable=False, unique=True)
    category = Column(String(64), nullable=False, default="General")
    description = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(36))
    created_at = Column(DateTime, default=datetime.now, nullable=False)


class Medication(db.Model):
    __tablename__ = "medications"
    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(120), nullable=False, unique=True)
    category = Column(String(64), nullable=False, default="General")
    description = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(36))
    created_at = Column(DateTime, default=datetime.now, nullable=False)


# Junction tables - patient↔condition, patient↔allergy, patient↔medication.
patient_conditions = db.Table(
    "patient_conditions",
    Column("patient_id", String(36), ForeignKey("patients.id", ondelete="CASCADE"), primary_key=True),
    Column("condition_id", String(36), ForeignKey("conditions.id", ondelete="CASCADE"), primary_key=True),
)
patient_allergies = db.Table(
    "patient_allergies",
    Column("patient_id", String(36), ForeignKey("patients.id", ondelete="CASCADE"), primary_key=True),
    Column("allergy_id", String(36), ForeignKey("allergies.id", ondelete="CASCADE"), primary_key=True),
)
patient_medications = db.Table(
    "patient_medications",
    Column("patient_id", String(36), ForeignKey("patients.id", ondelete="CASCADE"), primary_key=True),
    Column("medication_id", String(36), ForeignKey("medications.id", ondelete="CASCADE"), primary_key=True),
)


class TestCategory(db.Model):
    __tablename__ = "test_categories"
    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(120), nullable=False, unique=True)
    description = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)


class SampleType(db.Model):
    __tablename__ = "sample_types"
    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(120), nullable=False, unique=True)
    description = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)


class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True, default=_uuid)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    # the user successfully changes their password. This lets admins re-share
    # credentials when the email delivery fails.
    temp_password = Column(String(64))
    title = Column(String(32))
    full_name = Column(String(255))
    surname = Column(String(120))
    gender = Column(String(32))
    phone = Column(String(50))
    date_of_birth = Column(Date)
    employee_number = Column(String(64), unique=True)
    sa_id_number = Column(String(32), unique=True)
    hpcsa_number = Column(String(64), unique=True)
    avatar_url = Column(String(500))
    must_change_password = Column(Boolean, nullable=False, default=True)
    is_blocked = Column(Boolean, nullable=False, default=False)
    is_deactivated = Column(Boolean, nullable=False, default=False)
    deactivated_at = Column(DateTime)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(36))
    last_seen = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    user_roles = relationship("UserRole", backref="user", cascade="all, delete-orphan")
    patient_record = relationship(
        "Patient", backref="profile", uselist=False, foreign_keys="Patient.profile_id"
    )

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def roles(self):
        return [ur.role for ur in self.user_roles]

    @property
    def primary_role(self):
        order = ["admin", "lab_manager", "doctor", "lab_technician", "patient"]
        for r in order:
            if r in self.roles:
                return r
        return None

    @property
    def is_pending(self):
        return self.primary_role is None

    def has_role(self, role):
        return role in self.roles

    @property
    def is_active(self):  # Flask-Login hook
        return not self.is_blocked and not self.is_deactivated and self.deleted_at is None


class UserRole(db.Model):
    __tablename__ = "user_roles"
    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "role"),)


Index("idx_user_roles_role_user", UserRole.role, UserRole.user_id)
Index("idx_users_last_seen", User.last_seen)
Index("idx_users_active_presence", User.deleted_at, User.is_blocked, User.is_deactivated, User.last_seen)


class Patient(db.Model):
    __tablename__ = "patients"
    id = Column(String(36), primary_key=True, default=_uuid)
    profile_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), unique=True)
    mrn = Column(String(64), nullable=False, unique=True)
    full_name = Column(String(255), nullable=False)
    surname = Column(String(120))
    id_number = Column(String(32), unique=True)  # SA ID number (13 digits, Luhn)
    date_of_birth = Column(Date)
    gender = Column(String(32))
    blood_type = Column(String(8))
    phone = Column(String(50))
    email = Column(String(255))
    address = Column(Text)
    chronic_conditions = Column(Text)   # comma-separated or free text
    allergies = Column(Text)
    current_medication = Column(Text)
    created_by = Column(String(36), ForeignKey("users.id"))
    deleted_at = Column(DateTime)
    deleted_by = Column(String(36))
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    conditions = relationship("Condition", secondary=patient_conditions, backref="patients")
    allergy_list = relationship("Allergy", secondary=patient_allergies, backref="patients")
    medications = relationship("Medication", secondary=patient_medications, backref="patients")


Index("idx_patients_deleted_name", Patient.deleted_at, Patient.full_name)
Index("idx_patients_profile_deleted", Patient.profile_id, Patient.deleted_at)


class TestCatalog(db.Model):
    __tablename__ = "test_catalog"
    id = Column(String(36), primary_key=True, default=_uuid)
    code = Column(String(32), nullable=False, unique=True)
    name = Column(String(255), nullable=False, unique=True)
    category = Column(String(64), nullable=False)
    units = Column(String(32))
    reference_low = Column(Numeric(12, 3))
    reference_high = Column(Numeric(12, 3))
    reference_text = Column(Text)
    turnaround_hours = Column(Integer, nullable=False, default=1440)
    active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(36))
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    sample_type = Column(String(100))
    consumables_used = Column(Text)
    assigned_technician = db.Column(db.String(100))

    test_consumables = db.relationship(
    "TestConsumable",
    backref="test",
    lazy=True)
    
    assigned_technicians = db.relationship(
    "TechnicianTest",
    back_populates="test",
    lazy=True
    )

    @property
    def turnaround_minutes(self):
        """The project spec expresses turnaround as minutes.

        The original column is named ``turnaround_hours`` but the UI stores
        minutes in it, so expose an intention-revealing alias.
        """
        return self.turnaround_hours or 0


Index("idx_test_catalog_active_deleted", TestCatalog.active, TestCatalog.deleted_at)


class TestRequest(db.Model):
    __tablename__ = "test_requests"
    id = Column(String(36), primary_key=True, default=_uuid)
    request_number = Column(String(64), nullable=False, unique=True)
    patient_id = Column(String(36), ForeignKey("patients.id"), nullable=False)
    doctor_id = Column(String(36), ForeignKey("users.id"), nullable=True)  # nullable for patient self-bookings
    status = Column(String(32), nullable=False, default="submitted")
    priority = Column(String(16), nullable=False, default="routine")
    clinical_notes = Column(Text)
    release_note = Column(Text)
    released_at = Column(DateTime)
    cancel_reason = Column(Text)
    cancelled_by = Column(String(36), ForeignKey("users.id"))
    cancelled_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    patient = relationship("Patient", backref="requests")
    doctor = relationship("User", foreign_keys=[doctor_id])
    items = relationship("TestRequestItem", backref="request", cascade="all, delete-orphan")

    @property
    def can_cancel(self):
        return self.status in ("submitted", "samples_received")


    @property
    def active_items(self):
        return [item for item in self.items if item.status != "cancelled"]

    @property
    def is_complete(self):
        items = self.active_items
        return bool(items) and all(item.status in ("completed", "verified") for item in items)

    @property
    def all_verified(self):
        items = self.active_items
        return bool(items) and all(item.status == "verified" for item in items)

    @staticmethod
    def generate_number():
        return "REQ-" + datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6]


class TestRequestItem(db.Model):
    __tablename__ = "test_request_items"
    id = Column(String(36), primary_key=True, default=_uuid)
    request_id = Column(String(36), ForeignKey("test_requests.id", ondelete="CASCADE"), nullable=False)
    test_id = Column(String(36), ForeignKey("test_catalog.id"), nullable=False)
    status = Column(String(16), nullable=False, default="submitted")
    result_value = Column(Numeric(12, 3))
    result_text = Column(Text)
    result_notes = Column(Text)
    abnormal_flag = Column(String(16))
    assigned_to = Column(String(36), ForeignKey("users.id"))
    captured_by = Column(String(36), ForeignKey("users.id"))
    started_at = Column(DateTime)
    captured_at = Column(DateTime)
    completed_at = Column(DateTime)
    verified_by = Column(String(36), ForeignKey("users.id"))
    verified_at = Column(DateTime)
    verification_notes = Column(Text)
    review_notes = Column(Text)
    near_limit_reminded_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    test = relationship("TestCatalog")


class TestResultReview(db.Model):
    __tablename__ = "test_result_reviews"
    id = Column(String(36), primary_key=True, default=_uuid)
    item_id = Column(String(36), ForeignKey("test_request_items.id", ondelete="CASCADE"), nullable=False)
    reviewer_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    action = Column(String(24), nullable=False)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    item = relationship("TestRequestItem", backref="review_history")
    reviewer = relationship("User")


class OnlineConsultation(db.Model):
    __tablename__ = "online_consultations"
    id = Column(String(36), primary_key=True, default=_uuid)
    request_id = Column(String(36), ForeignKey("test_requests.id", ondelete="CASCADE"), nullable=False, index=True)
    patient_id = Column(String(36), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True)
    doctor_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_id = Column(String(36), ForeignKey("users.id"))
    status = Column(String(24), nullable=False, default="offered")
    patient_preference = Column(String(24))
    patient_response = Column(String(24))
    offered_at = Column(DateTime, default=datetime.now, nullable=False)
    patient_responded_at = Column(DateTime)
    scheduled_at = Column(DateTime)
    scheduled_end_at = Column(DateTime)
    doctor_started_at = Column(DateTime)
    ended_at = Column(DateTime)
    invite_message = Column(Text)
    decline_reason = Column(Text)
    session_record_filename = Column(String(255))
    session_record_mime = Column(String(80), default="text/plain")
    session_record_size = Column(Integer)
    session_record_body = Column(Text)
    room_token = Column(String(64), nullable=False, default=_room_token)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    request = relationship("TestRequest", backref="online_consultations")
    patient = relationship("Patient", backref="online_consultations")
    doctor = relationship("User", foreign_keys=[doctor_id], backref="doctor_online_consultations")
    requested_by = relationship("User", foreign_keys=[requested_by_id])

    @property
    def patient_user_id(self):
        return self.patient.profile_id if self.patient else None

    @property
    def can_join(self):
        return self.status in ("accepted", "started")

    @property
    def is_started(self):
        return self.status == "started" and self.doctor_started_at is not None


class DoctorAvailabilitySlot(db.Model):
    __tablename__ = "doctor_availability_slots"
    id = Column(String(36), primary_key=True, default=_uuid)
    doctor_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    starts_at = Column(DateTime, nullable=False, index=True)
    ends_at = Column(DateTime, nullable=False)
    location = Column(String(160))
    note = Column(Text)
    status = Column(String(20), nullable=False, default="open")
    booked_consultation_id = Column(String(36), ForeignKey("online_consultations.id", ondelete="SET NULL"), index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    doctor = relationship("User", foreign_keys=[doctor_id], backref="availability_slots")
    booked_consultation = relationship("OnlineConsultation", foreign_keys=[booked_consultation_id], backref="availability_slot")

    @property
    def is_open(self):
        return self.status == "open" and self.booked_consultation_id is None


class ConsultationSignal(db.Model):
    __tablename__ = "consultation_signals"
    id = Column(String(36), primary_key=True, default=_uuid)
    consultation_id = Column(String(36), ForeignKey("online_consultations.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    signal_type = Column(String(32), nullable=False)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)

    consultation = relationship("OnlineConsultation", backref="signals")
    sender = relationship("User")


Index("idx_online_consults_doctor_status", OnlineConsultation.doctor_id, OnlineConsultation.status, OnlineConsultation.scheduled_at)
Index("idx_online_consults_patient_status", OnlineConsultation.patient_id, OnlineConsultation.status, OnlineConsultation.scheduled_at)
Index("idx_online_consults_request_created", OnlineConsultation.request_id, OnlineConsultation.created_at)
Index("idx_doctor_availability_open", DoctorAvailabilitySlot.doctor_id, DoctorAvailabilitySlot.status, DoctorAvailabilitySlot.starts_at)
Index("idx_doctor_availability_booking", DoctorAvailabilitySlot.booked_consultation_id, DoctorAvailabilitySlot.status)
Index("idx_consult_signals_room_created", ConsultationSignal.consultation_id, ConsultationSignal.created_at)


Index("idx_request_items_request", TestRequestItem.request_id)
Index("idx_request_items_assigned_status_reminder", TestRequestItem.assigned_to, TestRequestItem.status, TestRequestItem.near_limit_reminded_at)
Index("idx_request_items_request_status", TestRequestItem.request_id, TestRequestItem.status)
Index("idx_request_items_status_test", TestRequestItem.status, TestRequestItem.test_id)
Index("idx_request_items_captured", TestRequestItem.captured_by, TestRequestItem.captured_at)
Index("idx_request_items_verified", TestRequestItem.verified_by, TestRequestItem.verified_at)
Index("idx_requests_doctor", TestRequest.doctor_id)
Index("idx_requests_patient", TestRequest.patient_id)
Index("idx_requests_status", TestRequest.status)
Index("idx_requests_doctor_status_created", TestRequest.doctor_id, TestRequest.status, TestRequest.created_at)
Index("idx_requests_patient_status_created", TestRequest.patient_id, TestRequest.status, TestRequest.created_at)
Index("idx_requests_created", TestRequest.created_at)
Index("idx_requests_released", TestRequest.released_at)


class Supplier(db.Model):
    __tablename__ = "suppliers"
    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String(255), nullable=False, unique=True)
    contact_name = Column(String(255))
    email = Column(String(255))
    phone = Column(String(50))
    address = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(36))
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)


class Consumable(db.Model):
    __tablename__ = "consumables"
    id = Column(String(36), primary_key=True, default=_uuid)
    sku = Column(String(64), nullable=False, unique=True)
    name = Column(String(255), nullable=False, unique=True)
    category = Column(String(64), nullable=False)
    unit = Column(String(32), nullable=False, default="unit")
    supplier_id = Column(String(36), ForeignKey("suppliers.id", ondelete="SET NULL"))
    reorder_level = Column(Integer, nullable=False, default=10)
    current_stock = Column(Integer, nullable=False, default=0)
    unit_cost = Column(Numeric(12, 2))
    expiry_date = Column(Date)
    active = Column(Boolean, nullable=False, default=True)
    deleted_at = Column(DateTime)
    deleted_by = Column(String(36))
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    supplier = relationship("Supplier", backref="consumables")


Index("idx_consumables_stock", Consumable.deleted_at, Consumable.current_stock, Consumable.reorder_level)


class StockMovement(db.Model):
    __tablename__ = "stock_movements"
    id = Column(String(36), primary_key=True, default=_uuid)
    consumable_id = Column(String(36), ForeignKey("consumables.id", ondelete="CASCADE"), nullable=False)
    movement_type = Column(String(16), nullable=False)
    quantity = Column(Integer, nullable=False)
    notes = Column(Text)
    created_by = Column(String(36))
    created_at = Column(DateTime, default=datetime.now, nullable=False)


class Notification(db.Model):
    __tablename__ = "notifications"
    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    body = Column(Text)
    link = Column(String(500))
    read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)


Index("idx_notifications_user_read_created", Notification.user_id, Notification.read, Notification.created_at)
Index("idx_notifications_user_created", Notification.user_id, Notification.created_at)


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"
    id = Column(String(36), primary_key=True, default=_uuid)
    sender_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    recipient_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    body = Column(Text, nullable=False)
    read_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    sender = relationship("User", foreign_keys=[sender_id])
    recipient = relationship("User", foreign_keys=[recipient_id])


Index("idx_chat_sender_recipient", ChatMessage.sender_id, ChatMessage.recipient_id)
Index("idx_chat_recipient_read", ChatMessage.recipient_id, ChatMessage.read_at)
Index("idx_chat_created_at", ChatMessage.created_at)
Index("idx_chat_pair_created", ChatMessage.sender_id, ChatMessage.recipient_id, ChatMessage.created_at)
Index("idx_chat_unread_thread", ChatMessage.recipient_id, ChatMessage.sender_id, ChatMessage.read_at)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = Column(String(36), primary_key=True, default=_uuid)
    actor_id = Column(String(36))
    action = Column(String(64), nullable=False)
    entity_type = Column(String(64))
    entity_id = Column(String(36))
    details = Column(Text)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)


# ---------------------------------------------------------------------------
# Consent management - patient grants doctor access to selected test request items
# ---------------------------------------------------------------------------

consent_grant_items = db.Table(
    "consent_grant_items",
    Column("grant_id", String(36), ForeignKey("consent_grants.id", ondelete="CASCADE"), primary_key=True),
    Column("request_id", String(36), ForeignKey("test_requests.id", ondelete="CASCADE"), primary_key=True),
)

consent_grant_request_items = db.Table(
    "consent_grant_request_items",
    Column("grant_id", String(36), ForeignKey("consent_grants.id", ondelete="CASCADE"), primary_key=True),
    Column("item_id", String(36), ForeignKey("test_request_items.id", ondelete="CASCADE"), primary_key=True),
)


Index("idx_consent_grant_items_request", consent_grant_items.c.request_id, consent_grant_items.c.grant_id)
Index("idx_consent_request_items_item", consent_grant_request_items.c.item_id, consent_grant_request_items.c.grant_id)


class ConsentGrant(db.Model):
    __tablename__ = "consent_grants"
    id = Column(String(36), primary_key=True, default=_uuid)
    patient_id = Column(String(36), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    doctor_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    granted_at = Column(DateTime, default=datetime.now, nullable=False)
    revoked_at = Column(DateTime)
    note = Column(Text)

    patient = relationship("Patient", backref="consent_grants")
    doctor = relationship("User", foreign_keys=[doctor_id])
    requests = relationship("TestRequest", secondary=consent_grant_items)
    request_items = relationship("TestRequestItem", secondary=consent_grant_request_items)

    @property
    def is_active(self):
        return self.revoked_at is None

    def items_for_request(self, req):
        selected = [item for item in self.request_items if item.request_id == req.id]
        if selected:
            return selected
        if any(shared.id == req.id for shared in self.requests):
            return list(req.items)
        return []

    def is_full_request_shared(self, req):
        selected = [item for item in self.request_items if item.request_id == req.id]
        return not selected or len(selected) == len(req.items)


Index("idx_consent_doctor_patient_active", ConsentGrant.doctor_id, ConsentGrant.patient_id, ConsentGrant.revoked_at, ConsentGrant.granted_at)
Index("idx_consent_patient_active", ConsentGrant.patient_id, ConsentGrant.revoked_at, ConsentGrant.granted_at)


# ---------------------------------------------------------------------------
# Doctor-initiated access requests (patient accept / decline)
# ---------------------------------------------------------------------------
ACCESS_REQUEST_STATUSES = ("pending", "accepted", "declined")


class AccessRequest(db.Model):
    __tablename__ = "access_requests"
    id = Column(String(36), primary_key=True, default=_uuid)
    doctor_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    patient_id = Column(String(36), ForeignKey("patients.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    responded_at = Column(DateTime)
    grant_id = Column(String(36), ForeignKey("consent_grants.id", ondelete="SET NULL"))

    doctor = relationship("User", foreign_keys=[doctor_id])
    patient = relationship("Patient", backref="access_requests")
    grant = relationship("ConsentGrant", foreign_keys=[grant_id])


Index("idx_access_patient_status_created", AccessRequest.patient_id, AccessRequest.status, AccessRequest.created_at)
Index("idx_access_doctor_status_created", AccessRequest.doctor_id, AccessRequest.status, AccessRequest.created_at)


class ConsumableOrder(db.Model):
    __tablename__ = "consumable_orders"

    id = Column(String(36), primary_key=True, default=_uuid)

    order_number = Column(String(64), unique=True, nullable=False)

    supplier_id = Column(
        String(36),
        ForeignKey("suppliers.id")
    )

    consumable_id = Column(
        String(36),
        ForeignKey("consumables.id")
    )

    quantity = Column(Integer, nullable=False, default=1)

    ordered_at = Column(DateTime, default=datetime.now)

    received_at = Column(DateTime)
    completed_at = Column(DateTime)
    cancelled_at = Column(DateTime)
    cancel_reason = Column(Text)
    supplier_notified_at = Column(DateTime)

    supplier = relationship("Supplier")

    consumable = relationship("Consumable")

    received_quantity = Column(Integer, nullable=False, default=0)

    status = Column(
        String(20),
        nullable=False,
        default="ordered"
    )
    @property
    def remaining_quantity(self):
        if self.items:
            return sum(item.remaining_quantity for item in self.items)
        return self.quantity - self.received_quantity

    @property
    def total_quantity(self):
        if self.items:
            return sum(item.quantity for item in self.items)
        return self.quantity

    @property
    def total_received_quantity(self):
        if self.items:
            return sum(item.received_quantity for item in self.items)
        return self.received_quantity

    def refresh_status(self):
        if self.status == "cancelled":
            return
        active_items = [item for item in self.items if item.status != "cancelled"]
        if self.items and not active_items:
            self.status = "cancelled"
            return
        if self.items:
            if active_items and all(item.status == "received" for item in active_items):
                self.status = "complete"
                self.completed_at = self.completed_at or datetime.now()
            elif any(item.received_quantity > 0 or item.status == "received" for item in active_items):
                self.status = "partially_complete"
            else:
                self.status = "ordered"
        elif self.received_quantity >= self.quantity:
            self.status = "complete"
            self.completed_at = self.completed_at or datetime.now()
        elif self.received_quantity:
            self.status = "partially_complete"
        else:
            self.status = "ordered"


class ConsumableOrderItem(db.Model):
    __tablename__ = "consumable_order_items"
    id = Column(String(36), primary_key=True, default=_uuid)
    order_id = Column(String(36), ForeignKey("consumable_orders.id", ondelete="CASCADE"), nullable=False)
    consumable_id = Column(String(36), ForeignKey("consumables.id"), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    received_quantity = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="ordered")
    received_at = Column(DateTime)
    cancelled_at = Column(DateTime)
    cancel_reason = Column(Text)

    order = relationship("ConsumableOrder", backref="items")
    consumable = relationship("Consumable")

    @property
    def remaining_quantity(self):
        return max(0, self.quantity - self.received_quantity)
    
class TestConsumable(db.Model):
    __tablename__ = "test_consumables"

    id = db.Column(
        db.String(36),
        primary_key=True,
        default=_uuid
    )

    test_id = db.Column(
        db.String(36),
        db.ForeignKey("test_catalog.id")
    )

    consumable_id = db.Column(
        db.String(36),
        db.ForeignKey("consumables.id")
    )

    quantity_required = db.Column(
        db.Integer,
        nullable=False,
        default=1
    )

    consumable = db.relationship(
        "Consumable",
        backref="test_consumable_links"
    )

class TechnicianTest(db.Model):
    __tablename__ = "technician_tests"

    id = db.Column(
        db.String(36),
        primary_key=True,
        default=_uuid
    )

    technician_id = db.Column(
        db.String(36),
        db.ForeignKey("users.id")
    )

    test_id = db.Column(
        db.String(36),
        db.ForeignKey("test_catalog.id")
    )

    technician = db.relationship(
    "User",
    backref="technician_assignments"
    )

    test = db.relationship(
    "TestCatalog",
    back_populates="assigned_technicians"
    )


Index("idx_technician_tests_technician_test", TechnicianTest.technician_id, TechnicianTest.test_id)
Index("idx_technician_tests_test", TechnicianTest.test_id)


class Sample(db.Model):
    __tablename__ = "samples"

    id = Column(String(36), primary_key=True, default=_uuid)
    request_id = Column(String(36), ForeignKey("test_requests.id", ondelete="CASCADE"), nullable=False)
    barcode = Column(String(100), unique=True, nullable=False)
    sample_type = Column(String(50), nullable=False)
    status = Column(String(20), nullable=False, default="collected")
    collected_at = Column(DateTime, default=datetime.now)
    received_by = Column(String(36), ForeignKey("users.id"))
    received_at = Column(DateTime)
    rejected_by = Column(String(36), ForeignKey("users.id"))
    rejected_at = Column(DateTime)
    rejection_reason = Column(Text)
    request = relationship("TestRequest", backref="samples")
    
    @staticmethod
    def generate_barcode(request_number, sample_index, sample_type):
        """
        Generate a unique barcode based on:
        - Request number
        - Sample index
        - Sample type code
        - Timestamp hash
        """
        # Create a prefix based on sample type
        sample_type_codes = {
            "Whole Blood": "WB",
            "EDTA Blood": "EDTA",
            "Citrated Plasma": "CP",
            "Serum": "SER",
            "Plasma": "PLAS",
            "Urine": "URI",
            "Bone Marrow Aspirate": "BMA"
        }
        
        type_code = sample_type_codes.get(sample_type, "GEN")
        
        # Format: [Request Short Code]-[Type Code]-[Index]-[Random]
        # Example: REQ-20240530-ABC123-EDTA-01-X7K9
        short_request = request_number[-8:] if len(request_number) > 8 else request_number
        
        # Generate unique hash
        unique_id = uuid.uuid4().hex[:4].upper()
        
        # Create barcode
        barcode = f"{type_code}-{short_request}-{sample_index:02d}-{unique_id}"
        
        return barcode
    
    @staticmethod
    def generate_simple_barcode():
        """Simple barcode generation with timestamp and random numbers"""
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        random_num = random.randint(1000, 9999)
        return f"SMP-{timestamp}-{random_num}"
    
    @staticmethod
    def generate_sequential_barcode(prefix="SMP"):
        """Get last barcode and increment"""
        last_sample = Sample.query.order_by(Sample.id.desc()).first()
        if last_sample and last_sample.barcode:
            try:
                # Extract number from last barcode
                parts = last_sample.barcode.split('-')
                if len(parts) >= 2:
                    last_num = int(parts[-1])
                    new_num = last_num + 1
                else:
                    new_num = 1
            except:
                new_num = 1
        else:
            new_num = 1
        
        date_str = datetime.now().strftime("%Y%m%d")
        return f"{prefix}-{date_str}-{new_num:06d}"    


Index("idx_samples_request_status", Sample.request_id, Sample.status)
