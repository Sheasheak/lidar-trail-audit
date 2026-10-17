"""Pre-October 2026 snapshot of TrailSafe (commit e2d83c7, 2026-09-17) — the
exact version that was live when the PM Space Prize application was
submitted on 30 Sept 2026. Kept running under /legacy, fully isolated from
the current app's code/templates/static files (own grader_v11.py, config.py,
grader.py, grader3d.py, templates/, static/), so judges reviewing the
submission see exactly what was submitted regardless of what's changed in
the current version since. Not a route for general use — linked from the
landing page as "(for Space Prize judges)".

Deliberately NOT edited beyond what import isolation requires: the four
grading modules here are untouched except for converting their absolute
"from config import ..." style imports to relative ones so they resolve to
this package's own copies instead of colliding with the current app's
same-named modules. No logic was changed.
"""
import json
import os
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for

try:
    import anthropic
except ImportError:
    anthropic = None

from . import grader_v11 as gv11

legacy_bp = Blueprint("legacy", __name__,
                       template_folder="templates", static_folder="static",
                       static_url_path="/static")

_LEGACY_ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(_LEGACY_ROOT))
SAMPLE_KML = os.path.join(REPO_ROOT, "data", "askins.kml")

SUMMARY_MODEL = "claude-opus-5"
_anthropic_client = anthropic.Anthropic() if (anthropic and os.environ.get("ANTHROPIC_API_KEY")) else None

FIELD_OBSTACLES = ["None", "Up to 50mm", "Up to 100mm", "Up to 200mm", "Up to 600mm", "No limit / bigger"]
FIELD_SURFACE = ["Hardened and smooth (sealed)", "Firm and stable (compacted gravel)",
                  "Mostly stable, some loose sections", "Generally firm but loose/broken in places",
                  "Highly variable — natural surface, mud, ruts"]
FIELD_FEATURES = ["All rollable — no jumps or drops", "Rollable — stepped drops up to 200mm",
                   "Rollable — stepped drops up to 400mm, small tabletops",
                   "Mostly rollable — drops up to 600mm, gap jumps with B-lines",
                   "Stepped drops up to 1.5m, large jumps", "No limits"]


def summarise_audit(result, api_key=None, field_obs=None):
    client = anthropic.Anthropic(api_key=api_key) if (anthropic and api_key) else _anthropic_client
    if not client:
        return None
    lidar_grade = result["overall_grade"]
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
        "trail_name": result["trail_name"],
        "stated_grade": f"{result['stated_grade']} ({gv11.GRADE_COLOURS[result['stated_grade']]})",
        "lidar_measured_grade": f"{lidar_grade} ({gv11.GRADE_COLOURS[lidar_grade]})",
        "avg_gradient_pct": result["avg_gradient"],
        "steepest_gradient_pct": result["worst_gradient"],
        "gradient_grade": result["gradient_grade"],
        "gradient_finding": result["gradient_reason"],
        "corner_grade": result["radius_grade"],
        "corner_finding": result["radius_reason"],
        "tightest_corner_m": result["tightest_corner"],
        "pct_corners_under_stated_min": result["pct_under_stated_min"],
        "mean_cross_slope_deg": result["mean_abs_sideslope"],
        "pct_trail_over_30deg_sideslope": result["pct_steep_sideslope"],
        "steep_sideslope_sections_m": [
            f"{r['start_m']}-{r['end_m']}m (peak {r['peak_deg']}°)"
            for r in result.get("sideslope_exceedances", [])
        ],
    }
    field_note = ""
    if obstacles is not None:
        audit_data["field_tread_obstacles"] = FIELD_OBSTACLES[obstacles]
    if surface is not None:
        audit_data["field_trail_surface"] = FIELD_SURFACE[surface]
    if features is not None:
        audit_data["field_technical_features"] = FIELD_FEATURES[features]
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
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text[4:] if text.startswith("json") else text
        data = json.loads(text.strip())
        grade = int(data["grade"])
        return {
            "grade": max(1, min(6, grade)),
            "grade_name": gv11.GRADE_COLOURS[max(1, min(6, grade))],
            "summary": data["summary"],
            "recommendation": data["recommendation"],
        }
    except Exception:
        traceback.print_exc()
        return None


RUNS = OrderedDict()
RUN_KML = OrderedDict()
MAX_RUNS = 12
CUSTOM_DEMS = OrderedDict()
MAX_DEMS = 6


def _render_input(error=None, status=200):
    return render_template("legacy/input.html", error=error), status


@legacy_bp.route("/")
def index():
    return render_template("legacy/editor.html")


@legacy_bp.route("/editor")
def editor():
    return render_template("legacy/editor.html")


@legacy_bp.route("/editor/terrain", methods=["POST"])
def editor_terrain():
    body = request.get_json(silent=True) or {}
    latlngs = body.get("latlngs")
    if not latlngs or len(latlngs) < 2:
        return jsonify({"error": "No trail line given."}), 400
    dem_source = CUSTOM_DEMS.get(body.get("dem_id"))
    points_wgs84 = [(lon, lat) for lat, lon in latlngs]
    try:
        overlays = gv11.terrain_overlays(points_wgs84, dem_source=dem_source)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    return jsonify(overlays)


@legacy_bp.route("/editor/dem", methods=["POST"])
def editor_dem():
    f = request.files.get("dem")
    if not f or not f.filename:
        return jsonify({"error": "Choose a DEM file (GeoTIFF)."}), 400
    if not f.filename.lower().endswith((".tif", ".tiff")):
        return jsonify({"error": 'That file is not a GeoTIFF (.tif/.tiff) — got "%s".' % f.filename}), 400
    try:
        loaded = gv11.load_uploaded_dem(f.read())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Could not read that file: %s" % e}), 400
    dem_id = uuid.uuid4().hex[:10]
    CUSTOM_DEMS[dem_id] = loaded
    while len(CUSTOM_DEMS) > MAX_DEMS:
        CUSTOM_DEMS.popitem(last=False)
    return jsonify({"dem_id": dem_id})


@legacy_bp.route("/grade")
def grade_form():
    return _render_input()


@legacy_bp.route("/audit", methods=["POST"])
def audit():
    source = request.form.get("source", "sample")
    if source == "upload":
        f = request.files.get("kml")
        if not f or not f.filename:
            return _render_input("Choose a KML file, or switch back to the bundled Askins line.", 400)
        if not f.filename.lower().endswith(".kml"):
            return _render_input('That file is not a .kml — got "%s".' % f.filename, 400)
        kml_bytes = f.read()
        default_name = os.path.splitext(os.path.basename(f.filename))[0]
    else:
        if not os.path.exists(SAMPLE_KML):
            return _render_input("The bundled Askins KML is missing from %s." % SAMPLE_KML, 500)
        with open(SAMPLE_KML, "rb") as fh:
            kml_bytes = fh.read()
        default_name = "Askins"

    trail_name = (request.form.get("trail_name") or "").strip() or \
        default_name.replace("_", " ").replace("-", " ").title()

    try:
        stated_grade = int(request.form.get("stated_grade", 4))
    except (TypeError, ValueError):
        stated_grade = 4
    stated_grade = max(1, min(6, stated_grade))
    want_sideslope = request.form.get("want_sideslope") == "1"
    dem_id = request.form.get("dem_id") or None
    dem_source = CUSTOM_DEMS.get(dem_id)

    try:
        result = gv11.run_audit(kml_bytes, trail_name=trail_name, stated_grade=stated_grade,
                                want_sideslope=want_sideslope, dem_source=dem_source)
    except ValueError as e:
        return _render_input(str(e), 400)
    except Exception as e:
        traceback.print_exc()
        return _render_input("Processing failed: %s" % e, 500)

    result["run_at"] = datetime.now().strftime("%d %b %Y, %H:%M")
    if dem_source is not None:
        result["_dem_id"] = dem_id

    run_id = uuid.uuid4().hex[:10]
    RUNS[run_id] = result
    while len(RUNS) > MAX_RUNS:
        RUNS.popitem(last=False)
    RUN_KML[run_id] = kml_bytes
    while len(RUN_KML) > MAX_RUNS:
        RUN_KML.popitem(last=False)
    return redirect(url_for("legacy.view_run", run_id=run_id))


@legacy_bp.route("/run/<run_id>")
def view_run(run_id):
    result = RUNS.get(run_id)
    if result is None:
        return _render_input(
            "That result is no longer held in memory — the server keeps the last "
            "%d runs. Run the audit again." % MAX_RUNS, 404)
    return render_template("legacy/result_v11.html", run_id=run_id, r=result,
                           grade_colours=gv11.GRADE_COLOURS,
                           server_key_available=bool(_anthropic_client))


@legacy_bp.route("/run/<run_id>/map")
def run_map(run_id):
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    return gv11.render_folium_map(result)


@legacy_bp.route("/run/<run_id>/plot3d")
def run_plot3d(run_id):
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    dem_source = CUSTOM_DEMS.get(result.get("_dem_id"))
    try:
        return gv11.render_plot3d(result, dem_source=dem_source)
    except Exception as e:
        traceback.print_exc()
        return "3D view unavailable: %s" % e, 500


@legacy_bp.route("/run/<run_id>.json")
def audit_json(run_id):
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    return jsonify({k: v for k, v in result.items()
                     if k not in ("gradient_png", "sideslope_png")})


@legacy_bp.route("/run/<run_id>/summary", methods=["POST"])
def run_summary(run_id):
    result = RUNS.get(run_id)
    if result is None:
        abort(404)
    body = request.get_json(silent=True) or {}
    api_key = (body.get("api_key") or "").strip() or None
    if not api_key and not _anthropic_client:
        return jsonify({"error": "No API key set — enter one above, or add ANTHROPIC_API_KEY to .env and restart."}), 400

    def _idx(name, choices):
        v = body.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None
        return v if 0 <= v < len(choices) else None

    field_obs = (_idx("field_obstacles", FIELD_OBSTACLES),
                 _idx("field_surface", FIELD_SURFACE),
                 _idx("field_features", FIELD_FEATURES))
    data = summarise_audit(result, api_key=api_key, field_obs=field_obs)
    if data is None:
        return jsonify({"error": "Grading failed — see server console."}), 500
    return jsonify(data)
