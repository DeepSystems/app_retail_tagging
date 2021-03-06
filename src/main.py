import sys
import json
import os
import threading
from collections import defaultdict
from pandas import read_excel
import numpy as np
import supervisely_lib as sly

my_app = sly.AppService()

REMOTE_DIRECTORY_PATH = "/upc_references"
LOCAL_DIRECTORY_PATH = os.path.join(my_app.data_dir, REMOTE_DIRECTORY_PATH[1:])
sly.fs.ensure_base_path(LOCAL_DIRECTORY_PATH)

FNAME_URL = "upc_ref_url.json"
FNAME_RES_UPC_BATCHES = "res_upc_batches.json"
FNAME_RES_USER_UPC_BATCHES = "res_user_upc_batches.json"
FNAME_CATALOG = "product_catalog.xlsx"
TAG_NAME = 'UPC CODE'

PRODUCT_CLASS_NAME = "Product"

user2upc = defaultdict(list)
upc2catalog = defaultdict(dict)
upc_gallery = defaultdict(list)

anns = {}
anns_lock = threading.Lock()
metas = {}
metas_lock = threading.Lock()


def get_annotation(api: sly.Api, project_id, image_id, figure_id=None):
    def _download_ann(force=False):
        meta = get_project_meta(api, project_id, force)
        ann_json = api.annotation.download(image_id).annotation
        ann = sly.Annotation.from_json(ann_json, meta)

        global anns_lock
        anns_lock.acquire()
        anns[image_id] = ann
        anns_lock.release()

    global anns
    if image_id not in anns:
        _download_ann()

    if figure_id is not None:
        ids = [label.geometry.sly_id for label in anns[image_id].labels]
        if figure_id not in ids:
            _download_ann(force=True)

    return anns[image_id]


def get_project_meta(api: sly.Api, project_id, force=False):
    global metas
    if project_id not in metas or force is True:
        meta_json = api.project.get_meta(project_id)
        meta = sly.ProjectMeta.from_json(meta_json)

        upc_tag_meta = meta.get_tag_meta(TAG_NAME)
        if upc_tag_meta is None:
            meta = meta.add_tag_meta(sly.TagMeta(TAG_NAME, sly.TagValueType.ANY_STRING))
            api.project.update_meta(project_id, meta.to_json())

            # get meta from server again to access tag_id (tag_meta_id)
            meta_json = api.project.get_meta(project_id)
            meta = sly.ProjectMeta.from_json(meta_json)

        global metas_lock
        metas_lock.acquire()
        metas[project_id] = meta
        metas_lock.release()

    return metas[project_id]


def get_first_id(ann: sly.Annotation):
    for idx, label in enumerate(ann.labels):
        if label.obj_class.name == PRODUCT_CLASS_NAME:
            return label.geometry.sly_id
    return None


def get_prev_id(ann: sly.Annotation, active_figure_id):
    prev_idx = None
    for idx, label in enumerate(ann.labels):
        if label.geometry.sly_id == active_figure_id:
            if prev_idx is None:
                return None
            return ann.labels[prev_idx].geometry.sly_id
        if label.obj_class.name == PRODUCT_CLASS_NAME:
            prev_idx = idx


def get_next_id(ann: sly.Annotation, active_figure_id):
    need_search = False
    for idx, label in enumerate(ann.labels):
        if label.geometry.sly_id == active_figure_id:
            need_search = True
            continue
        if need_search:
            if label.obj_class.name == PRODUCT_CLASS_NAME:
                return label.geometry.sly_id
    return None


def select_object(api: sly.Api, task_id, context, find_func, show_msg=False):
    user_id = context["userId"]
    image_id = context["imageId"]
    project_id = context["projectId"]
    ann_tool_session = context["sessionId"]

    ann = get_annotation(api, project_id, image_id)

    active_figure_id = context["figureId"]
    if active_figure_id is None:
        active_figure_id = get_first_id(ann)
    else:
        active_figure_id = find_func(ann, active_figure_id)
        if show_msg is True and active_figure_id is None:
            api.app.set_field(task_id, "state.dialogVisible", True)

    if active_figure_id is not None:
        api.img_ann_tool.set_figure(ann_tool_session, active_figure_id)
        api.img_ann_tool.zoom_to_figure(ann_tool_session, active_figure_id, 2)


@my_app.callback("prev_object")
@sly.timeit
def prev_object(api: sly.Api, task_id, context, state, app_logger):
    select_object(api, task_id, context, get_prev_id)


@my_app.callback("next_object")
@sly.timeit
def next_object(api: sly.Api, task_id, context, state, app_logger):
    select_object(api, task_id, context, get_next_id, show_msg=True)


@my_app.callback("assign_tag")
@sly.timeit
def assign_tag(api: sly.Api, task_id, context, state, app_logger):
    global user2upc

    project_id = context["projectId"]
    meta = get_project_meta(api, project_id)

    user_id = context["userId"]
    user2selectedUpc = state["user2selectedUpc"]
    selected_tag_index = user2selectedUpc[str(user_id)]
    selected_upc = user2upc[user_id][selected_tag_index]["upc"]

    active_figure_id = context["figureId"]
    if active_figure_id is None:
        sly.logger.warn("Figure is not selected.")

    tag_meta = meta.get_tag_meta(TAG_NAME)
    api.advanced.add_tag_to_object(tag_meta.sly_id, active_figure_id, value=selected_upc)


@my_app.callback("multi_assign_tag")
@sly.timeit
def multi_assign_tag(api: sly.Api, task_id, context, state, app_logger):
    global user2upc

    project_id = context["projectId"]
    image_id = context["imageId"]
    user_id = context["userId"]
    user2selectedUpc = state["user2selectedUpc"]

    meta = get_project_meta(api, project_id)
    selected_tag_index = user2selectedUpc[str(user_id)]
    selected_upc = user2upc[user_id][selected_tag_index]["upc"]

    active_figure_id = context["figureId"]
    if active_figure_id is None:
        sly.logger.warn("Figure is not selected.")

    ann = get_annotation(api, project_id, image_id, active_figure_id)
    selected_label = None
    for label in ann.labels:
        if label.geometry.sly_id == active_figure_id:
            selected_label = label
            break

    tag_meta = meta.get_tag_meta(TAG_NAME)
    for idx, label in enumerate(ann.labels):
        if label.geometry.to_bbox().intersects_with(selected_label.geometry.to_bbox()):
            api.advanced.add_tag_to_object(tag_meta.sly_id, label.geometry.sly_id, value=selected_upc)

def download_remote_files(api, team_id):
    sly.fs.ensure_base_path(LOCAL_DIRECTORY_PATH)
    for fname in [FNAME_URL, FNAME_RES_UPC_BATCHES, FNAME_RES_USER_UPC_BATCHES, FNAME_CATALOG]:
        remote_path = os.path.join(REMOTE_DIRECTORY_PATH, fname)
        if not api.file.exists(team_id, remote_path):
            raise FileExistsError("File {!r} does not exist".format(remote_path))
        local_path = os.path.join(LOCAL_DIRECTORY_PATH, fname)
        api.file.download(team_id, remote_path, local_path)

def init_user_2_upc(api, team_id):
    upc_url = sly.json.load_json_file(os.path.join(LOCAL_DIRECTORY_PATH, FNAME_URL))
    upc_batch = sly.json.load_json_file(os.path.join(LOCAL_DIRECTORY_PATH, FNAME_RES_UPC_BATCHES))
    user_upc_batch = sly.json.load_json_file(os.path.join(LOCAL_DIRECTORY_PATH, FNAME_RES_USER_UPC_BATCHES))

    global user2upc, upc_gallery

    for user, upc_batches in user_upc_batch.items():
        # @TODO: only for debug
        #user = "admin"
        user_info = api.user.get_member_info_by_login(team_id, user)
        if user_info is None:
            team_info = api.team.get_info_by_id(team_id)
            raise RuntimeError("User {!r} no found in team {!r}".format(user, team_info.name))
        for batch_id in upc_batches:
            for upc_code in upc_batch[str(batch_id)]:
                first_url = True
                for url in upc_url[upc_code]:
                    # @TODO: hardcode for quantigo
                    #url = url.replace("http://quantigo.supervise.ly:11111/",
                    #                  "http://quantigo.supervise.ly:11111/h5un6l2bnaz1vj8a9qgms4-public/")
                    upc_gallery[upc_code].append([url])
                    if "_full" in url:
                        continue
                    if first_url is False:
                        continue
                    first_url = False
                    user2upc[user_info.id].append({"upc": upc_code, "image_url": url})
        #@TODO: only for debug
        #break

def init_catalog():
    global upc2catalog
    sheets = read_excel(os.path.join(LOCAL_DIRECTORY_PATH, FNAME_CATALOG), sheet_name=None)
    catalog = sheets[list(sheets.keys())[0]]  # get first sheet from excel
    sly.logger.info("Size of catalog: {}".format(len(catalog)))
    upcs = list(catalog["UPC CODE"])
    # upc = '7861042566762'
    for upc in upcs:
        res = catalog[catalog['UPC CODE'] == np.int64(upc)]
        info = json.loads(res.to_json(orient="records"))
        if len(info) != 1:
            info = {}
        else:
            info = info[0]
        upc2catalog[upc] = info

def main():
    api = sly.Api.from_env()

    team_id = os.environ["TEAM_ID"]
    download_remote_files(api, team_id)
    init_user_2_upc(api, team_id)
    init_catalog()

    user2selectedUpc = {}
    for key, value in user2upc.items():
        user2selectedUpc[key] = 0

    user2upcIndex2Info = defaultdict(dict)
    for user_id, upcs in user2upc.items():
        for idx, upc_link in enumerate(upcs):
            info = upc2catalog[np.int64(upc_link["upc"])]
            user2upcIndex2Info[user_id][idx] = info

    user2upcIndex2upcGallery = defaultdict(lambda: defaultdict(dict))
    for user_id, upcs in user2upc.items():
        for idx, upc_link in enumerate(upcs):
            g = upc_gallery[upc_link["upc"]]
            user2upcIndex2upcGallery[user_id][idx] = g

    data = {
        "user2upc": user2upc,
        "user2upcIndex2Info": user2upcIndex2Info,
        "user2upcIndex2upcGallery": user2upcIndex2upcGallery,
        "demoGallery": [["https://i.imgur.com/llPpFm0.jpeg"]]
    }

    # state
    state = {
        "dialogVisible": False,
        "user2selectedUpc": user2selectedUpc
    }

    # # start event after successful service run
    # events = [
    #     {
    #         "state": {},
    #         "context": {},
    #         "command": "calculate"
    #     }
    # ]

    # Run application service
    my_app.run(data=data, state=state)


if __name__ == "__main__":
    sly.main_wrapper("main", main)

    # try:
    #     main()
    # except Exception as e:
    #     sly.logger.critical('Unexpected exception in main.', exc_info=True, extra={
    #         'event_type': sly.EventType.TASK_CRASHED,
    #         'exc_str': str(e),
    #     })
    #     # loglevel = os.getenv('LOG_LEVEL', 'TRACE')
    #     import logging
    #     if logging.getLevelName(sly.logger.level) in ["TRACE", "DEBUG"]:
    #         raise e

#@TODO:
# context + state по всем юзерам? + там будет labelerLogin, api_token, и тд