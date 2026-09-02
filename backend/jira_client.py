"""
jira_client.py — Jira Cloud REST API client for ShellMate session reporting.
Builds a rich ADF (Atlassian Document Format) document from terminal buffers
and chat history, then posts it as a new Jira issue.
"""
import base64
import re
from datetime import datetime

import httpx

# Matches ANSI/VT100 escape sequences (colours, cursor moves, etc.)
_ANSI_RE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


# ---------------------------------------------------------------------------
# ADF node helpers
# ---------------------------------------------------------------------------

def _text(content: str) -> dict:
    return {"type": "text", "text": content}


def _strong(content: str) -> dict:
    return {"type": "text", "text": content, "marks": [{"type": "strong"}]}


def _code_inline(content: str) -> dict:
    return {"type": "text", "text": content, "marks": [{"type": "code"}]}


def _heading(level: int, text: str) -> dict:
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [{"type": "text", "text": text}],
    }


def _paragraph(*nodes) -> dict:
    return {"type": "paragraph", "content": list(nodes)}


def _rule() -> dict:
    return {"type": "rule"}


def _code_block(text: str) -> dict:
    # Strip ANSI escape sequences — Jira ADF rejects control characters
    text = _ANSI_RE.sub("", text)
    # Remove remaining non-printable control chars except newline/tab
    text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', text)
    # Truncate very long buffers so the Jira field limit isn't hit
    MAX = 25_000
    if len(text) > MAX:
        text = f"... (truncated — showing last {MAX} chars) ...\n" + text[-MAX:]
    return {
        "type": "codeBlock",
        "attrs": {"language": "text"},
        "content": [{"type": "text", "text": text or "(empty)"}],
    }


def _panel(panel_type: str, *content) -> dict:
    return {
        "type": "panel",
        "attrs": {"panelType": panel_type},
        "content": list(content),
    }


def _bullet_list(items: list[str]) -> dict:
    return {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [_paragraph(_text(item))],
            }
            for item in items
        ],
    }


# ---------------------------------------------------------------------------
# Strip SUGGEST_CMD tags and HTML from AI message text
# ---------------------------------------------------------------------------

_SUGGEST_RE = re.compile(r"\[SUGGEST_CMD(?::\d+)?\](.*?)\[/SUGGEST_CMD\]", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_ai_text(raw: str) -> str:
    """Remove command block tags and HTML markup from an AI response."""
    text = _SUGGEST_RE.sub(lambda m: f"[CMD: {m.group(1).strip()}]", raw)
    text = _HTML_TAG_RE.sub("", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Main ADF builder
# ---------------------------------------------------------------------------

def build_adf(
    description: str,
    sessions: list[dict],   # [{label, hostname, connection_type, buffer_text}]
    chat_messages: list[dict],  # [{role: 'user'|'ai', text: str}]
) -> dict:
    nodes: list[dict] = []

    # --- Info panel: metadata -----------------------------------------------
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    device_names = [s.get("label", "?") for s in sessions]
    nodes.append(_panel(
        "info",
        _paragraph(
            _strong("Session date: "), _text(now),
            _text("   │   "),
            _strong("Devices: "), _text(", ".join(device_names) or "none"),
        ),
    ))

    # --- User notes ---------------------------------------------------------
    if description.strip():
        nodes.append(_heading(2, "Session Notes"))
        nodes.append(_paragraph(_text(description.strip())))

    nodes.append(_rule())

    # --- Terminal buffers ---------------------------------------------------
    if sessions:
        nodes.append(_heading(2, "Terminal Sessions"))
        for i, s in enumerate(sessions):
            label    = s.get("label", f"Tab {i + 1}")
            hostname = s.get("hostname", "")
            ctype    = s.get("connection_type", "ssh").upper()
            buf      = (s.get("buffer_text") or "").strip()

            nodes.append(_heading(3, f"Tab {i + 1} — {label}"))
            if hostname:
                nodes.append(_paragraph(
                    _strong("Host: "), _text(hostname),
                    _text("   │   "),
                    _strong("Type: "), _text(ctype),
                ))
            nodes.append(_code_block(buf or "(no output captured)"))

    nodes.append(_rule())

    # --- AI conversation ----------------------------------------------------
    if chat_messages:
        nodes.append(_heading(2, "ShellMate AI Conversation"))

        for msg in chat_messages:
            role = msg.get("role", "user")
            raw  = (msg.get("text") or "").strip()
            if not raw:
                continue

            if role == "user":
                nodes.append(_paragraph(_strong("You: "), _text(raw)))
            else:
                cleaned = _clean_ai_text(raw)
                # Split into paragraphs on double newlines
                for chunk in cleaned.split("\n\n"):
                    chunk = chunk.strip()
                    if chunk:
                        nodes.append({
                            "type": "blockquote",
                            "content": [_paragraph(_text(chunk))],
                        })

    return {"version": 1, "type": "doc", "content": nodes}


# ---------------------------------------------------------------------------
# Jira API
# ---------------------------------------------------------------------------

def _auth_header(email: str, token: str) -> str:
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {creds}"


async def create_issue(
    jira_url: str,
    email: str,
    api_token: str,
    project_key: str,
    summary: str,
    adf_body: dict,
    issue_type: str = "Task",
) -> dict:
    """POST to Jira REST API v3 to create a new issue. Returns the response JSON."""
    url = f"{jira_url.rstrip('/')}/rest/api/3/issue"
    headers = {
        "Authorization": _auth_header(email, api_token),
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    payload = {
        "fields": {
            "project":     {"key": project_key},
            "summary":     summary,
            "description": adf_body,
            "issuetype":   {"name": issue_type},
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if not resp.is_success:
            raise httpx.HTTPStatusError(
                f"Jira API error: {resp.status_code} — {resp.text[:500]}",
                request=resp.request, response=resp,
            )
        return resp.json()


async def search_issues(
    jira_url: str,
    email: str,
    api_token: str,
    project_key: str,
    query: str,
) -> list[dict]:
    """Search issues using the Jira issue picker API. Returns [{key, summary}]."""
    url = f"{jira_url.rstrip('/')}/rest/api/3/issue/picker"
    headers = {
        "Authorization": _auth_header(email, api_token),
        "Accept":        "application/json",
    }
    params = {
        "query":            query,
        "currentProjectId": project_key,
        "showSubTasks":     "false",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()

    # Flatten all sections (History Search, Current Search, etc.) and dedupe by key
    seen: set[str] = set()
    results: list[dict] = []
    for section in data.get("sections", []):
        for issue in section.get("issues", []):
            key = issue.get("key", "")
            if key and key not in seen:
                seen.add(key)
                results.append({
                    "key":     key,
                    "summary": issue.get("summaryText") or issue.get("summary", ""),
                    "status":  "",   # picker doesn't return status
                    "type":    "",
                })
    return results


async def add_comment(
    jira_url: str,
    email: str,
    api_token: str,
    issue_key: str,
    adf_body: dict,
) -> dict:
    """Add a comment (ADF) to an existing Jira issue."""
    url = f"{jira_url.rstrip('/')}/rest/api/3/issue/{issue_key}/comment"
    headers = {
        "Authorization": _auth_header(email, api_token),
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }
    payload = {"body": adf_body}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if not resp.is_success:
            raise httpx.HTTPStatusError(
                f"Jira API error: {resp.status_code} — {resp.text[:500]}",
                request=resp.request, response=resp,
            )
        return resp.json()


async def get_issue_types(
    jira_url: str,
    email: str,
    api_token: str,
    project_key: str,
) -> list[str]:
    """Return the list of issue type names available for the project."""
    url = f"{jira_url.rstrip('/')}/rest/api/3/project/{project_key}"
    headers = {
        "Authorization": _auth_header(email, api_token),
        "Accept":        "application/json",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return [it["name"] for it in data.get("issueTypes", [])]
