"""Account + saved-run persistence. Kept deliberately small: SavedRun stores
only the inputs (KML bytes, stated grade, options) and a handful of summary
fields for the history list — not the full in-memory audit result (dense
point arrays, base64 chart images), which would bloat the database for no
real benefit. Re-opening a saved run re-executes gv11.run_audit() from the
stored KML, same as a fresh /audit POST, and lands in the normal RUNS cache."""
from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    runs = db.relationship('SavedRun', backref='user', lazy='dynamic',
                            order_by='SavedRun.run_at.desc()', cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class SavedRun(db.Model):
    __tablename__ = 'saved_runs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    run_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # Inputs — enough to re-run the audit exactly as it was first graded.
    trail_name = db.Column(db.String(255), nullable=False)
    kml_bytes = db.Column(db.LargeBinary, nullable=False)
    stated_grade = db.Column(db.Integer, nullable=False)
    want_sideslope = db.Column(db.Boolean, default=True, nullable=False)

    # Summary fields for the history list, so it can render without
    # re-running every saved audit just to show a table.
    overall_grade = db.Column(db.Integer)
    avg_gradient = db.Column(db.Float)
    trail_length_m = db.Column(db.Float)
    out_of_grade_pct = db.Column(db.Float)

    # Mean of the trail line's own points — just enough to place a pin on
    # the account page's saved-trails map, not a replacement for the full
    # line (that still only lives in kml_bytes, parsed fresh on rerun).
    centroid_lat = db.Column(db.Float)
    centroid_lon = db.Column(db.Float)
