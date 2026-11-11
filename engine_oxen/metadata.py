"""Identity constants. Standard library only.

The TUI imports this module, not ``engine.py``, so reading the provider's
display name never pulls in the provider's SDK.
"""

PROVIDER_NAME = "Oxen.ai"
PROVIDER_HOMEPAGE = "https://oxen.ai"
PLATFORM = "oxen"
API_KEY_ENV_VAR = "OXEN_API_KEY"
API_KEY_PATTERN = r".+"
