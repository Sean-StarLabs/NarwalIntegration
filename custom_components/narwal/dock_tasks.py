"""Narwal dock task definitions and command gates."""

from __future__ import annotations

from dataclasses import dataclass

from .narwal_client.const import ACTIVE_CLEANING_STATUSES, WorkingStatus
from .narwal_client.models import (
    DOCK_TASK_DRY_DOCK_BAG,
    DOCK_TASK_DRY_DUST_BIN,
    DOCK_TASK_DRY_MOP,
    DOCK_TASK_EMPTY_DUSTBIN,
    DOCK_TASK_WASH_MOP,
    NarwalState,
)


@dataclass(frozen=True)
class DockTaskDefinition:
    """Description of one supported dock task."""

    key: str
    translation_key: str
    action: str
    icon: str


DOCK_TASKS: tuple[DockTaskDefinition, ...] = (
    DockTaskDefinition(
        key=DOCK_TASK_EMPTY_DUSTBIN,
        translation_key=DOCK_TASK_EMPTY_DUSTBIN,
        action="empty_dustbin",
        icon="mdi:delete-empty",
    ),
    DockTaskDefinition(
        key=DOCK_TASK_WASH_MOP,
        translation_key=DOCK_TASK_WASH_MOP,
        action="wash_mop",
        icon="mdi:waves-arrow-up",
    ),
    DockTaskDefinition(
        key=DOCK_TASK_DRY_MOP,
        translation_key=DOCK_TASK_DRY_MOP,
        action="dry_mop",
        icon="mdi:fan",
    ),
    DockTaskDefinition(
        key=DOCK_TASK_DRY_DUST_BIN,
        translation_key=DOCK_TASK_DRY_DUST_BIN,
        action="dry_dust_bag",
        icon="mdi:air-filter",
    ),
    DockTaskDefinition(
        key=DOCK_TASK_DRY_DOCK_BAG,
        translation_key=DOCK_TASK_DRY_DOCK_BAG,
        action="dry_station_bag",
        icon="mdi:shield-sun-outline",
    ),
)

GENERIC_STOP_DOCK_TASKS = frozenset(
    {
        DOCK_TASK_EMPTY_DUSTBIN,
        DOCK_TASK_WASH_MOP,
        DOCK_TASK_DRY_MOP,
    }
)
SCOPED_STOP_DOCK_TASKS = frozenset({DOCK_TASK_DRY_DOCK_BAG})
STOPPABLE_DOCK_TASKS = GENERIC_STOP_DOCK_TASKS | SCOPED_STOP_DOCK_TASKS


def has_blocking_error(state: NarwalState | None) -> bool:
    """Return True when the robot reports a fault that should block commands."""
    return state is None or state.working_status == WorkingStatus.ERROR or state.has_error


def is_clean_session_context(state: NarwalState | None) -> bool:
    """Return True while robot-side clean task context is still current."""
    if state is None:
        return False
    return (
        state.is_cleaning
        or state.has_assumed_robot_clean
        or state.working_status in ACTIVE_CLEANING_STATUSES
        or state.working_status == WorkingStatus.TASK_COMPLETED
        or state.has_recent_active_working_status
        or state.is_returning
    )


def can_start_dock_task(state: NarwalState | None, task_key: str | None = None) -> bool:
    """Return True when a dock task can be started safely."""
    if has_blocking_error(state):
        return False
    if state.working_status == WorkingStatus.UNKNOWN:
        return False
    if not state.is_docked or is_clean_session_context(state):
        return False
    if state.has_unmapped_active_dock_task:
        return False
    if state.assumed_active_dock_task is not None:
        return False
    if task_key is None:
        return not state.active_dock_task_keys
    active_keys = set(state.active_dock_task_keys)
    if task_key in active_keys:
        return False
    # Default conservative policy: do not expose parallel starts until hardware
    # testing verifies an exact task combination.
    return not active_keys


def can_start_robot_clean(state: NarwalState | None) -> bool:
    """Return True when reported state permits sending a new robot clean."""
    if has_blocking_error(state):
        return False
    if state.working_status == WorkingStatus.UNKNOWN:
        return False
    if state.has_assumed_robot_clean:
        return False
    return not state.blocks_robot_start_for_dock_task


def can_stop_dock_task(state: NarwalState | None, task_key: str | None = None) -> bool:
    """Return True when a dock task can be stopped without ambiguity."""
    if has_blocking_error(state):
        return False
    if state.working_status == WorkingStatus.UNKNOWN:
        return False
    if state.has_unmapped_active_dock_task:
        return False
    active_keys = state.active_dock_task_keys
    if not active_keys:
        return False
    if is_clean_session_context(state):
        return task_key in SCOPED_STOP_DOCK_TASKS and task_key in active_keys
    active_key_set = set(active_keys)
    if task_key is None:
        return (
            len(active_key_set) == 1
            and next(iter(active_key_set)) in STOPPABLE_DOCK_TASKS
        )
    if task_key not in active_key_set or task_key not in STOPPABLE_DOCK_TASKS:
        return False
    if task_key in SCOPED_STOP_DOCK_TASKS:
        return True
    return active_key_set == {task_key}
