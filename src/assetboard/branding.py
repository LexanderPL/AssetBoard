"""Public product and environment branding constants."""

APP_NAME = "AssetBoard"
ENV_PREFIX = "ASSETBOARD"
CLI_NAME = "assetboard"
USER_AGENT = APP_NAME
# The GitHub repository whose releases the opt-in update check reads (ADR-0023).
GITHUB_REPO = "LexanderPL/AssetBoard"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASE_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/tag/{{tag}}"
