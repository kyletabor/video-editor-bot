# Shared quality gate (veb-uv0). PowerShell users can run the command directly.
# Setup and pinned FFmpeg: docs/checks.md
UV ?= uv
CHECK = $(UV) run --locked scripts/check.py $(CHECK_FLAGS)

.PHONY: check check-whitespace check-contract check-assets check-bot check-render check-ffmpeg
check:
	@$(CHECK)

check-whitespace:
	@$(CHECK) whitespace

check-contract:
	@$(CHECK) contract

check-assets:
	@$(CHECK) assets

check-bot:
	@$(CHECK) bot

check-render:
	@$(CHECK) render

check-ffmpeg:
	@$(CHECK) ffmpeg
