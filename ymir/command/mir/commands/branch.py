import argparse
import logging

from mir import scm
from mir.commands import base
from mir.protos import mir_command_pb2 as mirpb
from mir.tools import checker, mir_storage_ops, revs_parser
from mir.tools.code import MirCode


class CmdBranch(base.BaseCommand):
    @staticmethod
    def run_with_args(mir_root: str, force_delete: str) -> int:
        return_code = checker.check(mir_root, [checker.Prerequisites.IS_INSIDE_MIR_REPO])
        if return_code != MirCode.RC_OK:
            return return_code

        # can not delete master branch
        if force_delete == "master":
            logging.info("can not delete master branch")
            return MirCode.RC_CMD_INVALID_BRANCH_OR_TAG

        cmd_opts = []
        head_task_id = ''
        if force_delete:
            cmd_opts.extend(["-D", force_delete])
            mir_tasks: mirpb.MirTasks = mir_storage_ops.MirStorageOps.load_single_storage(mir_root=mir_root,
                                                                                          mir_branch=force_delete,
                                                                                          ms=mirpb.MirStorage.MIR_TASKS)
            head_task_id = mir_tasks.head_task_id

        repo_git = scm.Scm(mir_root, scm_executable="git")
        output_str = repo_git.branch(cmd_opts)
        if output_str:
            logging.info("\n%s" % output_str)

        if force_delete and head_task_id:
            repo_git.tag(['-d', revs_parser.join_rev_tid(force_delete, head_task_id)])

        return MirCode.RC_OK

    def run(self) -> int:
        logging.debug("command branch: %s" % self.args)

        return CmdBranch.run_with_args(mir_root=self.args.mir_root, force_delete=self.args.force_delete)


def bind_to_subparsers(subparsers: argparse._SubParsersAction,
                       parent_parser: argparse.ArgumentParser) -> None:
    branch_arg_parser = subparsers.add_parser("branch",
                                              parents=[parent_parser],
                                              description="use this command to show mir repo branches",
                                              help="show mir repo branches")
    delete_group = branch_arg_parser.add_mutually_exclusive_group()
    group = delete_group.add_mutually_exclusive_group()
    group.add_argument("-D", dest="force_delete", type=str, help="delete branch, even if branch not merged")
    branch_arg_parser.set_defaults(func=CmdBranch)
