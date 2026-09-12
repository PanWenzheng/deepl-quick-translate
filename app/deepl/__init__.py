"""DeepL 调用。"""

from .client import DeepLClient, TranslationResult
from .errors import TranslationError
from .service import TranslationService, detect_direction

__all__ = [
    "DeepLClient",
    "TranslationResult",
    "TranslationError",
    "TranslationService",
    "detect_direction",
]
