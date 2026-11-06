"""Domain exceptions."""
from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    USER_ERROR = 1
    BLUEPRINT_ERROR = 2
    FILESYSTEM_CONFLICT = 3
    VERIFICATION_FAILED = 4
    INTERNAL_ERROR = 5


class ForgeError(Exception):
    """Base exception for all Forge errors."""
    exit_code: ExitCode = ExitCode.INTERNAL_ERROR
