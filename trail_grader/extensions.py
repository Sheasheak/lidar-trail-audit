"""Shared extension instances, created here (not in app.py) so auth.py can
use the same rate limiter without importing app.py and creating a circular
import (app.py already imports auth.py to register its blueprint)."""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["200 per day", "50 per hour"])
