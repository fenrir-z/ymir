import logging
import os
import shutil
import unittest
from unittest import mock

from google.protobuf.json_format import MessageToDict, ParseDict

import tests.utils as test_utils
from controller.utils import utils
from controller.utils.invoker_call import make_invoker_cmd_call
from controller.utils.invoker_mapping import RequestTypeToInvoker
from mir.protos import mir_command_pb2 as mir_cmd_pb
from proto import backend_pb2

RET_ID = 'commit t000aaaabbbbbbzzzzzzzzzzzzzzz3\nabc'


class TestInvokerTaskExporting(unittest.TestCase):
    def __init__(self, methodName: str) -> None:
        # dir structure:
        # test_involer_CLSNAME_sandbox_root
        # ├── media_storage_root
        # └── test_user
        #     └── ymir-dvc-test
        super().__init__(methodName=methodName)
        self._user_name = "user"
        self._mir_repo_name = "repoid"
        self._storage_name = "media_storage_root"
        self._task_id = 't000aaaabbbbbbzzzzzzzzzzzzzzb5'
        self._sub_task_id = utils.sub_task_id(self._task_id, 1)
        self._base_task_id = 't000aaaabbbbbbzzzzzzzzzzzzzzz4'
        self._guest_id1 = 't000aaaabbbbbbzzzzzzzzzzzzzzz1'
        self._guest_id2 = 't000aaaabbbbbbzzzzzzzzzzzzzzz2'

        self._sandbox_root = test_utils.dir_test_root(self.id().split(".")[-3:])
        self._user_root = os.path.join(self._sandbox_root, self._user_name)
        self._mir_repo_root = os.path.join(self._user_root, self._mir_repo_name)
        self._storage_root = os.path.join(self._sandbox_root, self._storage_name)

    def setUp(self):
        test_utils.check_commands()
        self._prepare_dirs()
        self._prepare_mir_repo()
        self._prepare_assets()

        logging.info("preparing done.")

    def _prepare_assets(self):
        image_names = ["1.jpg", "2.jpg"]
        for image_name in image_names:
            with open(os.path.join(self._storage_root, image_name), 'w') as f:
                f.write("1")

    def tearDown(self):
        if os.path.isdir(self._sandbox_root):
            shutil.rmtree(self._sandbox_root)
        pass

    # custom: env prepare
    def _prepare_dirs(self):
        if os.path.isdir(self._sandbox_root):
            logging.info("sandbox root exists, remove it first")
            shutil.rmtree(self._sandbox_root)
        os.makedirs(self._sandbox_root)
        os.mkdir(self._user_root)
        os.mkdir(self._mir_repo_root)
        os.mkdir(self._storage_root)

    def _prepare_mir_repo(self):
        # init repo
        test_utils.mir_repo_init(self._mir_repo_root)

    def _mock_run_func(*args, **kwargs):
        ret = type('', (), {})()
        ret.returncode = 0
        ret.stdout = RET_ID
        return ret

    @mock.patch("subprocess.run", side_effect=_mock_run_func)
    def test_invoker_00(self, mock_run):
        exporting_request = backend_pb2.TaskReqExporting()
        in_dataset_ids = [self._base_task_id]
        exporting_request.format = mir_cmd_pb.ObjectType.OT_DET_BOX_VOC_XML
        exporting_request.asset_dir = self._storage_root
        exporting_request.pred_dir = self._storage_root
        exporting_request.gt_dir = self._storage_root
        req_create_task = backend_pb2.ReqCreateTask()
        req_create_task.task_type = mir_cmd_pb.TaskType.TaskTypeExportData
        req_create_task.no_task_monitor = True
        req_create_task.exporting.CopyFrom(exporting_request)
        assets_config = {'assetskvlocation': self._storage_root}
        working_dir = os.path.join(self._sandbox_root, "work_dir",
                                   mir_cmd_pb.TaskType.Name(mir_cmd_pb.TaskType.TaskTypeExportData), self._task_id,
                                   'sub_task', self._task_id)

        response = make_invoker_cmd_call(invoker=RequestTypeToInvoker[backend_pb2.TASK_CREATE],
                                         sandbox_root=self._sandbox_root,
                                         assets_config=assets_config,
                                         req_type=backend_pb2.TASK_CREATE,
                                         user_id=self._user_name,
                                         repo_id=self._mir_repo_name,
                                         task_id=self._task_id,
                                         in_dataset_ids=in_dataset_ids,
                                         req_create_task=req_create_task)
        print(MessageToDict(response))

        expected_cmd_exporting = (
            f"mir export --root {self._mir_repo_root} --media-location {self._storage_root} "
            f"--asset-dir {self._storage_root} --src-revs {in_dataset_ids[0]}@{in_dataset_ids[0]} "
            f"--anno-format det-voc "
            f"--user-label-file {test_utils.user_label_file(self._sandbox_root, self._user_name)} "
            f"-w {working_dir} --pred-dir {self._storage_root} --gt-dir {self._storage_root}")
        mock_run.assert_has_calls(calls=[
            mock.call(expected_cmd_exporting.split(' '), capture_output=True, text=True),
        ])

        expected_ret = backend_pb2.GeneralResp()
        expected_dict = {'message': RET_ID}
        ParseDict(expected_dict, expected_ret)
        self.assertEqual(response, expected_ret)
