"""clipbot — turn a request about a recording into a validated edit plan.

Pipeline: probe -> captions -> select -> plan. The renderer (render/) never
sees anything but the plan; see contract/README.md.
"""

__all__ = ["probe", "captions", "select", "plan"]
