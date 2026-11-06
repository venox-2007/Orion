"""Focused tests for Step 13: Safe Voice Command Normalization.

Covers:
1. Normalization of spoken variations for approved apps:
   - notepad / note pad / node pad
   - calculator / calc
   - VS Code / vscode / visual studio code
   - file explorer / explorer
2. Context restriction: normalization applies only in app-command contexts,
   not in ordinary conversation or file names.
3. Allowlist protection: strictly matches against 20 approved applications,
   never launching unknown apps via unrestricted fuzzy matching.
4. Ambiguous input: asks for clarification rather than guessing when multiple
   approved apps match.
5. Multi-action preservation with spoken variations.
"""

from unittest.mock import patch

import pytest

from orion.integrations.telegram_bot import interpret_voice_command
from orion.tools.app_launcher import (
    APPROVED_APPLICATIONS,
    find_matching_apps,
    is_app_blocked,
    resolve_app_alias,
)
from orion.tools.router import (
    check_deterministic_app_launch,
    check_deterministic_multi_action,
    check_deterministic_window_query,
)


# ===========================================================================
# 1. SPOKEN APPLICATION NAME VARIATIONS (ACCEPTED ALIASES)
# ===========================================================================

def test_notepad_spoken_variations():
    """notepad, note pad, and node pad all resolve to 'notepad'."""
    assert resolve_app_alias("notepad") == "notepad"
    assert resolve_app_alias("note pad") == "notepad"
    assert resolve_app_alias("node pad") == "notepad"
    assert resolve_app_alias("not pad") == "notepad"
    assert resolve_app_alias("the note pad app") == "notepad"


def test_calculator_spoken_variations():
    """calculator, calc, and spoken variations resolve to 'calculator'."""
    assert resolve_app_alias("calculator") == "calculator"
    assert resolve_app_alias("calc") == "calculator"
    assert resolve_app_alias("cal c") == "calculator"
    assert resolve_app_alias("call c") == "calculator"
    assert resolve_app_alias("the calc") == "calculator"


def test_vscode_spoken_variations():
    """VS Code, vscode, and visual studio code resolve to 'vscode'."""
    assert resolve_app_alias("vscode") == "vscode"
    assert resolve_app_alias("vs code") == "vscode"
    assert resolve_app_alias("visual studio code") == "vscode"
    assert resolve_app_alias("v s code") == "vscode"
    assert resolve_app_alias("code") == "vscode"


def test_file_explorer_spoken_variations():
    """file explorer, explorer, and spoken variations resolve to 'file_explorer'."""
    assert resolve_app_alias("file explorer") == "file_explorer"
    assert resolve_app_alias("explorer") == "file_explorer"
    assert resolve_app_alias("files explorer") == "file_explorer"
    assert resolve_app_alias("windows explorer") == "file_explorer"


# ===========================================================================
# 2. CONTEXT RESTRICTION (APP-COMMAND CONTEXT VS ORDINARY CONVERSATION/FILES)
# ===========================================================================

def test_voice_normalization_restricted_to_app_command_context():
    """Ordinary conversation and file paths are never modified by voice normalization."""
    # 1. Ordinary conversation containing 'note pad' or 'node pad' remains untouched
    raw_convo_1 = "I need to buy a note pad from the store"
    interpreted_1, is_amb_1, clarif_1 = interpret_voice_command(raw_convo_1)
    assert interpreted_1 == "I need to buy a note pad from the store"
    assert is_amb_1 is False

    raw_convo_2 = "Can you explain what a node pad is"
    interpreted_2, is_amb_2, clarif_2 = interpret_voice_command(raw_convo_2)
    assert interpreted_2 == "Can you explain what a node pad is"
    assert is_amb_2 is False

    # 2. File modification commands remain untouched
    raw_file = "create file note pad.txt with shopping list"
    interpreted_file, _, _ = interpret_voice_command(raw_file)
    assert interpreted_file == "create file note pad.txt with shopping list"

    # 3. App command context DOES normalize spoken transcription slips
    raw_cmd = "please open node pad"
    interpreted_cmd, _, _ = interpret_voice_command(raw_cmd)
    assert interpreted_cmd == "please open notepad"


def test_app_launch_deterministic_routing_for_spoken_variations():
    """check_deterministic_app_launch routes spoken variants to approved launch proposals."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "", None)):
        # notepad variants
        res1 = check_deterministic_app_launch("open node pad")
        assert res1 is not None
        assert res1.get("is_proposal") is True
        assert res1["arguments"]["app_id"] == "notepad"

        res2 = check_deterministic_app_launch("launch note pad")
        assert res2 is not None
        assert res2.get("is_proposal") is True
        assert res2["arguments"]["app_id"] == "notepad"

        # calculator variants
        res3 = check_deterministic_app_launch("open calc")
        assert res3 is not None
        assert res3.get("is_proposal") is True
        assert res3["arguments"]["app_id"] == "calculator"

        # VS code variants
        res4 = check_deterministic_app_launch("open vs code")
        assert res4 is not None
        assert res4.get("is_proposal") is True
        assert res4["arguments"]["app_id"] == "vscode"

        # file explorer variants
        res5 = check_deterministic_app_launch("open explorer")
        assert res5 is not None
        assert res5.get("is_proposal") is True
        assert res5["arguments"]["app_id"] == "file_explorer"


# ===========================================================================
# 3. AMBIGUOUS INPUT HANDLING (CLARIFICATION RATHER THAN GUESSING)
# ===========================================================================

def test_ambiguous_app_input_asks_for_clarification():
    """When multiple approved apps match a query, ORION asks for clarification."""
    # 'microsoft' matches Word, Excel, PowerPoint, Edge, Teams
    matches = find_matching_apps("microsoft")
    assert len(matches) > 1
    assert "word" in matches
    assert "excel" in matches

    # Attempting to launch ambiguous 'microsoft' asks for clarification
    res = check_deterministic_app_launch("open microsoft")
    assert res is not None
    assert res.get("handled") is True
    assert res.get("is_proposal") is not True  # Does NOT guess or propose an arbitrary app
    reply = res.get("reply", "")
    assert "multiple approved applications matched" in reply.lower()
    assert "which one would you like to open" in reply.lower()


def test_ambiguous_window_command_asks_for_clarification():
    """Window commands on ambiguous application queries ask for clarification."""
    res = check_deterministic_window_query("maximize microsoft")
    assert res is not None
    assert res.get("handled") is True
    assert res.get("is_window_action") is not True
    reply = res.get("reply", "")
    assert "multiple approved applications matched" in reply.lower()
    assert "which one would you like to maximize" in reply.lower()


# ===========================================================================
# 4. ALLOWLIST PRESERVATION & REJECTION OF UNAPPROVED / UNKNOWN APPS
# ===========================================================================

def test_unknown_app_never_launched_via_fuzzy_matching():
    """Unapproved and unknown apps are strictly rejected without fuzzy guessing."""
    # Unknown apps
    assert resolve_app_alias("photoshop") is None
    assert resolve_app_alias("steam") is None
    assert resolve_app_alias("notepadplusplus") is None
    assert resolve_app_alias("randomapp123") is None

    # Check launch rejection
    res = check_deterministic_app_launch("open randomapp123")
    assert res is not None
    assert res.get("handled") is True
    assert res.get("is_proposal") is not True
    assert "not in the approved application allowlist" in res.get("reply", "")


def test_blocked_apps_strictly_rejected():
    """Command interpreters and blocked tools are rejected with security reasons."""
    blocked, reason = is_app_blocked("powershell")
    assert blocked is True
    assert "prohibited" in reason.lower()

    res = check_deterministic_app_launch("open powershell")
    assert res is not None
    assert res.get("handled") is True
    assert res.get("is_proposal") is not True
    assert "not in the approved application allowlist" in res.get("reply", "")


# ===========================================================================
# 5. MULTI-ACTION PRESERVATION WITH SPOKEN VARIATIONS
# ===========================================================================

def test_multi_action_commands_with_spoken_variations():
    """Multi-app and sequential commands preserve spoken variations."""
    with patch("orion.tools.app_launcher.is_app_available", return_value=(True, "", None)):
        # Multi-launch: "open node pad and calc"
        res_multi = check_deterministic_multi_action("open node pad and calc")
        assert res_multi is not None
        assert res_multi.get("is_proposal") is True
        args = res_multi.get("arguments", {})
        assert args.get("command_type") == "multi_launch"
        assert args.get("app_ids") == ["notepad", "calculator"]

        # Sequential: "open node pad then maximize it"
        res_seq = check_deterministic_multi_action("open node pad then maximize it")
        assert res_seq is not None
        assert res_seq.get("is_proposal") is True
        args_seq = res_seq.get("arguments", {})
        assert args_seq.get("target_app_id") == "notepad"
        assert args_seq.get("window_action") == "maximize"
