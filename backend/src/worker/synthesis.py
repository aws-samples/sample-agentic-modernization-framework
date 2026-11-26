"""
Bedrock synthesis — shared across assessment, workbench, and design modes.
"""

from __future__ import annotations

from backend.src.config import settings
from backend.src.worker.config import (
    APP_ID,
    BEDROCK_MODEL_ID,
    bedrock_client,
    logger,
)


def call_workbench_synthesis(steps_results: list[dict], atx_output: dict) -> dict:
    """Call Bedrock to synthesize transformation results."""
    from backend.src.worker.config import PLAN_ID

    system_prompt = """You are the Transformation Synthesis Agent. Analyze ATX transformation results and provide a summary.
Your response MUST include:
STATUS: <success|partial|needs_review>
CONFIDENCE: <number 0-100>
SUMMARY: <2-3 sentence summary>
Then provide detailed analysis."""

    prompt_parts = [
        f"# Transformation Report for {APP_ID} (Plan: {PLAN_ID})\n",
        "## Steps Executed",
    ]
    for sr in steps_results:
        prompt_parts.append(f"### Step {sr['step_number']}: {sr.get('td_name', '')}")
        prompt_parts.append(f"- Repo: {sr.get('repo_name', '')} | Status: {sr.get('status', '')}")
        if sr.get("message"):
            prompt_parts.append(f"- Details: {sr['message']}")
        prompt_parts.append("")

    if atx_output and atx_output.get("file_count", 0) > 0:
        prompt_parts.append("## ATX Output")
        prompt_parts.append(f"- Files: {atx_output.get('file_count', 0)}")
        prompt_parts.append(f"- S3: {atx_output.get('s3_prefix', '')}")

    prompt_parts.append("\nProvide your transformation synthesis report.")
    return _call_bedrock(system_prompt, "\n".join(prompt_parts), parse_strategy=False)


def _call_bedrock(system_prompt: str, user_prompt: str, parse_strategy: bool = True) -> dict:
    """Call Bedrock converse API and parse the response."""
    logger.info("Calling Bedrock synthesis (prompt: %d chars)", len(user_prompt))

    try:
        response = bedrock_client.converse(
            modelId=BEDROCK_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            inferenceConfig={"maxTokens": settings.synthesis_max_tokens, "temperature": settings.synthesis_temperature},
        )
    except Exception as exc:
        logger.error("Bedrock converse failed: %s", exc)
        if parse_strategy:
            return {
                "strategy": "replatform",
                "confidence": 50,
                "rationale": f"Bedrock failed: {exc}",
                "agent_response": f"ERROR: {exc}",
            }
        return {
            "status": "needs_review",
            "confidence": 50,
            "summary": f"Synthesis failed: {exc}",
            "agent_response": f"ERROR: {exc}",
            # Machine-readable failure flag: the workbench maps it to plan status
            # "completed_with_errors" instead of a clean "completed".
            "error": str(exc)[:500],
        }

    text = response["output"]["message"]["content"][0]["text"]

    result: dict = {"agent_response": text}
    if parse_strategy:
        result["strategy"] = "replatform"
        result["confidence"] = 70
        result["rationale"] = ""
    else:
        result["status"] = "success"
        result["confidence"] = 70
        result["summary"] = ""

    for line in text.split("\n"):
        ls = line.strip()
        if ls.startswith("STRATEGY:"):
            result["strategy"] = ls.replace("STRATEGY:", "").strip().lower()
        elif ls.startswith("CONFIDENCE:"):
            try:
                result["confidence"] = int(ls.replace("CONFIDENCE:", "").strip())
            except ValueError:
                pass
        elif ls.startswith("RATIONALE:"):
            result["rationale"] = ls.replace("RATIONALE:", "").strip()
        elif ls.startswith("STATUS:"):
            result["status"] = ls.replace("STATUS:", "").strip().lower()
        elif ls.startswith("SUMMARY:"):
            result["summary"] = ls.replace("SUMMARY:", "").strip()

    return result
