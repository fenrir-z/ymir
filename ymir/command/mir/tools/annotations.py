from collections import defaultdict
import enum
import io
import json
import logging
import os
from typing import Any, Callable, Dict, List, Set, Tuple, Union

from google.protobuf.json_format import ParseDict
import numpy as np
from PIL import Image, UnidentifiedImageError
import xmltodict
import yaml

from mir.tools import class_ids
from mir.tools.code import MirCode
from mir.tools.errors import MirRuntimeError
from mir.tools.models import ModelStorage
from mir.tools.phase_logger import PhaseLoggerCenter
from mir.protos import mir_command_pb2 as mirpb


class UnknownTypesStrategy(str, enum.Enum):
    STOP = 'stop'
    IGNORE = 'ignore'
    ADD = 'add'


def parse_anno_format(anno_format_str: str) -> "mirpb.AnnoFormat.V":
    _anno_dict: Dict[str, mirpb.AnnoFormat.V] = {
        # compatible with legacy format.
        "voc": mirpb.AnnoFormat.AF_DET_PASCAL_VOC,
        "ark": mirpb.AnnoFormat.AF_DET_ARK_JSON,
        "ls_json": mirpb.AnnoFormat.AF_DET_LS_JSON,
        "det-voc": mirpb.AnnoFormat.AF_DET_PASCAL_VOC,
        "det-ark": mirpb.AnnoFormat.AF_DET_ARK_JSON,
        "det-ls-json": mirpb.AnnoFormat.AF_DET_LS_JSON,
        "seg-poly": mirpb.AnnoFormat.AF_SEG_POLYGON,
        "seg-mask": mirpb.AnnoFormat.AF_SEG_MASK,
    }
    return _anno_dict.get(anno_format_str.lower(), mirpb.AnnoFormat.AF_NO_ANNOTATION)


def parse_anno_type(anno_type_str: str) -> "mirpb.AnnoType.V":
    _anno_dict: Dict[str, mirpb.AnnoType.V] = {
        "det-box": mirpb.AnnoType.AT_DET_BOX,
        "seg-poly": mirpb.AnnoType.AT_SEG_POLYGON,
        "seg-mask": mirpb.AnnoType.AT_SEG_MASK,
    }
    return _anno_dict.get(anno_type_str.lower(), mirpb.AnnoType.AT_UNKNOWN)


def _annotation_parse_func(anno_type: "mirpb.AnnoType.V") -> Callable:
    _func_dict: Dict["mirpb.AnnoType.V", Callable] = {
        mirpb.AnnoType.AT_DET_BOX: _import_annotations_voc_xml,
        mirpb.AnnoType.AT_SEG_POLYGON: _import_annotations_voc_xml,
        mirpb.AnnoType.AT_SEG_MASK: _import_annotations_seg_mask,
    }
    if anno_type not in _func_dict:
        raise NotImplementedError
    return _func_dict[anno_type]


def _object_dict_to_annotation(object_dict: dict, cid: int) -> mirpb.ObjectAnnotation:
    # Fill shared fields.
    annotation = mirpb.ObjectAnnotation()
    annotation.class_id = cid
    annotation.score = float(object_dict.get('confidence', '-1.0'))
    annotation.anno_quality = float(object_dict.get('box_quality', '-1.0'))
    tags = object_dict.get('tags', {})  # tags could be None
    if tags:
        annotation.tags.update(tags)

    if object_dict.get('bndbox'):
        bndbox_dict: Dict[str, Any] = object_dict['bndbox']
        xmin = int(float(bndbox_dict['xmin']))
        ymin = int(float(bndbox_dict['ymin']))
        xmax = int(float(bndbox_dict['xmax']))
        ymax = int(float(bndbox_dict['ymax']))
        width = xmax - xmin + 1
        height = ymax - ymin + 1

        annotation.box.x = xmin
        annotation.box.y = ymin
        annotation.box.w = width
        annotation.box.h = height
        annotation.box.rotate_angle = float(bndbox_dict.get('rotate_angle', '0.0'))
    elif object_dict.get('polygon'):
        raise NotImplementedError
    else:
        raise MirRuntimeError(error_code=MirCode.RC_CMD_INVALID_ARGS, error_message='no value for bndbox or polygon')
    return annotation


# import-dataset
def import_annotations(mir_annotation: mirpb.MirAnnotations, label_storage_file: str, prediction_dir_path: str,
                       groundtruth_dir_path: str, map_hashed_filename: Dict[str, str],
                       unknown_types_strategy: UnknownTypesStrategy, anno_type: "mirpb.AnnoType.V",
                       phase: str) -> Dict[str, int]:
    anno_import_result: Dict[str, int] = defaultdict(int)

    # read type_id_name_dict and type_name_id_dict
    class_type_manager = class_ids.load_or_create_userlabels(label_storage_file=label_storage_file)
    logging.info("loaded type id and names: %d", len(class_type_manager.all_ids()))

    if prediction_dir_path:
        logging.info(f"wrting prediction in {prediction_dir_path}")
        _import_annotations_from_dir(
            map_hashed_filename=map_hashed_filename,
            mir_annotation=mir_annotation,
            annotations_dir_path=prediction_dir_path,
            class_type_manager=class_type_manager,
            unknown_types_strategy=unknown_types_strategy,
            accu_new_class_names=anno_import_result,
            image_annotations=mir_annotation.prediction,
            anno_type=anno_type,
        )
        _import_annotation_meta(class_type_manager=class_type_manager,
                                annotations_dir_path=prediction_dir_path,
                                task_annotations=mir_annotation.prediction)
    PhaseLoggerCenter.update_phase(phase=phase, local_percent=0.5)

    if groundtruth_dir_path:
        logging.info(f"wrting ground-truth in {groundtruth_dir_path}")
        _import_annotations_from_dir(
            map_hashed_filename=map_hashed_filename,
            mir_annotation=mir_annotation,
            annotations_dir_path=groundtruth_dir_path,
            class_type_manager=class_type_manager,
            unknown_types_strategy=unknown_types_strategy,
            accu_new_class_names=anno_import_result,
            image_annotations=mir_annotation.ground_truth,
            anno_type=anno_type,
        )
    PhaseLoggerCenter.update_phase(phase=phase, local_percent=1.0)

    if unknown_types_strategy == UnknownTypesStrategy.STOP and anno_import_result:
        raise MirRuntimeError(error_code=MirCode.RC_CMD_UNKNOWN_TYPES,
                              error_message=f"{list(anno_import_result.keys())}")

    return anno_import_result


def _import_annotations_from_dir(map_hashed_filename: Dict[str, str], mir_annotation: mirpb.MirAnnotations,
                                 annotations_dir_path: str, class_type_manager: class_ids.UserLabels,
                                 unknown_types_strategy: UnknownTypesStrategy, accu_new_class_names: Dict[str, int],
                                 image_annotations: mirpb.SingleTaskAnnotations, anno_type: "mirpb.AnnoType.V") -> None:
    # todo: temp solution: set to seg type if SegmentationClass and labelmap.txt exist.
    #   will be removed once seg type can be passed via web.
    if (os.path.isdir(os.path.join(annotations_dir_path, "SegmentationClass"))
            and os.path.isfile(os.path.join(annotations_dir_path, "labelmap.txt"))):
        anno_type = mirpb.AnnoType.AT_SEG_MASK

    image_annotations.type = anno_type
    _annotation_parse_func(anno_type)(
        map_hashed_filename=map_hashed_filename,
        mir_annotation=mir_annotation,
        annotations_dir_path=annotations_dir_path,
        class_type_manager=class_type_manager,
        unknown_types_strategy=unknown_types_strategy,
        accu_new_class_names=accu_new_class_names,
        image_annotations=image_annotations,
    )

    logging.warning(f"imported {len(image_annotations.image_annotations)} / {len(map_hashed_filename)} annotations")


def _import_annotations_seg_mask(map_hashed_filename: Dict[str, str], mir_annotation: mirpb.MirAnnotations,
                                 annotations_dir_path: str, class_type_manager: class_ids.UserLabels,
                                 unknown_types_strategy: UnknownTypesStrategy, accu_new_class_names: Dict[str, int],
                                 image_annotations: mirpb.SingleTaskAnnotations) -> None:
    map_cname_color = _parse_labelmap(label_map_file=os.path.join(annotations_dir_path, 'labelmap.txt'),
                                      class_type_manager=class_type_manager)

    # batch add all names, including unknown/known names.
    if unknown_types_strategy == UnknownTypesStrategy.ADD:
        class_type_manager.add_main_names(list(map_cname_color.keys()))

    # build color map, map all unknown classes to background (0, 0, 0).
    map_color_cid: Dict[Tuple[int, int, int], int] = {}
    for name, color in map_cname_color.items():
        cid, cname = class_type_manager.id_and_main_name_for_name(name=name)
        if cname not in accu_new_class_names:
            accu_new_class_names[cname] = 0

        if cid >= 0:
            point = mirpb.IntPoint()
            point.x, point.y, point.z = color
            image_annotations.map_id_color[cid].CopyFrom(point)
            map_color_cid[color] = cid

    semantic_mask_dir = os.path.join(annotations_dir_path, "SegmentationClass")
    for asset_hash, main_file_name in map_hashed_filename.items():
        # for each asset, import it's annotations
        annotation_file = os.path.join(semantic_mask_dir, main_file_name + '.png')
        if not os.path.isfile(annotation_file):
            continue
        try:
            mask_image = Image.open(annotation_file)
        except (UnidentifiedImageError, OSError) as e:
            logging.info(f"{type(e).__name__}: {e}\nannotation_file: {annotation_file}\n")
            continue
        asset_type_str: str = mask_image.format.lower()
        if asset_type_str != 'png':
            logging.error(f"cannot import annotation_file: {annotation_file} as type: {asset_type_str}")
            continue

        mask_image = mask_image.convert('RGB')
        img_class_ids: Set[int] = set()
        width, height = mask_image.size
        img: np.ndarray = np.array(mask_image)
        np_mask: np.ndarray = np.zeros(shape=(height, width, 3), dtype=np.uint8)
        for color in map_color_cid:
            r = img[:, :, 0] == color[0]
            g = img[:, :, 1] == color[1]
            b = img[:, :, 2] == color[2]

            mask = r & g & b
            if np.any(mask):
                np_mask[mask] = color
                img_class_ids.add(map_color_cid[color])

        new_mask_image: Image.Image = Image.fromarray(np_mask)
        with io.BytesIO() as output:
            new_mask_image.save(output, format="PNG")
            image_annotations.image_annotations[asset_hash].masks.append(
                mirpb.MaskAnnotation(semantic_mask=output.getvalue()))
        image_annotations.image_annotations[asset_hash].img_class_ids[:] = list(img_class_ids)


def _import_annotations_voc_xml(map_hashed_filename: Dict[str, str], mir_annotation: mirpb.MirAnnotations,
                                annotations_dir_path: str, class_type_manager: class_ids.UserLabels,
                                unknown_types_strategy: UnknownTypesStrategy, accu_new_class_names: Dict[str, int],
                                image_annotations: mirpb.SingleTaskAnnotations) -> None:
    add_if_not_found = (unknown_types_strategy == UnknownTypesStrategy.ADD)
    task_class_ids: Set[int] = set()
    for asset_hash, main_file_name in map_hashed_filename.items():
        # for each asset, import it's annotations
        annotation_file = os.path.join(annotations_dir_path, main_file_name + '.xml')
        if not os.path.isfile(annotation_file):
            continue

        with open(annotation_file, 'r') as f:
            annos_xml_str = f.read()
        if not annos_xml_str:
            logging.error(f"cannot open annotation_file: {annotation_file}")
            continue

        annos_dict: dict = xmltodict.parse(annos_xml_str)['annotation']
        # cks
        cks = annos_dict.get('cks', {})  # cks could be None
        if cks:
            mir_annotation.image_cks[asset_hash].cks.update(cks)
        mir_annotation.image_cks[asset_hash].image_quality = float(annos_dict.get('image_quality', '-1.0'))

        # annotations and tags
        objects: Union[List[dict], dict] = annos_dict.get('object', [])
        if isinstance(objects, dict):
            # when there's only ONE object node in xml, it will be parsed to a dict, not a list
            objects = [objects]

        anno_idx = 0
        img_class_ids: Set[int] = set()
        for object_dict in objects:
            cid, new_type_name = class_type_manager.id_and_main_name_for_name(name=object_dict['name'])

            # check if seen this class_name.
            if new_type_name in accu_new_class_names:
                accu_new_class_names[new_type_name] += 1
            else:
                # for unseen class_name, only care about negative cid.
                if cid < 0:
                    if add_if_not_found:
                        cid, _ = class_type_manager.add_main_name(main_name=new_type_name)
                    accu_new_class_names[new_type_name] = 0

            if cid >= 0:
                annotation = _object_dict_to_annotation(object_dict, cid)
                annotation.index = anno_idx
                image_annotations.image_annotations[asset_hash].boxes.append(annotation)
                anno_idx += 1

                img_class_ids.add(cid)

        task_class_ids.update(img_class_ids)

        image_annotations.image_annotations[asset_hash].img_class_ids[:] = list(img_class_ids)

    image_annotations.task_class_ids[:] = list(task_class_ids)


def _import_annotation_meta(class_type_manager: class_ids.UserLabels, annotations_dir_path: str,
                            task_annotations: mirpb.SingleTaskAnnotations) -> None:
    annotation_meta_path = os.path.join(annotations_dir_path, 'meta.yaml')
    if not os.path.isfile(annotation_meta_path):
        return

    try:
        with open(annotation_meta_path, 'r') as f:
            annotation_meta_dict = yaml.safe_load(f)
    except Exception:
        annotation_meta_dict = None
    if not isinstance(annotation_meta_dict, dict):
        raise MirRuntimeError(error_code=MirCode.RC_CMD_INVALID_META_YAML_FILE,
                              error_message='Invalid meta.yaml')

    # model
    if 'model' in annotation_meta_dict:
        ParseDict(annotation_meta_dict['model'], task_annotations.model)

    # eval_class_ids
    eval_class_names = annotation_meta_dict.get('eval_class_names') or task_annotations.model.class_names
    task_annotations.eval_class_ids[:] = set(
        class_type_manager.id_for_names(list(eval_class_names), drop_unknown_names=True)[0])

    # executor_config
    if 'executor_config' in annotation_meta_dict:
        task_annotations.executor_config = json.dumps(annotation_meta_dict['executor_config'])


def copy_annotations_pred_meta(src_task_annotations: mirpb.SingleTaskAnnotations,
                               dst_task_annotations: mirpb.SingleTaskAnnotations) -> None:
    dst_task_annotations.eval_class_ids[:] = src_task_annotations.eval_class_ids
    dst_task_annotations.executor_config = src_task_annotations.executor_config
    dst_task_annotations.model.CopyFrom(src_task_annotations.model)


def _parse_labelmap(label_map_file: str, class_type_manager: class_ids.UserLabels) -> Dict[str, Tuple[int, int, int]]:
    # fortmat ref:
    # https://github.com/acesso-io/techcore-cvat/tree/develop/cvat/apps/dataset_manager/formats#segmentation-mask-import
    # single line reprs label&color map, e.g. "ego vehicle:0,181,0::" or "road:07::"; otherwise "..." for place-holder.
    if not os.path.isfile(label_map_file):
        raise MirRuntimeError(error_code=MirCode.RC_CMD_INVALID_ARGS, error_message="labelmap.txt is required.")
    with open(label_map_file) as f:
        records = f.readlines()

    # parse label_map.txt into map_cname_color.
    map_cname_color: Dict[str, Tuple[int, int, int]] = {}
    for record in records:
        record = record.strip()
        if ':' not in record or not record:
            logging.info("place-holder line, skipping.")
            continue

        record_split = record.split(':')
        if len(record_split) != 4:
            logging.info(f"invalid labelmap line: {record}")
            continue
        pos_ints: List[int] = [int(x) for x in record_split[1].split(',')]
        if len(pos_ints) == 1:  # single channel to 3 channels.
            pos_ints = [pos_ints[0], pos_ints[0], pos_ints[0]]
        if len(pos_ints) != 3:
            logging.info(f"invalid labelmap color idx: {pos_ints}")
            continue
        if pos_ints == (0, 0, 0):
            logging.info("ignore background color.")
            continue
        _, cname = class_type_manager.id_and_main_name_for_name(name=record_split[0])
        map_cname_color[cname] = (pos_ints[0], pos_ints[1], pos_ints[2])

    return map_cname_color


# copy
def copy_annotations(mir_annotations: mirpb.MirAnnotations, mir_context: mirpb.MirContext,
                     data_label_storage_file: str, label_storage_file: str) -> dict:
    if (data_label_storage_file == label_storage_file
            or (len(mir_annotations.prediction.image_annotations) == 0
                and len(mir_annotations.ground_truth.image_annotations) == 0)):
        # no need to make any changes to annotations
        return {}

    # need to change class ids
    src_class_id_mgr = class_ids.load_or_create_userlabels(label_storage_file=data_label_storage_file)
    dst_class_id_mgr = class_ids.load_or_create_userlabels(label_storage_file=label_storage_file)

    src_to_dst_ids = {
        src_class_id_mgr.id_and_main_name_for_name(n)[0]: dst_class_id_mgr.id_and_main_name_for_name(n)[0]
        for n in src_class_id_mgr.all_main_names()
    }

    _change_annotations_type_ids(single_task_annotations=mir_annotations.prediction, src_to_dst_ids=src_to_dst_ids)
    _change_annotations_type_ids(single_task_annotations=mir_annotations.ground_truth, src_to_dst_ids=src_to_dst_ids)

    return _gen_unknown_names_and_count(src_class_id_mgr=src_class_id_mgr,
                                        mir_context=mir_context,
                                        src_to_dst_ids=src_to_dst_ids)


def _change_annotations_type_ids(
    single_task_annotations: mirpb.SingleTaskAnnotations,
    src_to_dst_ids: Dict[int, int],
) -> None:
    for single_image_annotations in single_task_annotations.image_annotations.values():
        dst_image_annotations: List[mirpb.ObjectAnnotation] = []
        for annotation in single_image_annotations.boxes:
            dst_class_id = src_to_dst_ids[annotation.class_id]
            if dst_class_id >= 0:
                annotation.class_id = dst_class_id
                dst_image_annotations.append(annotation)
        del single_image_annotations.boxes[:]
        single_image_annotations.boxes.extend(dst_image_annotations)

    dst_eval_class_ids: List[int] = []
    for src_class_id in single_task_annotations.eval_class_ids:
        dst_class_id = src_to_dst_ids[src_class_id]
        if dst_class_id >= 0:
            dst_eval_class_ids.append(dst_class_id)
    single_task_annotations.eval_class_ids[:] = dst_eval_class_ids


def _gen_unknown_names_and_count(src_class_id_mgr: class_ids.UserLabels, mir_context: mirpb.MirContext,
                                 src_to_dst_ids: Dict[int, int]) -> Dict[str, int]:
    all_src_class_ids = set(mir_context.pred_stats.class_ids_cnt.keys()) | set(
        mir_context.gt_stats.class_ids_cnt.keys())
    unknown_src_class_ids = {src_id for src_id in all_src_class_ids if src_to_dst_ids[src_id] == -1}
    if not unknown_src_class_ids:
        return {}

    unknown_names_and_count: Dict[str, int] = {}
    for src_id in unknown_src_class_ids:
        name = src_class_id_mgr.main_name_for_id(src_id)
        cnt_gt: int = mir_context.pred_stats.class_ids_cnt[src_id]
        cnt_pred: int = mir_context.gt_stats.class_ids_cnt[src_id]
        unknown_names_and_count[name] = cnt_gt + cnt_pred
    return unknown_names_and_count


# filter
def filter_annotations(mir_annotations: mirpb.MirAnnotations, asset_ids_set: Set[str]) -> mirpb.MirAnnotations:
    matched_mir_annotations = mirpb.MirAnnotations()

    _gen_filter_task_annotations(src_task_annotations=mir_annotations.ground_truth,
                                 dst_task_annotations=matched_mir_annotations.ground_truth,
                                 asset_ids=asset_ids_set)
    _gen_filter_task_annotations(src_task_annotations=mir_annotations.prediction,
                                 dst_task_annotations=matched_mir_annotations.prediction,
                                 asset_ids=asset_ids_set)

    image_ck_asset_ids = asset_ids_set & set(mir_annotations.image_cks.keys())
    for asset_id in image_ck_asset_ids:
        matched_mir_annotations.image_cks[asset_id].CopyFrom(mir_annotations.image_cks[asset_id])

    copy_annotations_pred_meta(src_task_annotations=mir_annotations.prediction,
                               dst_task_annotations=matched_mir_annotations.prediction)

    return matched_mir_annotations


def _gen_filter_task_annotations(src_task_annotations: mirpb.SingleTaskAnnotations,
                                 dst_task_annotations: mirpb.SingleTaskAnnotations, asset_ids: Set[str]) -> None:
    dst_task_annotations.type = src_task_annotations.type
    joint_ids = asset_ids & src_task_annotations.image_annotations.keys()
    for asset_id in joint_ids:
        dst_task_annotations.image_annotations[asset_id].CopyFrom(src_task_annotations.image_annotations[asset_id])


# merge
def merge_annotations(host_mir_annotations: mirpb.MirAnnotations, guest_mir_annotations: mirpb.MirAnnotations,
                      strategy: str) -> None:
    """
    add all annotations in guest_mir_annotations into host_mir_annotations

    Args:
        host_mir_annotations (mirpb.MirAnnotations): host annotations
        guest_mir_annotations (mirpb.MirAnnotations): guest annotations
        strategy (str): host, guest, stop

    Raises:
        ValueError: if host or guest annotations empty
        ValueError: if conflicts occured in strategy stop
    """
    _merge_pair_annotations(host_annotation=host_mir_annotations.prediction,
                            guest_annotation=guest_mir_annotations.prediction,
                            target_annotation=host_mir_annotations.prediction,
                            strategy=strategy)
    host_mir_annotations.prediction.eval_class_ids.extend(guest_mir_annotations.prediction.eval_class_ids)

    _merge_pair_annotations(host_annotation=host_mir_annotations.ground_truth,
                            guest_annotation=guest_mir_annotations.ground_truth,
                            target_annotation=host_mir_annotations.ground_truth,
                            strategy=strategy)

    _merge_annotation_image_cks(host_mir_annotations=host_mir_annotations,
                                guest_mir_annotations=guest_mir_annotations,
                                target_mir_annotations=host_mir_annotations,
                                strategy=strategy)


def _merge_pair_annotations(host_annotation: mirpb.SingleTaskAnnotations, guest_annotation: mirpb.SingleTaskAnnotations,
                            target_annotation: mirpb.SingleTaskAnnotations, strategy: str) -> None:
    if (host_annotation.type != mirpb.AnnoType.AT_UNKNOWN and guest_annotation.type != mirpb.AnnoType.AT_UNKNOWN
            and host_annotation.type != guest_annotation.type):
        raise MirRuntimeError(error_code=MirCode.RC_CMD_INVALID_ANNO_TYPE,
                              error_message='host and guest anno type mismatch')

    target_annotation.type = host_annotation.type or guest_annotation.type

    host_only_ids, guest_only_ids, joint_ids = match_asset_ids(set(host_annotation.image_annotations.keys()),
                                                               set(guest_annotation.image_annotations.keys()))

    if strategy == "stop" and joint_ids:
        raise MirRuntimeError(error_code=MirCode.RC_CMD_MERGE_ERROR,
                              error_message='found conflict annotations in strategy stop')

    for asset_id in host_only_ids:
        target_annotation.image_annotations[asset_id].CopyFrom(host_annotation.image_annotations[asset_id])
    for asset_id in guest_only_ids:
        target_annotation.image_annotations[asset_id].CopyFrom(guest_annotation.image_annotations[asset_id])
    for asset_id in joint_ids:
        if strategy.lower() == "host":
            if asset_id not in target_annotation.image_annotations:
                target_annotation.image_annotations[asset_id].CopyFrom(host_annotation.image_annotations[asset_id])
        elif strategy.lower() == "guest":
            target_annotation.image_annotations[asset_id].CopyFrom(guest_annotation.image_annotations[asset_id])


def _merge_annotation_image_cks(host_mir_annotations: mirpb.MirAnnotations, guest_mir_annotations: mirpb.MirAnnotations,
                                target_mir_annotations: mirpb.MirAnnotations, strategy: str) -> None:
    host_only_ids, guest_only_ids, joint_ids = match_asset_ids(set(host_mir_annotations.image_cks.keys()),
                                                               set(guest_mir_annotations.image_cks.keys()))
    if strategy == "stop" and joint_ids:
        raise MirRuntimeError(error_code=MirCode.RC_CMD_MERGE_ERROR,
                              error_message='found conflict image cks in strategy stop')

    for asset_id in host_only_ids:
        target_mir_annotations.image_cks[asset_id].CopyFrom(host_mir_annotations.image_cks[asset_id])
    for asset_id in guest_only_ids:
        target_mir_annotations.image_cks[asset_id].CopyFrom(guest_mir_annotations.image_cks[asset_id])
    for asset_id in joint_ids:
        if strategy.lower() == "host":
            if asset_id not in target_mir_annotations.image_cks:
                target_mir_annotations.image_cks[asset_id].CopyFrom(host_mir_annotations.image_cks[asset_id])
        elif strategy.lower() == "guest":
            target_mir_annotations.image_cks[asset_id].CopyFrom(guest_mir_annotations.image_cks[asset_id])


def match_asset_ids(host_ids: set, guest_ids: set) -> Tuple[set, set, set]:
    """
    match asset ids

    Args:
        host_ids (set): host ids
        guest_ids (set): guest ids

    Returns:
        Tuple[set, set, set]: host_only_ids, guest_only_ids, joint_ids
    """
    insets = host_ids & guest_ids
    return (host_ids - insets, guest_ids - insets, insets)


# sampling
def sampling_annotations(mir_annotations: mirpb.MirAnnotations, sampled_asset_ids: List[str]) -> mirpb.MirAnnotations:
    sampled_mir_annotations = mirpb.MirAnnotations()

    for asset_id in sampled_asset_ids:
        if asset_id in mir_annotations.prediction.image_annotations:
            sampled_mir_annotations.prediction.image_annotations[asset_id].CopyFrom(
                mir_annotations.prediction.image_annotations[asset_id])
        if asset_id in mir_annotations.ground_truth.image_annotations:
            sampled_mir_annotations.ground_truth.image_annotations[asset_id].CopyFrom(
                mir_annotations.ground_truth.image_annotations[asset_id])

    sampled_mir_annotations.prediction.type = mir_annotations.prediction.type
    sampled_mir_annotations.ground_truth.type = mir_annotations.ground_truth.type
    copy_annotations_pred_meta(src_task_annotations=mir_annotations.prediction,
                               dst_task_annotations=sampled_mir_annotations.prediction)

    return sampled_mir_annotations


# mining
def mining_annotations(work_out_dir: str, asset_ids_set: Set[str], cls_id_mgr: class_ids.UserLabels,
                       model_storage: ModelStorage, add_prediction: bool,
                       mir_annotations: mirpb.MirAnnotations) -> mirpb.MirAnnotations:
    matched_mir_annotations = mirpb.MirAnnotations()

    # predictions
    prediction = matched_mir_annotations.prediction
    prediction.type = model_storage.model_type  # type: ignore
    if add_prediction:
        if model_storage.model_type == mirpb.AnnoType.AT_DET_BOX:
            _get_detbox_infer_annotations(task_annotations=matched_mir_annotations.prediction,
                                          file_path=os.path.join(work_out_dir, 'infer-result.json'),
                                          asset_ids_set=asset_ids_set,
                                          cls_id_mgr=cls_id_mgr)
        elif model_storage.model_type == mirpb.AnnoType.AT_SEG_MASK:
            _import_annotations_seg_mask(map_hashed_filename={asset_id: asset_id
                                                              for asset_id in asset_ids_set},
                                         mir_annotation=matched_mir_annotations,
                                         annotations_dir_path=work_out_dir,
                                         class_type_manager=cls_id_mgr,
                                         unknown_types_strategy=UnknownTypesStrategy.IGNORE,
                                         accu_new_class_names={},
                                         image_annotations=prediction)

        # pred meta
        prediction.eval_class_ids[:] = set(
            cls_id_mgr.id_for_names(model_storage.class_names, drop_unknown_names=True)[0])
        prediction.executor_config = json.dumps(model_storage.executor_config)
        prediction.model.CopyFrom(model_storage.get_model_meta())
    else:
        # use old
        pred_asset_ids = set(mir_annotations.prediction.image_annotations.keys()) & asset_ids_set
        for asset_id in pred_asset_ids:
            prediction.image_annotations[asset_id].CopyFrom(mir_annotations.prediction.image_annotations[asset_id])
        copy_annotations_pred_meta(src_task_annotations=mir_annotations.prediction, dst_task_annotations=prediction)\

    # ground truth
    ground_truth = matched_mir_annotations.ground_truth
    ground_truth.type = mir_annotations.ground_truth.type
    gt_asset_ids = set(mir_annotations.ground_truth.image_annotations.keys()) & asset_ids_set
    for asset_id in gt_asset_ids:
        ground_truth.image_annotations[asset_id].CopyFrom(mir_annotations.ground_truth.image_annotations[asset_id])

    # image cks
    image_ck_asset_ids = set(mir_annotations.image_cks.keys() & asset_ids_set)
    for asset_id in image_ck_asset_ids:
        matched_mir_annotations.image_cks[asset_id].CopyFrom(mir_annotations.image_cks[asset_id])

    return matched_mir_annotations


def _get_detbox_infer_annotations(task_annotations: mirpb.SingleTaskAnnotations, file_path: str,
                                  asset_ids_set: Set[str], cls_id_mgr: class_ids.UserLabels) -> None:
    with open(file_path, 'r') as f:
        results = json.loads(f.read())

    detections = results.get('detection')
    if not isinstance(detections, dict):
        logging.error('invalid infer-result.json')

    for asset_name, annotations_dict in detections.items():
        annotations = annotations_dict.get('boxes')
        if not isinstance(annotations, list):
            logging.error(f"invalid annotations: {annotations}")
            continue

        asset_id = os.path.splitext(os.path.basename(asset_name))[0]
        if asset_id not in asset_ids_set:
            continue

        single_image_annotations = mirpb.SingleImageAnnotations()
        idx = 0
        for annotation_dict in annotations:
            class_id = cls_id_mgr.id_and_main_name_for_name(name=annotation_dict['class_name'])[0]
            # ignore unknown class ids
            if class_id < 0:
                continue

            annotation = mirpb.ObjectAnnotation()
            annotation.index = idx
            ParseDict(annotation_dict['box'], annotation.box)
            annotation.class_id = class_id
            annotation.score = float(annotation_dict.get('score', 0))
            single_image_annotations.boxes.append(annotation)
            idx += 1
        task_annotations.image_annotations[asset_id].CopyFrom(single_image_annotations)
