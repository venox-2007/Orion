"""Focused tests for Step 14: Natural Command Follow-ups.

Covers:
1. Follow-up commands referring to the last successfully targeted approved app:
   - "maximize it", "minimize that", "bring it to front", "restore that"
2. Expiration (> 5 minutes / 300 seconds):
   - Asking which application to manage when context has expired.
3. Ambiguity:
   - Asking which application to manage when the last action targeted multiple applications.
4. No context / failed actions:
   - Asking which app when no successful application action exists.
5. User scoping:
   - History is strictly scoped to the authorized user; another user cannot access it.
6. Sensitive / destructive operations:
   - Never resolving pronouns to file deletion or sensitive commands.
7. Voice and text equivalence.
"""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orion.config.settings import Settings
from orion.integrations.telegram_bot import (
    UNAUTHORIZED_MESSAGE,
    create_telegram_app,
    interpret_voice_command,
)
from orion.memory.database import ConversationMemory
from orion.tools.app_launcher import get_approved_apps
from orion.tools.desktop_inspector import WindowInfo
from orion.tools.registry import ToolRegistry
from orion.tools.router import (
    check_deterministic_app_launch,
    check_deterministic_window_query,
    resolve_target_app_from_context,
    route_and_execute_tool,
)


def _make_win(hwnd: int, app_id: str, state: str = "Normal") -> WindowInfo:
    approved = get_approved_apps()
    disp = approved[app_id].display_name if app_id in approved else app_id
    return WindowInfo(
        hwnd=hwnd,
        app_id=app_id,
        display_name=disp,
        state=state,
        is_active=False,
    )


@pytest.fixture
def memory(tmp_path):
    db_path = tmp_path / "test_step14.db"
    return ConversationMemory(db_path=str(db_path))


@pytest.fixture
def bot_env(memory):
    owner_id = 12345
    token = "TEST_TOKEN_STEP14"

    mock_ollama = MagicMock()
    mock_ollama.generate_response = AsyncMock(return_value="I heard you.")
    mock_ollama.generate_structured_json = AsyncMock(return_value=None)

    reg = ToolRegistry()

    with patch("orion.config.settings.get_settings") as mock_settings:
        s = Settings(
            telegram_bot_token=token,
            telegram_owner_id=owner_id,
            voice_enabled=True,
            whisper_model="base",
            max_voice_duration_seconds=60,
            max_voice_file_size_bytes=5 * 1024 * 1024,
        )
        mock_settings.return_value = s
        app = create_telegram_app(
            token=token,
            owner_id=owner_id,
            ollama_client=mock_ollama,
            memory=memory,
            tool_registry=reg,
            transcriber=None,
        )

    msg_cb = None
    voice_cb = None
    for h_list in app.handlers.values():
        for h in h_list:
            if hasattr(h, "filters") and "text" in str(h.filters).lower():
                msg_cb = h.callback
            elif hasattr(h, "filters") and "voice" in str(h.filters).lower():
                voice_cb = h.callback

    return {
        "app": app,
        "owner_id": owner_id,
        "memory": memory,
        "message_handler": msg_cb,
        "voice_handler": voice_cb,
    }


# ===========================================================================
# 1. DATABASE MEMORY: LAST TARGETED APP & EXPIRATION
# ===========================================================================

def test_database_remembers_last_successful_app(memory):
    """Memory remembers the last successfully launched or managed app."""
    user_id = 1001
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "calculator"},
        status="success",
    )

    app_id, reason = memory.get_last_targeted_app(user_id)
    assert app_id == "calculator"
    assert reason is None


def test_database_ignores_failed_or_cancelled_actions(memory):
    """Failed or cancelled operations do not count as successfully targeted apps."""
    user_id = 1002
    # 1. Successful launch of notepad
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "notepad"},
        status="success",
    )
    # 2. Failed launch of word
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "word"},
        status="failed",
    )
    # 3. Cancelled launch of excel
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "excel"},
        status="cancelled",
    )

    app_id, reason = memory.get_last_targeted_app(user_id)
    assert app_id == "notepad"
    assert reason is None


def test_database_context_expires_after_5_minutes(memory):
    """Context expires if older than 300 seconds (5 minutes)."""
    user_id = 1003
    t_start = 1000000.0

    with patch("time.time", return_value=t_start):
        memory.log_action_history(
            user_id=user_id,
            action_type="launch_application",
            payload={"app_id": "calculator"},
            status="success",
        )
        # Immediate check (0 seconds elapsed) -> valid
        app_id, reason = memory.get_last_targeted_app(user_id, now=t_start + 60.0)
        assert app_id == "calculator"
        assert reason is None

        # 301 seconds elapsed (> 5 mins) -> expired
        app_id_exp, reason_exp = memory.get_last_targeted_app(user_id, now=t_start + 301.0)
        assert app_id_exp is None
        assert reason_exp == "expired"


def test_database_ambiguous_when_multiple_apps_targeted(memory):
    """When a composite multi-launch targeted multiple apps, pronoun resolution is ambiguous."""
    user_id = 1004
    memory.log_action_history(
        user_id=user_id,
        action_type="composite_app_action",
        payload={"command_type": "multi_launch", "app_ids": ["notepad", "calculator"]},
        status="success",
    )

    app_id, reason = memory.get_last_targeted_app(user_id)
    assert app_id is None
    assert reason == "ambiguous"


def test_database_scoped_to_user_id(memory):
    """User 1's history is not accessible by User 2."""
    user1 = 2001
    user2 = 2002
    memory.log_action_history(
        user_id=user1,
        action_type="launch_application",
        payload={"app_id": "vscode"},
        status="success",
    )

    app1, reason1 = memory.get_last_targeted_app(user1)
    assert app1 == "vscode"

    app2, reason2 = memory.get_last_targeted_app(user2)
    assert app2 is None
    assert reason2 == "no_context"


# ===========================================================================
# 2. DETERMINISTIC WINDOW QUERY & ROUTER RESOLUTION
# ===========================================================================

def test_resolve_pronoun_followup_maximize_it(memory):
    """'maximize it' resolves to the last targeted app proposal."""
    user_id = 3001
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "calculator"},
        status="success",
    )

    res = check_deterministic_window_query("maximize it", context={"user_id": user_id, "memory": memory})
    assert res is not None
    assert res.get("is_window_action") is True
    assert res.get("tool") == "maximize_application_window"
    assert res.get("arguments") == {"app_id": "calculator"}


def test_resolve_pronoun_followup_minimize_that(memory):
    """'minimize that' resolves to the last targeted app proposal."""
    user_id = 3002
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "notepad"},
        status="success",
    )

    res = check_deterministic_window_query("minimize that", context={"user_id": user_id, "memory": memory})
    assert res is not None
    assert res.get("is_window_action") is True
    assert res.get("tool") == "minimize_application_window"
    assert res.get("arguments") == {"app_id": "notepad"}


def test_resolve_pronoun_followup_bring_it_to_front(memory):
    """'bring it to front' and 'bring that to the front' resolve to focus tool."""
    user_id = 3003
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "vscode"},
        status="success",
    )

    for cmd in ["bring it to front", "bring that to the front", "bring it to the foreground", "focus it"]:
        res = check_deterministic_window_query(cmd, context={"user_id": user_id, "memory": memory})
        assert res is not None
        assert res.get("is_window_action") is True
        assert res.get("tool") == "focus_application_window"
        assert res.get("arguments") == {"app_id": "vscode"}


def test_expired_or_absent_context_asks_which_app(memory):
    """When context is absent, expired, or ambiguous, ORION asks which application."""
    user_id = 3004
    # No context at all
    res1 = check_deterministic_window_query("maximize it", context={"user_id": user_id, "memory": memory})
    assert res1 is not None
    assert res1.get("handled") is True
    assert "Which application would you like to maximize?" in res1.get("reply", "")

    res2 = check_deterministic_window_query("bring it to front", context={"user_id": user_id, "memory": memory})
    assert res2 is not None
    assert res2.get("handled") is True
    assert "Which application would you like to bring to the front?" in res2.get("reply", "")

    # Expired context
    t_start = 2000000.0
    with patch("time.time", return_value=t_start):
        memory.log_action_history(
            user_id=user_id,
            action_type="launch_application",
            payload={"app_id": "calculator"},
            status="success",
        )

    with patch("time.time", return_value=t_start + 350.0):
        res_exp = check_deterministic_window_query("minimize that", context={"user_id": user_id, "memory": memory})
        assert res_exp is not None
        assert res_exp.get("handled") is True
        assert "Which application would you like to minimize?" in res_exp.get("reply", "")


# ===========================================================================
# 3. ROUTE AND EXECUTE TOOL INTEGRATION
# ===========================================================================

@pytest.mark.asyncio
async def test_route_and_execute_tool_with_valid_followup(memory):
    """route_and_execute_tool produces a window proposal for valid follow-up."""
    user_id = 4001
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "calculator"},
        status="success",
    )

    win = _make_win(0x1001, "calculator", "Normal")
    with patch("orion.tools.desktop_inspector._enumerate_windows_native", return_value=[win]), \
         patch("sys.platform", "win32"):
        res = await route_and_execute_tool(
            "maximize it",
            context={"user_id": user_id, "memory": memory},
        )
        assert res is not None
        assert res.get("handled") is True
        assert res.get("is_proposal") is True
        assert res.get("action_type") == "maximize_application_window"
        assert res.get("arguments", {}).get("app_id") == "calculator"
        assert "Calculator" in res.get("proposal_text", "")


# ===========================================================================
# 4. VOICE COMMAND INTERPRETATION
# ===========================================================================

def test_voice_interpret_followup_with_valid_context(memory):
    """Spoken 'maximize it' resolves to 'maximize calculator' when calculator is in recent context."""
    user_id = 5001
    memory.log_action_history(
        user_id=user_id,
        action_type="launch_application",
        payload={"app_id": "calculator"},
        status="success",
    )

    text, is_amb, clarif = interpret_voice_command(
        "maximize it",
        recent_context={"owner_id": user_id, "memory": memory},
    )
    assert is_amb is False
    assert clarif is None
    assert text == "maximize calculator"


def test_voice_interpret_followup_expired_asks_clarification(memory):
    """Spoken 'bring it to front' asks clarification when context is expired."""
    user_id = 5002
    t_start = 3000000.0
    with patch("time.time", return_value=t_start):
        memory.log_action_history(
            user_id=user_id,
            action_type="launch_application",
            payload={"app_id": "vscode"},
            status="success",
        )

    with patch("time.time", return_value=t_start + 360.0):
        text, is_amb, clarif = interpret_voice_command(
            "bring it to front",
            recent_context={"owner_id": user_id, "memory": memory},
        )
        assert is_amb is True
        assert clarif == "Which application would you like to bring to the front?"


# ===========================================================================
# 5. SENSITIVE / DESTRUCTIVE COMMAND GUARD
# ===========================================================================

@pytest.mark.asyncio
async def test_destructive_pronoun_command_rejected_in_text(bot_env):
    """'delete it' or 'remove that' in text is never resolved to file or app actions."""
    msg_handler = bot_env["message_handler"]
    owner_id = bot_env["owner_id"]

    for destructive_cmd in ["delete it", "remove that", "wipe that"]:
        update = MagicMock()
        update.effective_user.id = owner_id
        update.effective_chat.type = "private"
        update.effective_chat.id = owner_id
        update.effective_message.text = destructive_cmd
        update.effective_message.reply_text = AsyncMock()

        ctx = MagicMock()
        ctx.bot.send_chat_action = AsyncMock()
        await msg_handler(update, ctx)

        update.effective_message.reply_text.assert_called_once()
        reply_content = update.effective_message.reply_text.call_args[0][0]
        assert "clarify which file or action" in reply_content.lower()


# ===========================================================================
# 6. UNAUTHORIZED USER REJECTION
# ===========================================================================

@pytest.mark.asyncio
async def test_unauthorized_user_followup_rejected(bot_env):
    """An unauthorized user cannot issue follow-up commands or access context."""
    msg_handler = bot_env["message_handler"]
    intruder_id = 99999

    update = MagicMock()
    update.effective_user.id = intruder_id
    update.effective_chat.type = "private"
    update.effective_chat.id = intruder_id
    update.effective_message.text = "maximize it"
    update.effective_message.reply_text = AsyncMock()

    ctx = MagicMock()
    await msg_handler(update, ctx)

    update.effective_message.reply_text.assert_called_once_with(UNAUTHORIZED_MESSAGE)
