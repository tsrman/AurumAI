from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkflowTemplateError(Exception):
    code: str
    status_code: int
