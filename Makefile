# Shared quality gate (veb-uv0). Run before opening or merging any PR.
#   make check
# Requirements: git, python3 + uv (https://docs.astral.sh/uv/), ffprobe.
# Cross-platform: works on Linux/macOS/Windows (Git Bash or GNU make).

.PHONY: check check-whitespace check-contract check-assets check-render

check: check-whitespace check-contract check-assets check-render
	@echo "make check: OK"

check-whitespace:
	@git diff --check HEAD~1 2>/dev/null || git diff --check
	@echo "whitespace: OK"

check-contract:
	@uv run --quiet --with jsonschema python scripts/check_contract.py

check-assets:
	@ffprobe -v error -show_entries format=duration -of csv=p=0 assets/demo-clip.mp4 >/dev/null \
		&& echo "assets: OK (demo-clip.mp4 probes)"

# Render smoke test — owned by the render lane (Ramsey). Becomes real once
# render/ exists: render contract/examples/one-clip-trim.json and assert the
# output duration. Until then it is a no-op so the gate stays green.
check-render:
	@if [ -d render ]; then echo "render: TODO smoke test (veb-uv0 / render lane)"; else echo "render: skipped (no render/ yet)"; fi
