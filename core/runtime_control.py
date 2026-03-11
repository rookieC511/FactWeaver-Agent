from __future__ import annotations

import os


INTERRUPT_POINT_ENV = "FACTWEAVER_INTERRUPT_POINT"
INTERRUPT_TASK_ID_ENV = "FACTWEAVER_INTERRUPT_TASK_ID"
INTERRUPT_EXIT_CODE_ENV = "FACTWEAVER_INTERRUPT_EXIT_CODE"


def configured_interrupt_point() -> str:
    return os.getenv(INTERRUPT_POINT_ENV, "").strip().lower()


def should_interrupt_task(task_id: str | None, point: str) -> bool:
    configured_point = configured_interrupt_point()
    if not configured_point or configured_point != point.strip().lower():
        return False

    configured_task_id = os.getenv(INTERRUPT_TASK_ID_ENV, "").strip()
    if configured_task_id and configured_task_id != (task_id or ""):
        return False
    return True


def interrupt_exit_code() -> int:
    try:
        return int(os.getenv(INTERRUPT_EXIT_CODE_ENV, "95"))
    except ValueError:
        return 95


def crash_process() -> None:
    os._exit(interrupt_exit_code())
