"""Google Tasks service layer — standalone.

Domain models and service for Google Tasks operations.
Zero imports from Backend_FastAPI.
"""

from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from shared.datetime_utils import (
    compute_task_window,
    normalize_rfc3339,
    parse_rfc3339_datetime,
    parse_time_string,
)
from shared.google_auth import get_tasks_service


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TaskListInfo:
    """Metadata describing a Google Task list."""

    id: str
    title: str
    updated: Optional[str] = None


@dataclass(slots=True)
class Task:
    """Representation of a user task independent from scheduling."""

    title: str
    status: str
    list_title: str
    list_id: str
    id: str
    due: Optional[datetime.datetime] = None
    notes: Optional[str] = None
    updated: Optional[str] = None
    completed: Optional[str] = None
    web_link: Optional[str] = None
    parent: Optional[str] = None
    position: Optional[str] = None

    @property
    def is_scheduled(self) -> bool:
        """Return True when the task has an associated due datetime."""
        return self.due is not None


@dataclass(slots=True)
class ScheduledTask:
    """Scheduled task included in calendar views."""

    title: str
    due: datetime.datetime
    due_display: str
    status: str
    list_title: str
    list_id: str
    id: str
    notes: Optional[str] = None
    updated: Optional[str] = None
    completed: Optional[str] = None
    web_link: Optional[str] = None
    is_overdue: bool = False


@dataclass(slots=True)
class TaskSearchResult:
    """Result structure for keyword-based task searches."""

    title: str
    status: str
    list_title: str
    list_id: str
    id: str
    due: Optional[str] = None
    updated: Optional[str] = None
    completed: Optional[str] = None
    notes: Optional[str] = None
    web_link: Optional[str] = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TaskServiceError(RuntimeError):
    """Raised when an unexpected error occurs while interacting with the Tasks API."""


class TaskAuthorizationError(TaskServiceError):
    """Raised when the user has not authorized Google Tasks access."""


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScheduledTaskCollection:
    """Container for scheduled task results."""

    tasks: List[ScheduledTask]
    warnings: List[str]
    remaining: int


@dataclass(slots=True)
class TaskSearchResponse:
    """Container for keyword-based task search results."""

    matches: List[TaskSearchResult]
    warnings: List[str]
    scanned_lists: List[str]
    truncated: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class TaskService:
    """Manage Google Task operations and convert API responses into domain models."""

    def __init__(
        self,
        user_email: str,
        *,
        service_factory: Callable[[str], Any] | None = None,
    ):
        self._user_email = user_email
        self._client: Any | None = None
        self._service_factory: Callable[[str], Any] = (
            service_factory if service_factory is not None else get_tasks_service
        )

    def _client_or_raise(self) -> Any:
        if self._client is None:
            try:
                self._client = self._service_factory(self._user_email)
            except ValueError as exc:
                raise TaskAuthorizationError(str(exc)) from exc
            except Exception as exc:
                raise TaskServiceError(str(exc)) from exc
        return self._client

    @staticmethod
    async def _execute(call: Any) -> dict[str, Any]:
        return await asyncio.to_thread(call.execute)

    # ---- Task lists -------------------------------------------------------

    async def list_task_lists(
        self, max_results: int = 100, page_token: Optional[str] = None
    ) -> tuple[List[TaskListInfo], Optional[str]]:
        """List Google Task lists available to the user."""
        client = self._client_or_raise()
        params: dict[str, Any] = {"maxResults": max(1, min(max_results, 100))}
        if page_token:
            params["pageToken"] = page_token

        try:
            response = await self._execute(client.tasklists().list(**params))
        except Exception as exc:
            raise TaskServiceError(f"Error listing task lists: {exc}") from exc

        items = [
            TaskListInfo(
                id=item.get("id", ""),
                title=item.get("title", "(Untitled list)"),
                updated=item.get("updated"),
            )
            for item in response.get("items", [])
            if item.get("id")
        ]
        return items, response.get("nextPageToken")

    async def get_task_list(self, task_list_id: str) -> TaskListInfo:
        """Get a single task list by ID."""
        client = self._client_or_raise()
        try:
            response = await self._execute(
                client.tasklists().get(tasklist=task_list_id)
            )
        except Exception as exc:
            raise TaskServiceError(
                f"Error retrieving task list '{task_list_id}': {exc}"
            ) from exc

        return TaskListInfo(
            id=response.get("id", task_list_id),
            title=response.get("title", "(Untitled list)"),
            updated=response.get("updated"),
        )

    async def delete_task_list(self, task_list_id: str) -> None:
        """Delete a task list by ID."""
        client = self._client_or_raise()
        try:
            await self._execute(client.tasklists().delete(tasklist=task_list_id))
        except Exception as exc:
            raise TaskServiceError(
                f"Error deleting task list {task_list_id}: {exc}"
            ) from exc

    # ---- Tasks ------------------------------------------------------------

    async def list_tasks(
        self,
        task_list_id: str,
        *,
        max_results: int = 50,
        page_token: Optional[str] = None,
        show_completed: bool = False,
        show_deleted: bool = False,
        show_hidden: bool = False,
        due_min: Optional[str] = None,
        due_max: Optional[str] = None,
    ) -> tuple[List[Task], Optional[str]]:
        """List tasks from a task list."""
        client = self._client_or_raise()

        due_min_rfc = parse_time_string(due_min) if due_min else None
        due_max_rfc = parse_time_string(due_max) if due_max else None

        params: dict[str, Any] = {
            "tasklist": task_list_id,
            "maxResults": max(1, min(max_results, 100)),
            "showCompleted": show_completed,
            "showDeleted": show_deleted,
            "showHidden": show_hidden,
        }

        if due_min_rfc:
            params["dueMin"] = due_min_rfc
        elif due_min:
            params["dueMin"] = due_min

        if due_max_rfc:
            params["dueMax"] = due_max_rfc
        elif due_max:
            params["dueMax"] = due_max

        if page_token:
            params["pageToken"] = page_token

        try:
            response = await self._execute(client.tasks().list(**params))
        except Exception as exc:
            raise TaskServiceError(f"Error listing tasks: {exc}") from exc

        try:
            list_info = await self.get_task_list(task_list_id)
        except TaskServiceError:
            list_info = TaskListInfo(id=task_list_id, title=task_list_id)

        tasks = [
            self._task_from_item(item, list_info)
            for item in response.get("items", [])
            if item.get("id")
        ]
        return tasks, response.get("nextPageToken")

    async def get_task(self, task_list_id: str, task_id: str) -> Task:
        """Retrieve a specific task by ID."""
        client = self._client_or_raise()
        try:
            response = await self._execute(
                client.tasks().get(tasklist=task_list_id, task=task_id)
            )
        except Exception as exc:
            raise TaskServiceError(f"Error retrieving task {task_id}: {exc}") from exc

        try:
            list_info = await self.get_task_list(task_list_id)
        except TaskServiceError:
            list_info = TaskListInfo(id=task_list_id, title=task_list_id)

        return self._task_from_item(response, list_info)

    async def create_task(
        self,
        task_list_id: str,
        *,
        title: str,
        notes: Optional[str] = None,
        due: Optional[str] = None,
        parent: Optional[str] = None,
        previous: Optional[str] = None,
    ) -> Task:
        """Create a new task."""
        client = self._client_or_raise()
        body: dict[str, Any] = {"title": title}

        if notes is not None:
            body["notes"] = notes
        if due:
            body["due"] = parse_time_string(due) or due

        params: dict[str, Any] = {"tasklist": task_list_id, "body": body}
        if parent:
            params["parent"] = parent
        if previous:
            params["previous"] = previous

        try:
            response = await self._execute(client.tasks().insert(**params))
        except Exception as exc:
            raise TaskServiceError(f"Error creating task: {exc}") from exc

        try:
            list_info = await self.get_task_list(task_list_id)
        except TaskServiceError:
            list_info = TaskListInfo(id=task_list_id, title=task_list_id)

        return self._task_from_item(response, list_info)

    async def update_task(
        self,
        task_list_id: str,
        task_id: str,
        *,
        title: Optional[str] = None,
        notes: Optional[str] = None,
        status: Optional[str] = None,
        due: Optional[str] = None,
    ) -> Task:
        """Update an existing task."""
        client = self._client_or_raise()

        try:
            current = await self._execute(
                client.tasks().get(tasklist=task_list_id, task=task_id)
            )
        except Exception as exc:
            raise TaskServiceError(
                f"Error retrieving task {task_id} before update: {exc}"
            ) from exc

        body: dict[str, Any] = {
            "id": task_id,
            "title": title if title is not None else current.get("title", ""),
            "status": status
            if status is not None
            else current.get("status", "needsAction"),
        }

        if notes is not None:
            body["notes"] = notes
        elif current.get("notes") is not None:
            body["notes"] = current["notes"]

        if due is not None:
            body["due"] = parse_time_string(due) or due
        elif current.get("due") is not None:
            body["due"] = current["due"]

        try:
            response = await self._execute(
                client.tasks().update(tasklist=task_list_id, task=task_id, body=body)
            )
        except Exception as exc:
            raise TaskServiceError(f"Error updating task {task_id}: {exc}") from exc

        try:
            list_info = await self.get_task_list(task_list_id)
        except TaskServiceError:
            list_info = TaskListInfo(id=task_list_id, title=task_list_id)

        return self._task_from_item(response, list_info)

    async def delete_task(self, task_list_id: str, task_id: str) -> None:
        """Delete a task."""
        client = self._client_or_raise()
        try:
            await self._execute(
                client.tasks().delete(tasklist=task_list_id, task=task_id)
            )
        except Exception as exc:
            raise TaskServiceError(f"Error deleting task {task_id}: {exc}") from exc

    async def move_task(
        self,
        task_list_id: str,
        task_id: str,
        *,
        parent: Optional[str] = None,
        previous: Optional[str] = None,
        destination_task_list: Optional[str] = None,
    ) -> Task:
        """Move or reparent a task within or between lists."""
        client = self._client_or_raise()

        params: dict[str, Any] = {"tasklist": task_list_id, "task": task_id}
        if parent:
            params["parent"] = parent
        if previous:
            params["previous"] = previous
        if destination_task_list:
            params["destinationTasklist"] = destination_task_list

        try:
            response = await self._execute(client.tasks().move(**params))
        except Exception as exc:
            raise TaskServiceError(f"Error moving task {task_id}: {exc}") from exc

        try:
            list_info = await self.get_task_list(destination_task_list or task_list_id)
        except TaskServiceError:
            list_info = TaskListInfo(
                id=destination_task_list or task_list_id,
                title=destination_task_list or task_list_id,
            )

        return self._task_from_item(response, list_info)

    async def clear_completed_tasks(self, task_list_id: str) -> None:
        """Clear all completed tasks from a task list."""
        client = self._client_or_raise()
        try:
            await self._execute(client.tasks().clear(tasklist=task_list_id))
        except Exception as exc:
            raise TaskServiceError(
                f"Error clearing completed tasks for list {task_list_id}: {exc}"
            ) from exc

    # ---- Scheduled tasks --------------------------------------------------

    async def collect_scheduled_tasks(
        self,
        time_min_rfc: Optional[str],
        time_max_rfc: Optional[str],
        max_results: Optional[int],
    ) -> ScheduledTaskCollection:
        """Collect scheduled tasks within a time window."""
        client = self._client_or_raise()

        _, end_dt, past_due_cutoff = compute_task_window(time_min_rfc, time_max_rfc)
        now = datetime.datetime.now(datetime.timezone.utc)

        display_limit = (
            max(1, max_results) if max_results is not None and max_results > 0 else None
        )
        max_to_collect = display_limit * 2 if display_limit is not None else None

        collected: List[ScheduledTask] = []
        warnings: List[str] = []

        tasklist_page_token: Optional[str] = None

        while True:
            tasklist_params: dict[str, Any] = {"maxResults": 100}
            if tasklist_page_token:
                tasklist_params["pageToken"] = tasklist_page_token

            try:
                tasklist_response = await self._execute(
                    client.tasklists().list(**tasklist_params)
                )
            except Exception as exc:
                raise TaskServiceError(f"Error listing task lists: {exc}") from exc

            for task_list in tasklist_response.get("items", []):
                list_id = task_list.get("id")
                if not list_id:
                    continue
                list_title = task_list.get("title", "(Untitled list)")

                task_page_token: Optional[str] = None
                while True:
                    task_params: dict[str, Any] = {
                        "tasklist": list_id,
                        "showCompleted": False,
                        "showDeleted": False,
                        "showHidden": False,
                        "maxResults": 100,
                        "dueMax": normalize_rfc3339(end_dt),
                    }
                    if past_due_cutoff is not None:
                        task_params["dueMin"] = normalize_rfc3339(past_due_cutoff)
                    if task_page_token:
                        task_params["pageToken"] = task_page_token

                    try:
                        task_response = await self._execute(
                            client.tasks().list(**task_params)
                        )
                    except Exception as exc:
                        warnings.append(f"Tasks ({list_title}): {exc}")
                        break

                    items = task_response.get("items", [])
                    for item in items:
                        due_raw = item.get("due")
                        if not due_raw:
                            continue

                        due_dt = parse_rfc3339_datetime(due_raw)
                        if due_dt is None:
                            continue

                        if due_dt > end_dt + datetime.timedelta(seconds=1):
                            continue
                        if past_due_cutoff is not None and due_dt < past_due_cutoff:
                            continue

                        status = item.get("status", "needsAction")
                        if status.lower() == "completed":
                            continue

                        collected.append(
                            ScheduledTask(
                                title=item.get("title", "(No title)"),
                                due=due_dt,
                                due_display=normalize_rfc3339(due_dt),
                                status=status,
                                list_title=list_title,
                                list_id=list_id,
                                id=item.get("id", ""),
                                notes=item.get("notes"),
                                updated=item.get("updated"),
                                completed=item.get("completed"),
                                web_link=item.get("webViewLink")
                                or item.get("selfLink"),
                                is_overdue=due_dt < now,
                            )
                        )

                        if (
                            max_to_collect is not None
                            and len(collected) >= max_to_collect
                        ):
                            break

                    if max_to_collect is not None and len(collected) >= max_to_collect:
                        break

                    task_page_token = task_response.get("nextPageToken")
                    if not task_page_token:
                        break

                if max_to_collect is not None and len(collected) >= max_to_collect:
                    break

            if max_to_collect is not None and len(collected) >= max_to_collect:
                break

            tasklist_page_token = tasklist_response.get("nextPageToken")
            if not tasklist_page_token:
                break

        collected.sort(key=lambda entry: entry.due)
        if display_limit is None:
            displayed = collected
            remaining = 0
        else:
            displayed = collected[:display_limit]
            remaining = max(0, len(collected) - len(displayed))

        return ScheduledTaskCollection(displayed, warnings, remaining)

    # ---- Search -----------------------------------------------------------

    async def search_tasks(
        self,
        query: str,
        *,
        task_list_id: Optional[str] = None,
        max_results: int = 25,
        include_completed: bool = False,
        include_hidden: bool = False,
        include_deleted: bool = False,
        search_notes: bool = True,
        due_min: Optional[str] = None,
        due_max: Optional[str] = None,
    ) -> TaskSearchResponse:
        """Search Google Tasks across multiple lists."""
        client = self._client_or_raise()

        trimmed_query = (query or "").strip()

        due_min_rfc = parse_time_string(due_min) if due_min else None
        due_max_rfc = parse_time_string(due_max) if due_max else None

        display_limit = max(1, max_results)
        max_to_collect = display_limit * 3
        normalized_query = trimmed_query.lower()

        collected: List[TaskSearchResult] = []
        warnings: List[str] = []
        scanned_lists: List[str] = []
        lists_to_scan: List[TaskListInfo] = []

        if task_list_id:
            try:
                lists_to_scan.append(await self.get_task_list(task_list_id))
            except TaskServiceError as exc:
                raise TaskServiceError(str(exc)) from exc
        else:
            tasklist_page_token: Optional[str] = None
            while True:
                tasklist_params: dict[str, Any] = {"maxResults": 100}
                if tasklist_page_token:
                    tasklist_params["pageToken"] = tasklist_page_token

                try:
                    tasklist_response = await self._execute(
                        client.tasklists().list(**tasklist_params)
                    )
                except Exception as exc:
                    raise TaskServiceError(
                        f"Error listing task lists: {exc}"
                    ) from exc

                for item in tasklist_response.get("items", []):
                    list_id = item.get("id")
                    if not list_id:
                        continue
                    lists_to_scan.append(
                        TaskListInfo(
                            id=list_id,
                            title=item.get("title", "(Untitled list)"),
                            updated=item.get("updated"),
                        )
                    )

                tasklist_page_token = tasklist_response.get("nextPageToken")
                if not tasklist_page_token:
                    break

            if not lists_to_scan:
                raise TaskServiceError(f"No task lists found for {self._user_email}.")

        for list_info in lists_to_scan:
            scanned_lists.append(list_info.title)

            task_page_token: Optional[str] = None
            list_title_normalized = (list_info.title or "").lower()
            list_match = normalized_query in list_title_normalized
            while True:
                task_params: dict[str, Any] = {
                    "tasklist": list_info.id,
                    "maxResults": 100,
                    "showCompleted": include_completed,
                    "showDeleted": include_deleted,
                    "showHidden": include_hidden,
                }

                if due_min_rfc:
                    task_params["dueMin"] = due_min_rfc
                elif due_min:
                    task_params["dueMin"] = due_min

                if due_max_rfc:
                    task_params["dueMax"] = due_max_rfc
                elif due_max:
                    task_params["dueMax"] = due_max

                if task_page_token:
                    task_params["pageToken"] = task_page_token

                try:
                    task_response = await self._execute(
                        client.tasks().list(**task_params)
                    )
                except Exception as exc:
                    warnings.append(f"Tasks ({list_info.title}): {exc}")
                    break

                for item in task_response.get("items", []):
                    status = item.get("status", "needsAction")
                    if not include_completed and status.lower() == "completed":
                        continue

                    title = item.get("title", "(No title)")
                    notes = item.get("notes") if search_notes else None
                    haystack_parts = [title]
                    if notes:
                        haystack_parts.append(notes)

                    if (
                        not list_match
                        and normalized_query not in " ".join(haystack_parts).lower()
                    ):
                        continue

                    collected.append(
                        TaskSearchResult(
                            title=title,
                            status=status,
                            list_title=list_info.title,
                            list_id=list_info.id,
                            id=item.get("id", ""),
                            due=item.get("due"),
                            updated=item.get("updated"),
                            completed=item.get("completed"),
                            notes=item.get("notes") if search_notes else None,
                            web_link=item.get("webViewLink")
                            or item.get("selfLink"),
                        )
                    )

                    if len(collected) >= max_to_collect:
                        break

                if len(collected) >= max_to_collect:
                    break

                task_page_token = task_response.get("nextPageToken")
                if not task_page_token:
                    break

            if len(collected) >= max_to_collect:
                break

        if collected:
            collected.sort(
                key=lambda task: (
                    parse_rfc3339_datetime(task.due)
                    or datetime.datetime.max.replace(tzinfo=datetime.timezone.utc),
                    parse_rfc3339_datetime(task.updated)
                    or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
                )
            )

        selected = collected[:display_limit]
        truncated = max(0, len(collected) - len(selected))

        return TaskSearchResponse(
            matches=selected,
            warnings=warnings,
            scanned_lists=scanned_lists,
            truncated=truncated,
        )

    # ---- Helpers ----------------------------------------------------------

    @staticmethod
    def _task_from_item(item: dict[str, Any], list_info: TaskListInfo) -> Task:
        due_dt = parse_rfc3339_datetime(item.get("due"))
        return Task(
            title=item.get("title", "(No title)"),
            status=item.get("status", "needsAction"),
            list_title=list_info.title,
            list_id=list_info.id,
            id=item.get("id", ""),
            due=due_dt,
            notes=item.get("notes"),
            updated=item.get("updated"),
            completed=item.get("completed"),
            web_link=item.get("webViewLink") or item.get("selfLink"),
            parent=item.get("parent"),
            position=item.get("position"),
        )


__all__ = [
    "Task",
    "ScheduledTask",
    "TaskListInfo",
    "TaskSearchResult",
    "TaskService",
    "TaskServiceError",
    "TaskAuthorizationError",
    "ScheduledTaskCollection",
    "TaskSearchResponse",
]
