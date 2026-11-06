"""Back-compat shim — police mode lives in light_effects now."""

from light_effects import LightEffectController, LightSnapshot, light_effects

# Historical name used by earlier app.py imports
PoliceModeController = LightEffectController
police_mode = light_effects

__all__ = [
    "LightSnapshot",
    "LightEffectController",
    "PoliceModeController",
    "police_mode",
    "light_effects",
]
