# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Identify host-generated recovery context across recorded attempts."""

NOTICE_KEY = "jiuwenswarm_recovery_notice"
NOTICE_POSITION_KEY = "jiuwenswarm_recovery_position"


def is_recovery_message(message: object) -> bool:
    from replay.backtrack import is_backtrack_memory_message

    return is_backtrack_memory_message(message) or (
        isinstance(message, dict) and message.get("role") == "user"
        and (message.get("metadata") or {}).get(NOTICE_KEY) is True
    )


def recovery_notice(notebook: str, *, position: int) -> dict:
    if not notebook:
        return {
            "role": "user",
            "content": ("[Host recovery notification; original task unchanged.] The selected decision is being "
                        "regenerated after rollback. Consult the single system recovery notebook as fallible "
                        "evidence, recheck uncertain test assumptions, and continue the full task. This is a "
                        "persistent history marker, not a new user request."),
            "metadata": {NOTICE_KEY: True, NOTICE_POSITION_KEY: position},
        }
    return {
        "role": "user",
        "content": (
            "[Host recovery notification; the original user task is unchanged.]\n"
            "A rewind has already completed. The failed trial below already happened in the abandoned "
            "branch. Continue with its corrected strategy; do not recreate that trial. "
            "Earlier read-only calls have already been replayed. This is a persistent historical "
            "notification, not a new request on each model turn. Use later tool results to recognize "
            "completed steps. Once the corrected approach has passed verification, stop calling tools "
            "and give the user the final answer.\n\n" + notebook
        ),
        "metadata": {NOTICE_KEY: True, NOTICE_POSITION_KEY: position},
    }
