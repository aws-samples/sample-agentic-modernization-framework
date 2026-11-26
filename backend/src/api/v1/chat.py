"""
Chat Advisor API.
Provides a conversational interface for migration guidance.
Supports two modes:
  1. App-specific — advises on a single application (app_id = "APP-xxx")
  2. Portfolio-wide — answers general migration questions using full inventory data
Conversation history persisted to S3 for cross-session memory.
"""

from __future__ import annotations

import hashlib
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.src.auth.rbac import Role, require_role
from backend.src.config import settings
from backend.src.core import aws
from backend.src.core.models import ApiResponse, error_response, generic_error_message

logger = logging.getLogger(__name__)
router = APIRouter()

# ── System Prompts ─────────────────────────────────────────────────────

ADVISOR_SYSTEM_PROMPT = """You are a Migration Advisor helping application teams prepare for AWS migration.

RESPONSE FORMAT RULES (STRICT):
- Keep responses SHORT — max 3-4 sentences
- After updating a field, confirm in ONE line: "✓ Added XYZ as owner for APP-AL2023."
- Show completeness as a single line: "Completeness: 45% → next: What compliance standards apply?"
- Do NOT use tables, headers, or bullet lists for confirmations
- Do NOT repeat back all the data you just saved
- Do NOT list all missing fields unless the user asks "what's missing?"
- Always end with ONE follow-up question for the next most important gap
- Be direct, warm, concise — like a busy architect in a Slack message

PHASE GATES:
- When phase_readiness shows assessment_ready=true, tell the user: "✓ Ready for assessment! Say 'run assessment' to start."
- When wave_planning_ready=true, tell the user: "✓ Ready for wave planning!"
- Focus on filling fields needed for the NEXT phase (check phase_readiness.next_phase)
- Prioritize mandatory fields for the next gate over nice-to-have fields
- You cannot change the migration strategy, targets, exposure or approvals. If the user disagrees with the
  strategy, tell them to change it at the Discovery review gate in the UI (valid strategies: rehost,
  replatform, refactor, repurchase, relocate, retain, retire)

CAPABILITIES:
- Read app data and completeness (get_app_context)
- Update fields (update_app_field)
- Add owners (add_owner)
- Add compliance (update_app_compliance)
- Assign machines (search_machines → assign_machines_to_app)
- Add integrations (add_integrations)

BEHAVIOR:
- On first message, call get_app_context silently — don't dump the full state
- Check phase_readiness — if next_phase has missing fields, ask about those FIRST
- When user provides info, save it immediately, confirm briefly, check if gate is now passed
- If a gate just passed, mention it briefly but do NOT auto-trigger actions
- NEVER run assessment or any action unless the user EXPLICITLY asks (e.g. "run assessment", "start assessment")
- If user says "run assessment" or "start assessment", call run_assessment tool directly
"""

PORTFOLIO_SYSTEM_PROMPT = """You are a Migration Strategy Advisor for a large-scale AWS migration program.
You have access to the FULL portfolio of applications and infrastructure inventory.

RESPONSE FORMAT RULES (STRICT):
- Keep responses SHORT — max 4-5 sentences for simple questions
- For comparisons (EKS vs ECS, etc.), use a brief pros/cons format (2-3 bullets each)
- Be direct, warm, concise — like a senior architect in a Slack message
- Do NOT dump raw data — summarize and give actionable advice
- When asked about strategies, reference the actual inventory data (server count, OS distribution, etc.)

CAPABILITIES:
- get_portfolio_summary: Get full inventory stats (server count, OS distribution, apps, databases, connections)
- get_app_context: Look up a specific app if the user mentions one
- search_machines: Find servers by hostname

BEHAVIOR:
- On first message, call get_portfolio_summary silently to understand the estate
- Answer general migration questions using real inventory data (e.g., "What if I go for EKS?" → check OS distribution, app count, dependencies)
- Compare AWS services (EKS vs ECS, RDS vs Aurora, etc.) in context of the actual workload
- Recommend strategies based on the real infrastructure profile
- If the user asks about a specific app, use get_app_context
- NEVER ask for an app_id unless the user is clearly talking about a specific application
- You can discuss: target architectures, cost implications, migration patterns, wave strategies, tooling choices
"""


class ChatRequest(BaseModel):
    app_id: str
    message: str
    # Accepted for compatibility but ignored: the session is derived server-side
    # from the caller's identity (chat_session_id).
    session_id: str = ""


def _is_portfolio_context(app_id: str) -> bool:
    """Check if this is a portfolio-wide chat (not app-specific)."""
    return app_id in ("portfolio", "migration-assessment", "wave-planning", "general", "")


_CHAT_APP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{0,128}$")


def chat_session_id(user, app_id: str) -> str:
    """Server-side chat session id: one per (user, app/portfolio).

    The client's `session_id` is ignored — choosing it let one user continue (and
    read back through the Harness session) another user's conversation. The user
    is identified by the token's `sub` (email/username as a fallback), hashed so
    the S3 key / runtimeSessionId carries no personal data.
    """
    ident = getattr(user, "sub", "") or getattr(user, "email", "") or getattr(user, "username", "")
    if not ident:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user_key = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:24]
    scope = "portfolio" if _is_portfolio_context(app_id) else app_id
    return f"chat-{user_key}-{scope}"


@router.post("/message", dependencies=[require_role(Role.ENGINEER)])
def chat_message(req: ChatRequest, request: Request):
    """Send a message to the migration advisor, backed by the AgentCore Harness.

    Single architecture: chat and the Step Functions workflow use the same
    Harness (via the invoke-runtime Lambda bridge). The Harness has one fixed
    system prompt, so the advisor persona + context is prepended to the message.
    Multi-turn continuity is provided by a stable runtimeSessionId, derived
    server-side per user (chat_session_id). ENGINEER: chat drives Harness tools
    that write app fields.
    """
    if not _CHAT_APP_ID_RE.fullmatch(req.app_id or ""):
        return error_response("Invalid app_id", code="VALIDATION_ERROR")
    try:
        is_portfolio = _is_portfolio_context(req.app_id)
        session_id = chat_session_id(getattr(request.state, "user", None), req.app_id)

        guidance = PORTFOLIO_SYSTEM_PROMPT if is_portfolio else ADVISOR_SYSTEM_PROMPT
        context = (
            "[Context: Portfolio-wide chat. User may ask about any app, strategy, or workflow status.]"
            if is_portfolio
            else f"[Context: Discussing application {req.app_id}. Use this app_id for all tool calls.]"
        )
        prompt = f"{guidance}\n\n{context}\n\nUser: {req.message}"

        result = _invoke_harness(app_id=req.app_id or "portfolio", prompt=prompt, session_id=session_id)
        if result.get("status") == "error":
            return ApiResponse(status="error", data={"message": result.get("message", "chat failed")})

        response_text = result.get("response") or result.get("message") or ""

        # Persist a lightweight transcript for the UI (Harness owns real session memory).
        messages = _load_session(session_id)
        messages.append({"role": "user", "content": [{"text": req.message}]})
        messages.append({"role": "assistant", "content": [{"text": response_text}]})
        _save_session(session_id, messages)

        return ApiResponse(data={"response": response_text, "app_id": req.app_id, "session_id": session_id})

    except Exception as exc:
        logger.error("Chat failed for %s: %s", req.app_id, exc, exc_info=True)
        return ApiResponse(status="error", data={"message": generic_error_message(exc)})


def _invoke_harness(app_id: str, prompt: str, session_id: str) -> dict:
    """Invoke the AgentCore Harness synchronously via the invoke-runtime Lambda.

    The Lambda only exists where the AgentCore control plane is deployed. When
    it is missing — for example an API running against an older stack's data —
    say so plainly instead of surfacing a raw AccessDeniedException, which reads
    as a broken product rather than an unconfigured one.
    """
    import json as _json

    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    fn = settings.agentcore_invoke_lambda
    unavailable = {
        "status": "error",
        "message": (
            "The chat advisor needs the AgentCore runtime, which is not deployed "
            "in this environment. Portfolio, discovery, wave planning and "
            "governance all work without it."
        ),
    }
    if not fn:
        return unavailable

    # No retries: a retried chat re-runs the Harness turn. The read timeout must
    # outlast a Harness turn (boto's default 60s doesn't).
    lam = boto3.client(
        "lambda",
        region_name=settings.aws_region,
        config=Config(read_timeout=settings.chat_invoke_timeout_seconds, retries={"max_attempts": 1}),
    )
    try:
        resp = lam.invoke(
            FunctionName=fn,
            InvocationType="RequestResponse",
            Payload=_json.dumps(
                {"mode": "chat", "app_id": app_id, "prompt": prompt, "session_id": session_id}
            ).encode("utf-8"),
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        # Not deployed / not permitted to this role — both mean "not available
        # here", not "the request was bad".
        if code in ("ResourceNotFoundException", "AccessDeniedException", "AccessDenied"):
            logger.warning("chat unavailable: %s cannot be invoked (%s)", fn, code)
            return unavailable
        raise
    return _json.loads(resp["Payload"].read().decode("utf-8") or "{}")


# ── Session persistence (S3) ──────────────────────────────────────────

import json

_s3 = aws.client("s3")
def _bucket() -> str:
    """Resolved on use, not at import (no STS call when the module loads)."""
    return settings.get_s3_bucket()
_SESSION_PREFIX = "chat-sessions"


def _load_session(session_id: str) -> list:
    """Load conversation history from S3."""
    try:
        key = f"{_SESSION_PREFIX}/{session_id}.json"
        resp = _s3.get_object(Bucket=_bucket(), Key=key)
        data = json.loads(resp["Body"].read().decode("utf-8"))
        return data.get("messages", [])
    except _s3.exceptions.NoSuchKey:
        return []
    except Exception as exc:
        logger.warning("Failed to load chat session %s: %s", session_id, exc)
        return []


def _save_session(session_id: str, messages: list):
    """Save conversation history to S3."""
    try:
        key = f"{_SESSION_PREFIX}/{session_id}.json"
        # Keep only the most recent messages to avoid unbounded growth
        recent = messages[-settings.chat_history_max_messages:]

        # Convert messages to JSON-serializable format
        serializable = []
        for msg in recent:
            if isinstance(msg, dict):
                serializable.append(msg)
            elif hasattr(msg, "to_dict"):
                serializable.append(msg.to_dict())
            else:
                serializable.append({"role": "assistant", "content": [{"text": str(msg)}]})

        _s3.put_object(
            Bucket=_bucket(),
            Key=key,
            Body=json.dumps({"session_id": session_id, "messages": serializable}, default=str).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:
        logger.warning("Failed to save chat session %s: %s", session_id, exc)
