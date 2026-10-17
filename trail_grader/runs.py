"""The in-memory finished-run cache, and the run-audit-and-store helper that
both the main /audit POST and auth.rerun (re-running a saved history entry)
need — factored out here so auth.py doesn't have to import app.py (which
imports auth.py to register its blueprint)."""
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime

import grader_v11 as gv11

# Finished runs, newest last. Bounded so a long session can't eat memory —
# this is a live cache for serving /run/<id>/map, /plot3d etc., not
# long-term storage (that's what SavedRun in models.py is for).
RUNS = OrderedDict()
RUN_KML = OrderedDict()
MAX_RUNS = 12

# run_id -> SavedRun.id, for runs saved to an account — lets the result page
# show "Saved ✓" instead of a Save button without a DB round trip on
# every view. UI nicety only, not authoritative: resets on restart same as
# RUNS/RUN_KML, so worst case after a redeploy the button offers to save
# again rather than silently refusing to.
SAVED_RUN_IDS = {}


def run_audit_and_store(kml_bytes, trail_name, stated_grade, want_sideslope, dem_source=None):
    """Runs the audit pipeline and caches the result under a new run_id.
    Returns (run_id, None, 200) on success, or (None, error_message, status)
    on failure — status distinguishes a bad trail line (400) from an
    unexpected pipeline failure (500), same as the original /audit route."""
    try:
        result = gv11.run_audit(kml_bytes, trail_name=trail_name, stated_grade=stated_grade,
                                want_sideslope=want_sideslope, dem_source=dem_source)
    except ValueError as e:
        return None, str(e), 400
    except Exception as e:
        traceback.print_exc()
        return None, 'Processing failed: %s' % e, 500

    result['run_at'] = datetime.now().strftime('%d %b %Y, %H:%M')
    run_id = uuid.uuid4().hex[:10]
    RUNS[run_id] = result
    while len(RUNS) > MAX_RUNS:
        RUNS.popitem(last=False)
    RUN_KML[run_id] = kml_bytes
    while len(RUN_KML) > MAX_RUNS:
        RUN_KML.popitem(last=False)
    return run_id, None, 200
