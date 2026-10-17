"""Accounts: signup/login/logout plus a saved-run history page. Deliberately
minimal — no email verification, no password reset (no outbound mail setup
exists in this project yet). Rate-limited the same way /audit and /editor/*
already are, since signup/login are the other obvious brute-force/spam
targets in this app."""
import re

import grader_v11 as gv11
from flask import Blueprint, Response, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from extensions import limiter
from models import SavedRun, User, db
from runs import run_audit_and_store

auth_bp = Blueprint('auth', __name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@auth_bp.route('/signup', methods=['GET', 'POST'])
@limiter.limit('10 per hour')
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('auth.account'))
    if request.method == 'GET':
        return render_template('signup.html')

    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''

    if not EMAIL_RE.match(email):
        return render_template('signup.html', error='Enter a valid email address.', email=email), 400
    if len(password) < 8:
        return render_template('signup.html', error='Password must be at least 8 characters.', email=email), 400
    if User.query.filter_by(email=email).first():
        return render_template('signup.html', error='An account with that email already exists.', email=email), 400

    user = User(email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return redirect(url_for('auth.account'))


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('20 per hour')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('auth.account'))
    if request.method == 'GET':
        return render_template('login.html', next=request.args.get('next', ''))

    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''
    next_url = request.form.get('next') or url_for('auth.account')

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return render_template('login.html', error='Incorrect email or password.', email=email, next=next_url), 400

    login_user(user, remember=True)
    # Only ever redirect to a same-site path from the hidden "next" field —
    # never an absolute/external URL, so a crafted link can't use login as
    # an open redirect.
    if not next_url.startswith('/'):
        next_url = url_for('auth.account')
    return redirect(next_url)


@auth_bp.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('current'))


@auth_bp.route('/account')
@login_required
def account():
    runs = current_user.runs.all()
    return render_template('account.html', runs=runs, grade_colours=gv11.GRADE_COLOURS)


@auth_bp.route('/account/rename/<int:saved_run_id>', methods=['POST'])
@login_required
def rename_run(saved_run_id):
    saved = SavedRun.query.filter_by(id=saved_run_id, user_id=current_user.id).first_or_404()
    name = (request.form.get('trail_name') or '').strip()
    if name:
        saved.trail_name = name[:255]
        db.session.commit()
    return redirect(url_for('auth.account'))


@auth_bp.route('/account/rerun/<int:saved_run_id>', methods=['POST'])
@login_required
def rerun(saved_run_id):
    """Re-execute a saved audit from its stored KML/options, exactly like a
    fresh /audit POST, and hand off to the normal run-viewing flow — avoids
    persisting (and keeping in sync) a second copy of the full result."""
    saved = SavedRun.query.filter_by(id=saved_run_id, user_id=current_user.id).first_or_404()
    run_id, error, status = run_audit_and_store(
        saved.kml_bytes, saved.trail_name, saved.stated_grade, saved.want_sideslope)
    if run_id is None:
        return render_template('account.html', runs=current_user.runs.all(),
                               grade_colours=gv11.GRADE_COLOURS, error=error), status
    return redirect(url_for('view_run', run_id=run_id))


@auth_bp.route('/account/kml/<int:saved_run_id>')
@login_required
def saved_kml(saved_run_id):
    saved = SavedRun.query.filter_by(id=saved_run_id, user_id=current_user.id).first_or_404()
    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', saved.trail_name).strip('_') or 'trail'
    return Response(saved.kml_bytes, mimetype='application/vnd.google-earth.kml+xml',
                     headers={'Content-Disposition': 'attachment; filename="%s.kml"' % safe_name})


@auth_bp.route('/account/delete/<int:saved_run_id>', methods=['POST'])
@login_required
def delete_run(saved_run_id):
    saved = SavedRun.query.filter_by(id=saved_run_id, user_id=current_user.id).first_or_404()
    db.session.delete(saved)
    db.session.commit()
    return redirect(url_for('auth.account'))
