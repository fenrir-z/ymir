from id_definition.error_codes import UpdaterErrorCode


class UpdateError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__()
        self.code = code
        self.message = message


class BackupDirNotEmpty(UpdateError):
    def __init__(self) -> None:
        super().__init__(code=UpdaterErrorCode.BACKUP_DIR_NOT_EMPTY,
                         message='Backup directory not empty')


class SandboxVersionNotSupported(UpdateError):
    def __init__(self, sandbox_version: str) -> None:
        super().__init__(code=UpdaterErrorCode.SANDBOX_VERSION_NOT_SUPPORTED,
                         message=f"Sandbox version: {sandbox_version} not supported")


class EnvVersionNotMatch(UpdateError):
    def __init__(self) -> None:
        super().__init__(code=UpdaterErrorCode.ENV_VERSION_NOT_MATCH,
                         message='.env version not matched')
