"""Standalone Gmail MCP server.

Exposes search, read, send, draft, thread, and label management via MCP protocol.
Zero imports from Backend_FastAPI — fully standalone.

Run:
    python -m servers.gmail --transport streamable-http --host 0.0.0.0 --port 9005
"""

from __future__ import annotations

import asyncio
import base64
from email.mime.text import MIMEText
from typing import Any, Dict, List, Literal, Optional

from fastmcp import FastMCP

from shared.google_auth import DEFAULT_USER_EMAIL, get_gmail_service

# Default port for HTTP transport
DEFAULT_HTTP_PORT = 9005

mcp = FastMCP("gmail")
GMAIL_BATCH_SIZE = 25
HTML_BODY_TRUNCATE_LIMIT = 20000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_message_bodies(payload: dict) -> dict:
    """Extract plain text and HTML body from a Gmail message payload."""
    text_body = ""
    html_body = ""
    parts = [payload] if "parts" not in payload else payload.get("parts", [])
    part_queue = list(parts)

    while part_queue:
        part = part_queue.pop(0)
        mime_type = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")

        if body_data:
            try:
                decoded_data = base64.urlsafe_b64decode(body_data).decode(
                    "utf-8", errors="ignore"
                )
                if mime_type == "text/plain" and not text_body:
                    text_body = decoded_data
                elif mime_type == "text/html" and not html_body:
                    html_body = decoded_data
            except Exception:
                pass

        if mime_type.startswith("multipart/") and "parts" in part:
            part_queue.extend(part.get("parts", []))

    if payload.get("body", {}).get("data"):
        try:
            decoded_data = base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="ignore"
            )
            mime_type = payload.get("mimeType", "")
            if mime_type == "text/plain" and not text_body:
                text_body = decoded_data
            elif mime_type == "text/html" and not html_body:
                html_body = decoded_data
        except Exception:
            pass

    return {"text": text_body, "html": html_body}


def _format_body_content(text_body: str, html_body: str) -> str:
    """Format message body content with HTML fallback and truncation."""
    if text_body.strip():
        return text_body
    elif html_body.strip():
        if len(html_body) > HTML_BODY_TRUNCATE_LIMIT:
            html_body = (
                html_body[:HTML_BODY_TRUNCATE_LIMIT] + "\n\n[HTML content truncated...]"
            )
        return f"[HTML Content Converted]\n{html_body}"
    else:
        return "[No readable content found]"


def _extract_attachments(payload: dict) -> List[Dict[str, Any]]:
    """Extract attachment metadata from a Gmail message payload."""
    attachments: list[Dict[str, Any]] = []

    def search_parts(part: dict) -> None:
        body = part.get("body", {})
        if part.get("filename") and body.get("attachmentId"):
            att_dict: Dict[str, Any] = {
                "filename": part["filename"],
                "mimeType": part.get("mimeType", "application/octet-stream"),
                "size": body.get("size", 0),
                "attachmentId": body["attachmentId"],
                "partId": part.get("partId", ""),
            }
            headers = {h.get("name"): h.get("value") for h in part.get("headers", [])}
            if "Content-Disposition" in headers:
                att_dict["disposition"] = headers["Content-Disposition"]
            attachments.append(att_dict)

        if "parts" in part:
            for subpart in part["parts"]:
                search_parts(subpart)

    search_parts(payload)
    return attachments


def _extract_headers(payload: dict, header_names: List[str]) -> Dict[str, str]:
    """Extract specified headers from a Gmail message payload."""
    headers: Dict[str, str] = {}
    for header in payload.get("headers", []):
        name = header.get("name", "")
        if name in header_names:
            headers[name] = header.get("value", "")
    return headers


def _generate_gmail_web_url(item_id: str, account_index: int = 0) -> str:
    """Generate a Gmail web interface URL for a message or thread."""
    return f"https://mail.google.com/mail/u/{account_index}/#all/{item_id}"


def _prepare_gmail_message(
    subject: str,
    body: str,
    to: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    body_format: Literal["plain", "html"] = "plain",
    from_email: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Prepare a Gmail message for sending."""
    if body_format == "html":
        message = MIMEText(body, "html")
    else:
        message = MIMEText(body, "plain")

    message["Subject"] = subject
    if to:
        message["To"] = to
    if cc:
        message["Cc"] = cc
    if bcc:
        message["Bcc"] = bcc
    if from_email:
        message["From"] = from_email

    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return raw_message, thread_id


def _format_thread_content(thread_data: dict, thread_id: str) -> str:
    """Format Gmail thread content for display."""
    messages = thread_data.get("messages", [])
    if not messages:
        return f"Thread {thread_id} has no messages."

    first_payload = messages[0].get("payload", {})
    first_headers = _extract_headers(first_payload, ["Subject"])
    thread_subject = first_headers.get("Subject", "(no subject)")

    content_lines = [
        f"Thread ID: {thread_id}",
        f"Subject: {thread_subject}",
        f"Messages: {len(messages)}",
        f"Web Link: {_generate_gmail_web_url(thread_id)}",
        "",
    ]

    for i, message in enumerate(messages, 1):
        payload = message.get("payload", {})
        headers = _extract_headers(payload, ["From", "Date", "Subject", "To", "Cc"])
        sender = headers.get("From", "(unknown sender)")
        date = headers.get("Date", "(unknown date)")
        subject = headers.get("Subject", "(no subject)")
        to = headers.get("To", "")
        cc = headers.get("Cc", "")

        bodies = _extract_message_bodies(payload)
        text_body = bodies.get("text", "")
        html_body = bodies.get("html", "")
        body_data = _format_body_content(text_body, html_body)

        content_lines.extend(
            [
                f"=== Message {i} ===",
                f"From: {sender}",
                f"Date: {date}",
            ]
        )

        if subject != thread_subject:
            content_lines.append(f"Subject: {subject}")

        if to:
            content_lines.append(f"To: {to}")
        if cc:
            content_lines.append(f"Cc: {cc}")

        content_lines.extend(
            [
                "",
                body_data,
                "",
            ]
        )

    return "\n".join(content_lines)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool("gmail_search_messages")
async def search_gmail_messages(
    query: str,
    user_email: str = DEFAULT_USER_EMAIL,
    page_size: int = 10,
) -> str:
    """Search Gmail messages using Gmail search syntax.

    Args:
        query: Gmail search query (e.g. "from:user@example.com", "subject:report")
        user_email: User's email for authentication
        page_size: Maximum number of results to return
    """
    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    try:
        response = await asyncio.to_thread(
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max(page_size, 1))
            .execute
        )
    except Exception as exc:
        return f"Error searching Gmail messages: {exc}"

    messages = response.get("messages", []) or []
    next_token = response.get("nextPageToken")

    if not messages:
        return f"No messages found for query '{query}'."

    lines = [
        f"Found {len(messages)} messages matching '{query}':",
        "",
    ]

    for idx, message in enumerate(messages, start=1):
        message_id = message.get("id", "unknown")
        thread_id = message.get("threadId", "unknown")
        message_url = (
            _generate_gmail_web_url(message_id) if message_id != "unknown" else "N/A"
        )
        thread_url = (
            _generate_gmail_web_url(thread_id) if thread_id != "unknown" else "N/A"
        )

        subject = "(no subject)"
        attachments: list[Dict[str, Any]] = []
        if message_id != "unknown":
            try:
                full_message = await asyncio.to_thread(
                    service.users()
                    .messages()
                    .get(userId="me", id=message_id, format="full")
                    .execute
                )
                payload = full_message.get("payload", {})
                headers = _extract_headers(payload, ["Subject"])
                subject = headers.get("Subject", "(no subject)")
                attachments = _extract_attachments(payload)
            except Exception:
                pass

        message_lines = [
            f"{idx}. Subject: {subject}",
            f"   Message ID: {message_id}",
            f"   Thread ID: {thread_id}",
            f"   Message URL: {message_url}",
            f"   Thread URL:  {thread_url}",
        ]

        if attachments:
            message_lines.append(f"   Attachments ({len(attachments)}):")
            for att_idx, att in enumerate(attachments, start=1):
                att_filename = att.get("filename", "unknown")
                att_size = att.get("size", 0)
                att_mime = att.get("mimeType", "unknown")
                size_kb = att_size / 1024 if att_size else 0
                message_lines.append(
                    f"      {att_idx}. {att_filename} ({size_kb:.1f} KB, {att_mime})"
                )

        message_lines.append("")
        lines.extend(message_lines)

    if next_token:
        lines.append(f"Next page token: {next_token}")

    return "\n".join(lines)


@mcp.tool("gmail_get_message_content")
async def get_gmail_message_content(
    message_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Retrieve content of a Gmail message by message ID.

    Args:
        message_id: Gmail message ID (16-char base64 string)
        user_email: User's email for authentication
    """
    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    try:
        metadata = await asyncio.to_thread(
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["Subject", "From"],
            )
            .execute
        )
        full_message = await asyncio.to_thread(
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute
        )
    except Exception as exc:
        return f"Error retrieving Gmail message {message_id}: {exc}"

    headers = _extract_headers(metadata.get("payload", {}), ["Subject", "From"])
    subject = headers.get("Subject", "(no subject)")
    sender = headers.get("From", "(unknown sender)")

    bodies = _extract_message_bodies(full_message.get("payload", {}))
    body_text = _format_body_content(bodies.get("text", ""), bodies.get("html", ""))

    return "\n".join(
        [
            f"Subject: {subject}",
            f"From: {sender}",
            "",
            "--- BODY ---",
            body_text,
        ]
    )


@mcp.tool("gmail_list_message_attachments")
async def list_gmail_message_attachments(
    message_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """List attachments for a Gmail message including filename, mimeType, size, and IDs."""
    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    try:
        message = await asyncio.to_thread(
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute
        )
    except Exception as exc:
        return f"Error retrieving Gmail message {message_id}: {exc}"

    payload = message.get("payload", {})
    attachments = _extract_attachments(payload)
    if not attachments:
        return f"No attachments found for message '{message_id}'."

    lines = [f"Found {len(attachments)} attachments in message {message_id}:", ""]
    for idx, att in enumerate(attachments, start=1):
        lines.extend(
            [
                f"{idx}. Filename: {att.get('filename')}",
                f"   MIME: {att.get('mimeType')}",
                f"   Size: {att.get('size')} bytes",
                f"   Attachment ID: {att.get('attachmentId')}",
                f"   Part ID: {att.get('partId')}",
                (
                    f"   Disposition: {att.get('disposition')}"
                    if att.get("disposition")
                    else ""
                ),
                "",
            ]
        )
    return "\n".join(line for line in lines if line != "")


@mcp.tool("gmail_get_messages_content_batch")
async def get_gmail_messages_content_batch(
    message_ids: List[str],
    user_email: str = DEFAULT_USER_EMAIL,
    format: Literal["full", "metadata"] = "full",
) -> str:
    """Retrieve content of multiple Gmail messages at once.

    Args:
        message_ids: List of Gmail message IDs
        user_email: User's email for authentication
        format: "full" for complete content or "metadata" for headers only
    """
    if not message_ids:
        return "No message IDs provided."

    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    output: List[str] = []
    for chunk_start in range(0, len(message_ids), GMAIL_BATCH_SIZE):
        chunk = message_ids[chunk_start : chunk_start + GMAIL_BATCH_SIZE]
        for message_id in chunk:
            try:
                message = await asyncio.to_thread(
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=message_id,
                        format=format,
                        metadataHeaders=["Subject", "From"]
                        if format == "metadata"
                        else None,
                    )
                    .execute
                )
            except Exception as exc:
                output.append(f"Message {message_id}: {exc}")
                continue

            payload = message.get("payload", {})
            headers = _extract_headers(payload, ["Subject", "From"])
            subject = headers.get("Subject", "(no subject)")
            sender = headers.get("From", "(unknown sender)")
            message_url = _generate_gmail_web_url(message_id)

            if format == "metadata":
                output.extend(
                    [
                        f"Message ID: {message_id}",
                        f"Subject: {subject}",
                        f"From: {sender}",
                        f"Web Link: {message_url}",
                        "",
                    ]
                )
            else:
                bodies = _extract_message_bodies(payload)
                body_text = _format_body_content(
                    bodies.get("text", ""), bodies.get("html", "")
                )

                output.extend(
                    [
                        f"Message ID: {message_id}",
                        f"Subject: {subject}",
                        f"From: {sender}",
                        f"Web Link: {message_url}",
                        "",
                        body_text,
                        "",
                        "---",
                        "",
                    ]
                )

    return f"Retrieved {len(message_ids)} messages:\n\n" + "\n".join(output).rstrip(
        "-\n "
    )


@mcp.tool("gmail_send_message")
async def send_gmail_message(
    user_email: str = DEFAULT_USER_EMAIL,
    to: str = "",
    subject: str = "",
    body: str = "",
    body_format: Literal["plain", "html"] = "plain",
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> str:
    """Send a Gmail message.

    Args:
        user_email: Sender's email for authentication
        to: Recipient email address
        subject: Email subject line
        body: Email body content
        body_format: "plain" or "html"
        cc: CC email address
        bcc: BCC email address
        thread_id: Thread ID for replies
        in_reply_to: Message-ID being replied to
        references: Chain of Message-IDs for threading
    """
    if not to:
        return "Recipient email address (to) is required."
    if not subject:
        return "Subject is required."
    if not body:
        return "Body content is required."

    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    try:
        raw_message, final_thread_id = _prepare_gmail_message(
            subject=subject,
            body=body,
            to=to,
            cc=cc,
            bcc=bcc,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            body_format=body_format,
        )
    except ValueError as exc:
        return str(exc)

    payload: Dict[str, str] = {"raw": raw_message}
    if final_thread_id:
        payload["threadId"] = final_thread_id

    try:
        response = await asyncio.to_thread(
            service.users().messages().send(userId="me", body=payload).execute
        )
    except Exception as exc:
        return f"Error sending Gmail message: {exc}"

    message_id = response.get("id", "(unknown)")
    return f"Email sent! Message ID: {message_id}"


@mcp.tool("gmail_draft_message")
async def draft_gmail_message(
    user_email: str = DEFAULT_USER_EMAIL,
    subject: str = "",
    body: str = "",
    to: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    body_format: Literal["plain", "html"] = "plain",
) -> str:
    """Create a Gmail draft message.

    Args:
        user_email: User's email for authentication
        subject: Email subject line
        body: Email body content
        to: Recipient email address (optional for drafts)
        cc: CC email address
        bcc: BCC email address
        thread_id: Thread ID for replies
        in_reply_to: Message-ID being replied to
        references: Chain of Message-IDs for threading
        body_format: "plain" or "html"
    """
    if not subject:
        return "Subject is required to create a draft."
    if not body:
        return "Body content is required to create a draft."

    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    try:
        raw_message, final_thread_id = _prepare_gmail_message(
            subject=subject,
            body=body,
            to=to,
            cc=cc,
            bcc=bcc,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            body_format=body_format,
        )
    except ValueError as exc:
        return str(exc)

    draft_body: Dict[str, Dict[str, str]] = {"message": {"raw": raw_message}}
    if final_thread_id:
        draft_body["message"]["threadId"] = final_thread_id

    try:
        response = await asyncio.to_thread(
            service.users().drafts().create(userId="me", body=draft_body).execute
        )
    except Exception as exc:
        return f"Error creating Gmail draft: {exc}"

    draft_id = response.get("id", "(unknown)")
    return f"Draft created! Draft ID: {draft_id}"


@mcp.tool("gmail_get_thread_content")
async def get_gmail_thread_content(
    thread_id: str,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Retrieve full content of a Gmail thread.

    Args:
        thread_id: Gmail thread ID
        user_email: User's email for authentication
    """
    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    try:
        thread = await asyncio.to_thread(
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute
        )
    except Exception as exc:
        return f"Error retrieving Gmail thread {thread_id}: {exc}"

    return _format_thread_content(thread, thread_id)


@mcp.tool("gmail_get_threads_content_batch")
async def get_gmail_threads_content_batch(
    thread_ids: List[str],
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Retrieve full content of multiple Gmail threads at once.

    Args:
        thread_ids: List of Gmail thread IDs
        user_email: User's email for authentication
    """
    if not thread_ids:
        return "No thread IDs provided."

    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    output: List[str] = []
    for chunk_start in range(0, len(thread_ids), GMAIL_BATCH_SIZE):
        chunk = thread_ids[chunk_start : chunk_start + GMAIL_BATCH_SIZE]
        for thread_id in chunk:
            try:
                thread = await asyncio.to_thread(
                    service.users()
                    .threads()
                    .get(userId="me", id=thread_id, format="full")
                    .execute
                )
            except Exception as exc:
                output.append(f"Thread {thread_id}: {exc}")
                continue

            output.append(_format_thread_content(thread, thread_id))
            output.append("---")

    return f"Retrieved {len(thread_ids)} threads:\n\n" + "\n".join(output).rstrip(
        "-\n "
    )


@mcp.tool("gmail_list_labels")
async def list_gmail_labels(user_email: str = DEFAULT_USER_EMAIL) -> str:
    """List all Gmail labels (system and user-created)."""
    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    try:
        response = await asyncio.to_thread(
            service.users().labels().list(userId="me").execute
        )
    except Exception as exc:
        return f"Error listing Gmail labels: {exc}"

    labels = response.get("labels", []) or []
    if not labels:
        return "No labels found."

    system_labels: List[Dict[str, Any]] = []
    user_labels: List[Dict[str, Any]] = []

    for label in labels:
        if label.get("type") == "system":
            system_labels.append(label)
        else:
            user_labels.append(label)

    lines = [f"Found {len(labels)} labels:", ""]
    if system_labels:
        lines.append("System labels:")
        for label in system_labels:
            lines.append(f"- {label.get('name')} (ID: {label.get('id')})")
        lines.append("")

    if user_labels:
        lines.append("User labels:")
        for label in user_labels:
            lines.append(f"- {label.get('name')} (ID: {label.get('id')})")

    return "\n".join(lines).strip()


@mcp.tool("gmail_manage_label")
async def manage_gmail_label(
    action: Literal["create", "update", "delete"],
    user_email: str = DEFAULT_USER_EMAIL,
    name: Optional[str] = None,
    label_id: Optional[str] = None,
    label_list_visibility: Literal["labelShow", "labelHide"] = "labelShow",
    message_list_visibility: Literal["show", "hide"] = "show",
) -> str:
    """Create, update, or delete a Gmail label.

    Args:
        action: "create", "update", or "delete"
        user_email: User's email for authentication
        name: Label name (required for create, optional for update)
        label_id: Label ID (required for update/delete)
        label_list_visibility: "labelShow" or "labelHide"
        message_list_visibility: "show" or "hide"
    """
    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    try:
        if action == "create":
            if not name:
                return "Label name is required for create action."

            label_object = {
                "name": name,
                "labelListVisibility": label_list_visibility,
                "messageListVisibility": message_list_visibility,
            }
            response = await asyncio.to_thread(
                service.users().labels().create(userId="me", body=label_object).execute
            )
            return (
                "Label created successfully!\n"
                f"Name: {response.get('name')}\n"
                f"ID: {response.get('id')}"
            )

        if action in {"update", "delete"} and not label_id:
            return "Label ID is required for update and delete actions."

        if action == "update":
            current = await asyncio.to_thread(
                service.users().labels().get(userId="me", id=label_id).execute
            )
            label_object = {
                "id": label_id,
                "name": name if name is not None else current.get("name"),
                "labelListVisibility": label_list_visibility,
                "messageListVisibility": message_list_visibility,
            }
            updated = await asyncio.to_thread(
                service.users()
                .labels()
                .update(userId="me", id=label_id, body=label_object)
                .execute
            )
            return (
                "Label updated successfully!\n"
                f"Name: {updated.get('name')}\n"
                f"ID: {updated.get('id')}"
            )

        if action == "delete":
            label = await asyncio.to_thread(
                service.users().labels().get(userId="me", id=label_id).execute
            )
            label_name = label.get("name", label_id)
            await asyncio.to_thread(
                service.users().labels().delete(userId="me", id=label_id).execute
            )
            return f"Label '{label_name}' (ID: {label_id}) deleted successfully!"

        return f"Unsupported action: {action}"
    except Exception as exc:
        return f"Error managing Gmail label: {exc}"


@mcp.tool("gmail_modify_message_labels")
async def modify_gmail_message_labels(
    message_id: str,
    add_label_ids: Optional[List[str]] = None,
    remove_label_ids: Optional[List[str]] = None,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Add or remove labels from a Gmail message.

    Args:
        message_id: Gmail message ID
        add_label_ids: Label IDs to add
        remove_label_ids: Label IDs to remove
        user_email: User's email for authentication
    """
    add_label_ids = add_label_ids or []
    remove_label_ids = remove_label_ids or []
    if not add_label_ids and not remove_label_ids:
        return "At least one of add_label_ids or remove_label_ids must be provided."

    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    body: Dict[str, List[str]] = {}
    if add_label_ids:
        body["addLabelIds"] = add_label_ids
    if remove_label_ids:
        body["removeLabelIds"] = remove_label_ids

    try:
        await asyncio.to_thread(
            service.users()
            .messages()
            .modify(userId="me", id=message_id, body=body)
            .execute
        )
    except Exception as exc:
        return f"Error modifying labels for message {message_id}: {exc}"

    actions = []
    if add_label_ids:
        actions.append(f"Added labels: {', '.join(add_label_ids)}")
    if remove_label_ids:
        actions.append(f"Removed labels: {', '.join(remove_label_ids)}")

    return (
        "Message labels updated successfully!\n"
        f"Message ID: {message_id}\n"
        f"{'; '.join(actions)}"
    )


@mcp.tool("gmail_batch_modify_message_labels")
async def batch_modify_gmail_message_labels(
    message_ids: List[str],
    add_label_ids: Optional[List[str]] = None,
    remove_label_ids: Optional[List[str]] = None,
    user_email: str = DEFAULT_USER_EMAIL,
) -> str:
    """Add or remove labels from multiple Gmail messages at once.

    Args:
        message_ids: List of Gmail message IDs
        add_label_ids: Label IDs to add
        remove_label_ids: Label IDs to remove
        user_email: User's email for authentication
    """
    if not message_ids:
        return "Message IDs are required for batch modification."

    add_label_ids = add_label_ids or []
    remove_label_ids = remove_label_ids or []
    if not add_label_ids and not remove_label_ids:
        return "At least one of add_label_ids or remove_label_ids must be provided."

    try:
        service = get_gmail_service(user_email)
    except ValueError as exc:
        return f"Authentication error: {exc}."
    except Exception as exc:
        return f"Error creating Gmail service: {exc}"

    body: Dict[str, List[str]] = {"ids": message_ids}
    if add_label_ids:
        body["addLabelIds"] = add_label_ids
    if remove_label_ids:
        body["removeLabelIds"] = remove_label_ids

    try:
        await asyncio.to_thread(
            service.users().messages().batchModify(userId="me", body=body).execute
        )
    except Exception as exc:
        return f"Error modifying labels for messages {message_ids}: {exc}"

    actions = []
    if add_label_ids:
        actions.append(f"Added labels: {', '.join(add_label_ids)}")
    if remove_label_ids:
        actions.append(f"Removed labels: {', '.join(remove_label_ids)}")

    return f"Labels updated for {len(message_ids)} messages: {'; '.join(actions)}"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the Gmail MCP server with the specified transport."""
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run(transport="stdio")


def main() -> None:  # pragma: no cover - CLI helper
    import argparse

    parser = argparse.ArgumentParser(description="Gmail MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for HTTP server",
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = [
    "mcp",
    "run",
    "main",
    "DEFAULT_HTTP_PORT",
    "search_gmail_messages",
    "get_gmail_message_content",
    "list_gmail_message_attachments",
    "get_gmail_messages_content_batch",
    "send_gmail_message",
    "draft_gmail_message",
    "get_gmail_thread_content",
    "get_gmail_threads_content_batch",
    "list_gmail_labels",
    "manage_gmail_label",
    "modify_gmail_message_labels",
    "batch_modify_gmail_message_labels",
]
