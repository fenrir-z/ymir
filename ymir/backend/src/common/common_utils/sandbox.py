from collections import defaultdict
import os
import re
import shutil
from typing import Callable, List, Dict, Set

import yaml

from id_definition.error_codes import UpdaterErrorCode
from id_definition.task_id import IDProto
from mir import version


class SandboxError(Exception):
    def __init__(self, error_code: int, error_message: str) -> None:
        super().__init__()
        self.error_code = error_code
        self.error_message = error_message

    def __str__(self) -> str:
        return f"code: {self.error_code}, content: {self.error_message}"


def detect_sandbox_src_ver(sandbox_root: str) -> str:
    """
    detect user space versions in this sandbox

    Args:
        sandbox_root (str): root of this sandbox

    Returns:
        str: sandbox version

    Raises:
        SandboxError if labels.yaml not found, or can not be parsed as yaml;
        found no user space version or multiple user space versions.
    """
    user_to_repos = _detect_users_and_repos(sandbox_root)
    ver_to_users: Dict[str, List[str]] = defaultdict(list)
    for user_id in user_to_repos:
        user_label_file = os.path.join(sandbox_root, user_id, 'labels.yaml')
        try:
            with open(user_label_file, 'r') as f:
                user_label_dict = yaml.safe_load(f)
        except (FileNotFoundError, yaml.YAMLError) as e:
            raise SandboxError(error_code=UpdaterErrorCode.INVALID_USER_LABEL_FILE,
                               error_message=f"invalid label file: {user_label_file}") from e

        ver_to_users[user_label_dict.get('ymir_version', version.DEFAULT_YMIR_SRC_VERSION)].append(user_id)

    if len(ver_to_users) != 1:
        raise SandboxError(error_code=UpdaterErrorCode.INVALID_USER_SPACE_VERSIONS,
                           error_message=f"invalid user space versions: {ver_to_users}")

    return list(ver_to_users.keys())[0]


def update(sandbox_root: str, update_funcs: List[Callable]) -> None:
    _backup(sandbox_root)

    user_to_repos = _detect_users_and_repos(sandbox_root)
    try:
        for user_id, repo_ids in user_to_repos.items():
            for repo_id in repo_ids:
                for update_func in update_funcs:
                    update_func(mir_root=os.path.join(sandbox_root, user_id, repo_id))
    except Exception as e:
        _roll_back(sandbox_root)
        raise e

    # cleanup
    _remove_backup(sandbox_root)


def _backup(sandbox_root: str) -> None:
    backup_dir = os.path.join(sandbox_root, 'backup')
    os.makedirs(backup_dir, exist_ok=True)
    if os.listdir(backup_dir):
        raise SandboxError(error_code=UpdaterErrorCode.BACKUP_DIR_NOT_EMPTY,
                           error_message=f"Backup directory not empty: {backup_dir}")

    for user_id in _detect_users_and_repos(sandbox_root):
        shutil.copytree(src=os.path.join(sandbox_root, user_id), dst=os.path.join(backup_dir, user_id))


def _roll_back(sandbox_root: str) -> None:
    backup_dir = os.path.join(sandbox_root, 'backup')
    for user_id in _detect_users_and_repos(sandbox_root):
        src_user_dir = os.path.join(backup_dir, user_id)
        dst_user_dir = os.path.join(sandbox_root, user_id)
        shutil.rmtree(dst_user_dir)
        shutil.copytree(src_user_dir, dst_user_dir)

    _remove_backup(sandbox_root)


def _remove_backup(sandbox_root: str) -> None:
    shutil.rmtree(os.path.join(sandbox_root, 'backup'))


def _detect_users_and_repos(sandbox_root: str) -> Dict[str, Set[str]]:
    """
    detect user and repo directories in this sandbox

    Args:
        sandbox_root (str): root of this sandbox

    Returns:
        Dict[str, List[str]]: key: user id, value: repo ids
    """
    user_to_repos = defaultdict(set)
    for user_id in os.listdir(sandbox_root):
        match_result = re.match(f"\\d{{{IDProto.ID_LEN_USER_ID}}}", user_id)
        if not match_result:
            continue
        user_dir = os.path.join(sandbox_root, user_id)
        user_to_repos[user_id].update([
            repo_id for repo_id in os.listdir(user_dir) if re.match(f"\\d{{{IDProto.ID_LEN_REPO_ID}}}", repo_id)
            and os.path.isdir(os.path.join(user_dir, repo_id, '.git'))
        ])
    return user_to_repos
