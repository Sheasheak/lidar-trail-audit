import json, os, re, threading, traceback, uuid, webbrowser
from collections import OrderedDict
from datetime import datetime

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for
from flask_login import LoginManager, current_user, login_required

import grader_v11 as gv11
from extensions import limiter
from models import SavedRun, User, db
from runs import MAX_RUNS, RUNS, RUN_KML, SAVED_RUN_IDS, run_audit_and_store

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
try:
    import anthropic
except ImportError:
    anthropic = None

app = Flask(__name__)
# 50MB: KML uploads are tiny, but a user-uploaded DEM GeoTIFF for the editor's
# "grade any trail" option needs more headroom than the original 10MB cap.
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_KML = os.path.join(REPO_ROOT, 'data', 'askins.kml')

if load_dotenv:
    load_dotenv(os.path.join(REPO_ROOT, '.env'))

# SECRET_KEY signs the session cookie — a dev fallback is fine locally, but
# without a real one set on Render every redeploy invalidates all sessions
# (everyone gets logged out). DATABASE_URL: Render's Postgres add-on hands
# out "postgres://...", which SQLAlchemy 1.4+ rejects — it wants
# "postgresql://" — hence the scheme swap. No DATABASE_URL (local dev) falls
# back to a SQLite file next to the app.
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-only-insecure-key-set-SECRET_KEY-in-prod')
db_url = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(REPO_ROOT, 'trailsafe.db'))
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

limiter.init_app(app)
db.init_app(app)
with app.app_context():
    db.create_all()

login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.init_app(app)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# Imported after load_dotenv(), not before: the legacy blueprint reads
# ANTHROPIC_API_KEY at import time for its own AI-summary client, same as
# this file does below — importing it earlier would silently leave that
# client unset, the exact bug already hit once this session with the main
# app's own LINZ_API_KEY.
from legacy import legacy_bp
app.register_blueprint(legacy_bp, url_prefix='/legacy')

from auth import auth_bp
app.register_blueprint(auth_bp)

SUMMARY_MODEL = "claude-opus-5"
_anthropic_client = anthropic.Anthropic() if (anthropic and os.environ.get('ANTHROPIC_API_KEY')) else None


FIELD_OBSTACLES = ['None', 'Up to 50mm', 'Up to 100mm', 'Up to 200mm', 'Up to 600mm', 'No limit / bigger']
FIELD_SURFACE = ['Hardened and smooth (sealed)', 'Firm and stable (compacted gravel)',
                  'Mostly stable, some loose sections', 'Generally firm but loose/broken in places',
                  'Highly variable — natural surface, mud, ruts']
FIELD_FEATURES = ['All rollable — no jumps or drops', 'Rollable — stepped drops up to 200mm',
                   'Rollable — stepped drops up to 400mm, small tabletops',
                   'Mostly rollable — drops up to 600mm, gap jumps with B-lines',
                   'Stepped drops up to 1.5m, large jumps', 'No limits']


def summarise_audit(result, api_key=None, field_obs=None):
    """Grade, plain-language summary, and recommendation via Claude — ported
    from TrailAudit_v11.ipynb cell 24, same audit_data shape, extended to have
    Claude itself settle the final grade and hand back a separate
    recommendation rather than just narrating a Python-computed number."""
    client = anthropic.Anthropic(api_key=api_key) if (anthropic and api_key) else _anthropic_client
    if not client:
        return None
    lidar_grade = result['overall_grade']
    field_grades = []
    field_obs = field_obs or (None, None, None)
    obstacles, surface, features = field_obs
    if obstacles is not None:
        field_grades.append(obstacles + 1)
    if surface is not None:
        field_grades.append(surface + 1)
    if features is not None:
        field_grades.append(features + 1)

    audit_data = {
        "trail_name": result['trail_name'],
        "stated_grade": f"{result['stated_grade']} ({gv11.GRADE_COLOURS[result['stated_grade']]})",
        "lidar_measured_grade": f"{lidar_grade} ({gv11.GRADE_COLOURS[lidar_grade]})",
        "avg_gradient_pct": result['avg_gradient'],
        "steepest_gradient_pct": result['worst_gradient'],
        "gradient_grade": result['gradient_grade'],
        "gradient_finding": result['gradient_reason'],
        "corner_grade": result['radius_grade'],
        "corner_finding": result['radius_reason'],
        "tightest_corner_m": result['tightest_corner'],
        "pct_corners_under_stated_min": result['pct_under_stated_min'],
        "mean_cross_slope_deg": result['mean_abs_sideslope'],
        "pct_trail_over_30deg_sideslope": result['pct_steep_sideslope'],
        "steep_sideslope_sections_m": [
            f"{r['start_m']}-{r['end_m']}m (peak {r['peak_deg']}°)"
            for r in result.get('sideslope_exceedances', [])
        ],
    }
    field_note = ''
    if obstacles is not None:
        audit_data['field_tread_obstacles'] = FIELD_OBSTACLES[obstacles]
    if surface is not None:
        audit_data['field_trail_surface'] = FIELD_SURFACE[surface]
    if features is not None:
        audit_data['field_technical_features'] = FIELD_FEATURES[features]
    if field_grades:
        field_note = (f" The rider's field observations (tread obstacles, surface, "
                       f"technical features/jumps — see the field_ prefixed figures "
                       f"below, per NZ MTB Guidelines p.5-7) imply grade(s) "
                       f"{', '.join(str(g) for g in field_grades)}. LiDAR can't see "
                       f"jumps, drops or surface texture, so field observations take "
                       f"precedence whenever they imply a harder grade than the "
                       f"terrain data alone.")

    prompt = f"""You are grading a mountain bike trail safety audit. The
figures below were measured from LiDAR elevation data and graded against the
NZ Mountain Bike Trail Design & Construction Guidelines (Recreation Aotearoa,
2022), where grade 1 is easiest and grade 6 is extreme.
{field_note}

Decide the final grade: it is the highest of lidar_measured_grade and any
field-observed grade(s) implied above — field observations only ever push the
grade up from the LiDAR reading, never down, since they capture hazards LiDAR
cannot see. mean_cross_slope_deg and pct_trail_over_30deg_sideslope are
contextual only — the guidelines don't assign a grade off sideslope alone, so
don't let them change the grade, but do mention them in the summary if the
trail sits on steep sidling ground, since that drives exposure and how much
benching/cut-fill the tread needs. Use ONLY the figures given — do not invent
numbers or recompute gradient/corner statistics yourself.

If steep_sideslope_sections_m is non-empty, that exposure is real risk the
grade number doesn't capture (sideslope never raises the grade). In that case
the recommendation must call out physical mitigation — edge barriers, rail,
or rock armouring — at those specific chainage ranges, not just a
re-signposting or grade change. If steep_sideslope_sections_m is empty, don't
mention barriers at all.

Respond with ONLY a JSON object (no markdown fences, no other text) with
exactly these keys:
  "grade": integer 1-6, the final grade per the rule above
  "summary": 2-3 short paragraphs of plain, professional English for a trail
    manager, explaining whether the trail matches its signposted grade and
    what the main findings are, including any notable sideslope/exposure
  "recommendation": 1-2 short sentences of concrete, actionable next steps
    for the trail manager (e.g. re-signpost, remediate a specific section,
    add edge barriers at a flagged chainage range, or confirm current
    signage is fine) — kept separate from the summary

DATA:
{audit_data}"""
    try:
        msg = client.messages.create(
            model=SUMMARY_MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in msg.content if b.type == "text").strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            text = text[4:] if text.startswith('json') else text
        data = json.loads(text.strip())
        grade = int(data['grade'])
        return {
            'grade': max(1, min(6, grade)),
            'grade_name': gv11.GRADE_COLOURS[max(1, min(6, grade))],
            'summary': data['summary'],
            'recommendation': data['recommendation'],
        }
    except Exception:
        traceback.print_exc()
        return None


MAX_VISION_PHOTOS = 6
VISION_MODEL = "claude-opus-5"
# Generous enough for consumer GPS drift (worse under canopy), tight enough
# to reject a photo/frame whose GPS fix is nowhere near this trail at all
# (wrong trail, stale/cached location, or an unrelated ride) instead of
# silently snapping it onto the trail anyway — see nearest_chainage.
VISION_GPS_MAX_DISTANCE_M = 200


HAZARD_CATEGORIES = ['root', 'loose_rock', 'jump_or_drop', 'erosion', 'surface_condition', 'exposure', 'other']


def analyze_trail_photo(photo_bytes, media_type='image/jpeg', api_key=None, stated_grade=None):
    """AI trail camera prototype: send one photo to a vision-capable Claude
    model, asking it to flag the technical features/hazards bare-earth
    LiDAR can't see. This is the realistic near-term path from the
    /roadmap/vision page, not the custom-trained production pipeline
    documented there — sampling individual geotagged photos through an
    existing model, no training involved (there is no training step in
    this prototype at all — the fix for vague/unhelpful output here is
    prompt specificity, not a dataset).

    stated_grade (the trail's signposted NZ MTB grade, 1-6) is optional
    context so a feature expected at that grade — e.g. a jump on a
    signposted grade 5 jump trail — isn't flagged as a "hazard" the way
    the same feature would be on a grade 2 trail; without it severity
    calibration has no reference point. Returns a dict or None on failure
    (bad key, model error, unparseable response)."""
    client = anthropic.Anthropic(api_key=api_key) if (anthropic and api_key) else _anthropic_client
    if not client:
        return None
    grade_context = (
        f"This trail is signposted as NZ MTB grade {stated_grade} — judge severity "
        f"relative to what a rider on a grade {stated_grade} trail should already expect "
        f"(e.g. a jump lip is normal on a grade 5+ jump trail and not a hazard there, but "
        f"would be on a grade 2 trail). "
    ) if stated_grade else ""
    prompt = (
        "This is a single photo taken on a mountain bike trail. Bare-earth LiDAR "
        "elevation data already grades this trail's gradient, turn radius and "
        "cross-slope — it cannot see what's in this photo: roots, loose rock, "
        "jump lips or drops, erosion, exposure, or surface condition. Look only "
        "for technical features or hazards actually visible in this photo.\n\n"
        f"{grade_context}"
        "Generic phrases like \"exercise caution\" or \"trail looks rough\" are "
        "useless to an inspector — every finding must name the exact feature, "
        "roughly where it sits in frame (e.g. \"across the full tread width\", "
        "\"on the outside of the turn\", \"on the landing\"), and a size "
        "estimate if visible.\n\n"
        "A single photo has no inherent scale — a small rock close up and a "
        "big rock far away can look identical — so a size estimate is only "
        "trustworthy if it's calibrated against something of known real-world "
        "size actually visible in frame. If part of the bike is visible (most "
        "useful: a wheel — a 29\" mountain bike wheel's outer diameter, tyre "
        "included, is about 0.74 m; a 27.5\" wheel is about 0.70 m — or failing "
        "that a crank arm at about 0.175 m), use it as a ruler and say so (e.g. "
        "\"~0.3m root cluster, estimated against the visible front wheel\"). If "
        "nothing of known size is visible, still give your best estimate but "
        "say explicitly that it's an uncalibrated visual guess (e.g. \"a 1m+ "
        "drop, rough visual estimate — no reference object in frame\"), so an "
        "inspector can tell a measured figure from a guess.\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no other text):\n"
        '  "hazard_found": true or false\n'
        f'  "category": one of {HAZARD_CATEGORIES} if hazard_found is true, otherwise null\n'
        '  "summary": one specific sentence naming the exact feature, its '
        'position in frame, and a size estimate labelled as calibrated or a '
        'rough guess per above — not a generic warning — or "" if hazard_found '
        'is false\n'
        '  "severity": "minor" (cosmetic, doesn\'t change the ride), "moderate" '
        '(needs line choice/caution but rideable at the stated grade), or '
        '"significant" (a rider at the stated grade could reasonably not expect '
        'this and get hurt, or it undermines the signposted grade) if '
        'hazard_found is true, otherwise null\n'
        '  "recommendation": one short concrete inspector action (e.g. '
        '"regrade/remove root cluster", "flag for re-signposting", "monitor '
        'erosion next inspection", "no action needed") or "" if hazard_found is '
        'false\n'
        "Don't invent anything not visible in the photo — if it just looks like "
        "unremarkable trail tread, hazard_found is false."
    )
    try:
        import base64
        b64 = base64.b64encode(photo_bytes).decode('ascii')
        msg = client.messages.create(
            model=VISION_MODEL, max_tokens=1000,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": prompt},
            ]}],
        )
        text = next((b.text for b in msg.content if b.type == "text"), None)
        if text is None:
            raise ValueError(f"No text content in model response (stop_reason={msg.stop_reason})")
        text = text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            text = text[4:] if text.startswith('json') else text
        data = json.loads(text.strip())
        category = data.get('category')
        return {
            'hazard_found': bool(data.get('hazard_found')),
            'category': category if category in HAZARD_CATEGORIES else None,
            'summary': (data.get('summary') or '').strip(),
            'severity': data.get('severity'),
            'recommendation': (data.get('recommendation') or '').strip(),
        }
    except Exception:
        traceback.print_exc()
        return None


# User-uploaded DEMs for the editor's "grade any trail" option, keyed by
# dem_id. Values are the (array, transform, nodata) tuple from
# gv11.load_uploaded_dem, already reprojected — loaded once at upload time
# rather than on every terrain/audit call that references it.
CUSTOM_DEMS = OrderedDict()
MAX_DEMS = 6


def _render_input(error=None, status=200):
    return render_template('input.html', error=error), status


@app.errorhandler(429)
def _rate_limited(e):
    """Flask-Limiter's default 429 is a plain HTML page, which the editor's
    fetch()-based routes choke on (.json() throws a cryptic "Unexpected
    token '<'" instead of showing the actual message) — match each route's
    normal response shape instead. description carries the limit that was
    hit (e.g. "10 per 1 hour")."""
    message = 'Too many requests (%s) — try again shortly.' % getattr(e, 'description', 'rate limit hit')
    if request.path.startswith('/editor/') or request.path.endswith('/summary'):
        return jsonify({'error': message}), 429
    return _render_input(message, 429)


@app.route('/')
def index():
    """Bare picker between the live current version and the archived
    pre-October /legacy build — the main page, deliberately unstyled (no
    audit.css) so it can't be mistaken for favouring either option."""
    return render_template('versions.html')


@app.route('/current')
def current():
    """The marketing landing page — what the tool is and what's coming
    next — for the "Current version" branch off the / picker, rather than
    dropping straight into the editor."""
    return render_template('landing.html')


@app.route('/editor')
def editor():
    return render_template('editor.html')


@app.route('/batch')
def batch():
    """Pick several KML files at once; each gets its own "Edit" button into
    the trail editor. Entirely client-side hand-off (sessionStorage, see
    editor.html) — nothing is uploaded here, so no server-side storage or
    cleanup to manage for files that are never opened."""
    return render_template('batch.html')


@app.route('/versions')
def versions():
    """Bare picker between the live current version and the archived
    pre-October /legacy build — deliberately unstyled (no audit.css) so
    it can't be mistaken for favouring either option."""
    return render_template('versions.html')


@app.route('/roadmap/vision')
def roadmap_vision():
    """R&D status page for the AI trail camera / hazard-detection roadmap
    item — the "hard version" (custom-trained, production-grade computer
    vision), as opposed to the frame-sampled vision-LLM prototype path.
    Static page, no backend logic."""
    return render_template('roadmap_vision.html')


SITEMAP_PAGES = ['/current', '/grade', '/editor', '/batch', '/roadmap/vision']


@app.route('/robots.txt')
def robots_txt():
    """Keep crawlers off account/auth pages and the archived /legacy build
    (same grading tool, would otherwise compete with /current for ranking)."""
    lines = [
        'User-agent: *',
        'Disallow: /account',
        'Disallow: /login',
        'Disallow: /signup',
        'Disallow: /organisation',
        'Disallow: /legacy/',
        '',
        'Sitemap: https://trailsafe.nz/sitemap.xml',
    ]
    return Response('\n'.join(lines), mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    urls = ''.join(f'<url><loc>https://trailsafe.nz{p}</loc></url>' for p in SITEMAP_PAGES)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>')
    return Response(xml, mimetype='application/xml')


@app.route('/editor/terrain', methods=['POST'])
@limiter.limit("30 per minute")
def editor_terrain():
    body = request.get_json(silent=True) or {}
    latlngs = body.get('latlngs')
    if not latlngs or len(latlngs) < 2:
        return jsonify({'error': 'No trail line given.'}), 400
    dem_source = CUSTOM_DEMS.get(body.get('dem_id'))
    points_wgs84 = [(lon, lat) for lat, lon in latlngs]
    try:
        overlays = gv11.terrain_overlays(points_wgs84, dem_source=dem_source)
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500
    return jsonify(overlays)


@app.route('/editor/dem', methods=['POST'])
@limiter.limit("10 per hour")
def editor_dem():
    """Accepts a user-uploaded GeoTIFF DEM so the editor (and grading, via
    the 'Done' handoff) can work for trails outside the bundled Canterbury/
    Christchurch tile coverage."""
    f = request.files.get('dem')
    if not f or not f.filename:
        return jsonify({'error': 'Choose a DEM file (GeoTIFF).'}), 400
    if not f.filename.lower().endswith(('.tif', '.tiff')):
        return jsonify({'error': 'That file is not a GeoTIFF (.tif/.tiff) — got "%s".' % f.filename}), 400
    try:
        loaded = gv11.load_uploaded_dem(f.read())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': 'Could not read that file: %s' % e}), 400
    dem_id = uuid.uuid4().hex[:10]
    CUSTOM_DEMS[dem_id] = loaded
    while len(CUSTOM_DEMS) > MAX_DEMS:
        CUSTOM_DEMS.popitem(last=False)
    return jsonify({'dem_id': dem_id})


@app.route('/grade')
def grade_form():
    """The manual upload-a-KML-and-pick-a-stated-grade form, for anyone who
    doesn't want to use the interactive editor first."""
    return _render_input()


@app.route('/audit', methods=['POST'])
@limiter.limit("20 per hour")
def audit():
    source = request.form.get('source', 'sample')
    if source == 'upload':
        f = request.files.get('kml')
        if not f or not f.filename:
            return _render_input('Choose a KML file, or switch back to the bundled Askins line.', 400)
        if not f.filename.lower().endswith('.kml'):
            return _render_input('That file is not a .kml — got "%s".' % f.filename, 400)
        kml_bytes = f.read()
        default_name = os.path.splitext(os.path.basename(f.filename))[0]
    else:
        if not os.path.exists(SAMPLE_KML):
            return _render_input('The bundled Askins KML is missing from %s.' % SAMPLE_KML, 500)
        with open(SAMPLE_KML, 'rb') as fh:
            kml_bytes = fh.read()
        default_name = 'Askins'

    trail_name = (request.form.get('trail_name') or '').strip() or \
        default_name.replace('_', ' ').replace('-', ' ').title()

    try:
        stated_grade = int(request.form.get('stated_grade', 4))
    except (TypeError, ValueError):
        stated_grade = 4
    stated_grade = max(1, min(6, stated_grade))
    want_sideslope = request.form.get('want_sideslope') == '1'
    dem_id = request.form.get('dem_id') or None
    dem_source = CUSTOM_DEMS.get(dem_id)

    run_id, error, status = run_audit_and_store(kml_bytes, trail_name, stated_grade, want_sideslope, dem_source)
    if run_id is None:
        return _render_input(error, status)

    result = RUNS[run_id]
    if dem_source is not None:
        result['_dem_id'] = dem_id

    # Custom-DEM runs aren't saved to history: CUSTOM_DEMS is itself an
    # in-memory, server-restart-ephemeral cache, so a saved run referencing
    # a dem_id that's since been evicted couldn't be faithfully re-run later.
    if current_user.is_authenticated and dem_source is None:
        _save_run(run_id, current_user, trail_name, kml_bytes, stated_grade, want_sideslope, result)

    return redirect(url_for('view_run', run_id=run_id))


def _save_run(run_id, user, trail_name, kml_bytes, stated_grade, want_sideslope, result):
    """Shared by /audit's auto-save (when already signed in) and the result
    page's explicit "Save to my account" button (for a run that happened
    before signing in, or a custom-DEM run's text-only fields)."""
    pts = result.get('points_wgs84_dense') or []
    centroid_lat = sum(p[1] for p in pts) / len(pts) if pts else None
    centroid_lon = sum(p[0] for p in pts) / len(pts) if pts else None
    saved = SavedRun(
        user_id=user.id, trail_name=trail_name, kml_bytes=kml_bytes,
        stated_grade=stated_grade, want_sideslope=want_sideslope,
        overall_grade=result.get('overall_grade'), avg_gradient=result.get('avg_gradient'),
        trail_length_m=result.get('trail_length_m'), out_of_grade_pct=result.get('out_of_grade_pct'),
        centroid_lat=centroid_lat, centroid_lon=centroid_lon,
    )
    db.session.add(saved)
    db.session.commit()
    SAVED_RUN_IDS[run_id] = saved.id
    return saved


@app.route('/run/<run_id>/save', methods=['POST'])
@login_required
def save_run(run_id):
    result = RUNS.get(run_id)
    kml_bytes = RUN_KML.get(run_id)
    if result is None or kml_bytes is None:
        abort(404)
    if run_id not in SAVED_RUN_IDS:
        _save_run(run_id, current_user, result.get('trail_name', 'Trail'), kml_bytes,
                  result.get('stated_grade', 4), bool(result.get('sideslopes')), result)
    return redirect(url_for('view_run', run_id=run_id))


@app.route('/run/<run_id>/kml')
def run_kml(run_id):
    kml_bytes = RUN_KML.get(run_id)
    if kml_bytes is None:
        abort(404)
    trail_name = (RUNS.get(run_id) or {}).get('trail_name', 'trail')
    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', trail_name).strip('_') or 'trail'
    return Response(kml_bytes, mimetype='application/vnd.google-earth.kml+xml',
                     headers={'Content-Disposition': 'attachment; filename="%s.kml"' % safe_name})


@app.route('/run/<run_id>')
def view_run(run_id):
    result = RUNS.get(run_id)
    if result is None:
        return _render_input(
            'That result is no longer held in memory — the server keeps the last '
            '%d runs. Run the audit again.' % MAX_RUNS, 404)
    return render_template('result_v11.html', run_id=run_id, r=result,
                           is_saved=run_id in SAVED_RUN_IDS, has_kml=run_id in RUN_KML,
                           grade_colours=gv11.GRADE_COLOURS,
                           server_key_available=bool(_anthropic_client))


@app.route('/run/<run_id>/map')
def run_map(run_id):
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    return gv11.render_folium_map(result)


@app.route('/run/<run_id>/plot3d')
def run_plot3d(run_id):
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    dem_source = CUSTOM_DEMS.get(result.get('_dem_id'))
    try:
        return gv11.render_plot3d(result, dem_source=dem_source)
    except Exception as e:
        traceback.print_exc()
        return '3D view unavailable: %s' % e, 500


@app.route('/run/<run_id>.json')
def audit_json(run_id):
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    return jsonify({k: v for k, v in result.items()
                     if k not in ('gradient_png', 'sideslope_png')})


@app.route('/run/<run_id>/summary', methods=['POST'])
@limiter.limit("10 per hour")
def run_summary(run_id):
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    body = request.get_json(silent=True) or {}
    api_key = (body.get('api_key') or '').strip() or None
    if not api_key and not _anthropic_client:
        return jsonify({'error': 'No API key set — enter one above, or add ANTHROPIC_API_KEY to .env and restart.'}), 400

    def _idx(name, choices):
        v = body.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if 0 <= v < len(choices) else None

    field_obs = (_idx('field_obstacles', FIELD_OBSTACLES),
                 _idx('field_surface', FIELD_SURFACE),
                 _idx('field_features', FIELD_FEATURES))
    data = summarise_audit(result, api_key=api_key, field_obs=field_obs)
    if data is None:
        return jsonify({'error': 'Grading failed — see server console.'}), 500
    return jsonify(data)


@app.route('/run/<run_id>/vision', methods=['POST'])
@limiter.limit("10 per hour")
def run_vision(run_id):
    """AI trail camera prototype (see /roadmap/vision for the full
    architecture and what this isn't). Each uploaded photo is matched to a
    chainage via its own GPS EXIF, then sent to Claude vision to flag
    hazards LiDAR can't see. Video frames have no GPS of their own; if a
    companion GPX track is uploaded alongside the video, each frame's
    elapsed time (plus an optional offset, for when recording and GPX
    logging didn't start at exactly the same moment) is matched against
    the GPX track's own elapsed time to find its real position — falling
    back to linear interpolation between a manually-entered start/end
    metre range if no GPX is given. Findings accumulate on the run across
    repeat uploads rather than replacing each other."""
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    api_key = (request.form.get('api_key') or '').strip() or None
    if not api_key and not _anthropic_client:
        return jsonify({'error': 'No API key set — enter one above, or add ANTHROPIC_API_KEY to .env and restart.'}), 400

    photos = request.files.getlist('photos')
    video = request.files.get('video')
    if not photos and not (video and video.filename):
        return jsonify({'error': 'Choose at least one photo, or a video.'}), 400
    if len(photos) > MAX_VISION_PHOTOS:
        return jsonify({'error': f'Max {MAX_VISION_PHOTOS} photos per batch — try fewer at a time.'}), 400

    points = result.get('points_wgs84_dense') or []
    trail_length_m = result.get('trail_length_m')
    stated_grade = result.get('stated_grade')
    findings = []

    for f in photos:
        name = (f.filename or 'photo').lower()
        if not name.endswith(('.jpg', '.jpeg', '.png')):
            findings.append({'filename': f.filename, 'chainage_m': None, 'error': 'Not a JPEG/PNG — skipped.'})
            continue
        photo_bytes = f.read()
        media_type = 'image/png' if name.endswith('.png') else 'image/jpeg'

        gps = gv11.extract_photo_gps(photo_bytes) if media_type == 'image/jpeg' else None
        chainage_m = lat = lon = None
        if gps and points:
            lat, lon = gps
            chainage_m = gv11.nearest_chainage(points, lat, lon, max_distance_m=VISION_GPS_MAX_DISTANCE_M)

        analysis = analyze_trail_photo(photo_bytes, media_type=media_type, api_key=api_key,
                                        stated_grade=stated_grade)
        if analysis is None:
            findings.append({'filename': f.filename, 'chainage_m': chainage_m,
                             'error': 'Analysis failed — see server console.'})
            continue

        findings.append({
            'filename': f.filename, 'chainage_m': chainage_m, 'lat': lat, 'lon': lon,
            'gps_found': gps is not None, **analysis,
        })

    if video and video.filename:
        name = video.filename.lower()
        if not name.endswith(('.mp4', '.mov', '.m4v', '.webm', '.avi')):
            findings.append({'filename': video.filename, 'chainage_m': None,
                             'error': 'Not a recognised video format — skipped.'})
        else:
            # Prefer a real GPS match via a companion GPX track, if one was
            # uploaded alongside the video — see gps_at_time/parse_gpx_track.
            # Falls back to linear interpolation between a manually-entered
            # start/end chainage if no GPX is given, or no GPX file at all
            # (frames just get no chainage, same as a photo with no GPS).
            def _parse_m(key):
                try:
                    v = float(request.form.get(key, ''))
                except (TypeError, ValueError):
                    return None
                return max(0.0, min(v, trail_length_m)) if trail_length_m else v
            start_m, end_m = _parse_m('video_start_m'), _parse_m('video_end_m')

            try:
                offset_s = float(request.form.get('video_gpx_offset_s', '') or 0)
            except (TypeError, ValueError):
                offset_s = 0.0

            gpx_file = request.files.get('gpx')
            gpx_track = gpx_error = None
            if gpx_file and gpx_file.filename:
                try:
                    gpx_track = gv11.parse_gpx_track(gpx_file.read())
                except Exception as e:
                    gpx_error = str(e)
                    findings.append({'filename': gpx_file.filename, 'chainage_m': None,
                                     'error': f'Could not use that GPX track: {e}'})

            try:
                frames = gv11.extract_video_frames(video.read(), filename_hint=video.filename,
                                                   max_frames=MAX_VISION_PHOTOS)
            except Exception as e:
                traceback.print_exc()
                frames = []
                findings.append({'filename': video.filename, 'chainage_m': None,
                                 'error': f'Could not read that video: {e}'})

            for i, (jpeg_bytes, fraction, elapsed_s) in enumerate(frames):
                chainage_m = lat = lon = None
                gps_found = False
                if gpx_track:
                    target_ts = gpx_track[0][0] + offset_s + elapsed_s
                    lat, lon = gv11.gps_at_time(gpx_track, target_ts)
                    chainage_m = (gv11.nearest_chainage(points, lat, lon, max_distance_m=VISION_GPS_MAX_DISTANCE_M)
                                  if points else None)
                    gps_found = True
                elif start_m is not None and end_m is not None:
                    chainage_m = round(start_m + fraction * (end_m - start_m))
                analysis = analyze_trail_photo(jpeg_bytes, media_type='image/jpeg', api_key=api_key,
                                                stated_grade=stated_grade)
                label = f"{video.filename} @ {round(fraction * 100)}%"
                if analysis is None:
                    findings.append({'filename': label, 'chainage_m': chainage_m,
                                     'error': 'Analysis failed — see server console.'})
                    continue
                findings.append({
                    'filename': label, 'chainage_m': chainage_m, 'lat': lat, 'lon': lon,
                    'gps_found': gps_found, **analysis,
                })

    result.setdefault('vision_findings', []).extend(findings)
    return jsonify({'findings': findings})


def open_browser():
    webbrowser.open('http://127.0.0.1:5000')


if __name__ == '__main__':
    threading.Timer(1.5, open_browser).start()
    # threaded=True: a live audit run blocks on thousands of LINZ API calls
    # (a minute or more) — without this the whole server would stall for
    # anyone else hitting it meanwhile.
    app.run(debug=False, port=5000, threaded=True)
