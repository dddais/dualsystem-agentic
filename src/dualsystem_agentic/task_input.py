"""A finalized web instruction, distinct from a terminal target name."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskInput:
    instruction: str
    target_queries: list[str] | None = None
    target: str | None = None

    def __post_init__(self):
        if not isinstance(self.instruction, str) or not self.instruction.strip() or len(self.instruction) > 2000:
            raise ValueError("instruction must be nonempty text (maximum 2000 characters)")
        if self.target is not None and (not isinstance(self.target, str) or not self.target.strip() or len(self.target) > 200):
            raise ValueError("target must be a nonempty object name")
        if self.target_queries is not None and (
            not isinstance(self.target_queries, list) or not 1 <= len(self.target_queries) <= 8 or any(
                not isinstance(q, str) or not q.strip() or len(q) > 200 for q in self.target_queries
            )
        ):
            raise ValueError("target_queries must contain 1..8 nonempty object names")

    @classmethod
    def from_payload(cls, payload):
        return cls(payload["instruction"], payload.get("target_queries"), payload.get("target"))
