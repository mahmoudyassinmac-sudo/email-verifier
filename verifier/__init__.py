"""Email list verifier - 6-stage pipeline (clean, syntax, disposable, role, MX, SMTP)."""
from .config import Settings
from .engine import VerifierEngine

__version__ = "1.0.0"
__all__ = ["Settings", "VerifierEngine", "__version__"]
