"""
ProcTHOR FastAPI Server

兼容 HoloScene API 协议的自然语言场景生成服务。
支持两种调用方式：
1. POST /generate — HoloScene 兼容协议：自然语言 query → 异步生成 → task_id 轮询
2. POST /generate_direct — 原生 ProcTHOR 参数化生成（同步直接返回 JSON）

启动方式：
    uvicorn server:app --host 0.0.0.0 --port 8001

接口文档：
    http://localhost:8001/docs
"""
import json
import logging
import os
import re
import tempfile
import threading
import traceback
import uuid
from typing import List, Literal, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from openai import OpenAI
from pydantic import BaseModel, Field, field_validator

from procthor.generation import (
    PROCTHOR10K_ROOM_SPEC_SAMPLER,
    HouseGenerator,
    _create_default_generation_functions,
)
from procthor.generation import materials as materials_module
from procthor.generation.house import NextSamplingStage
from procthor.utils.types import SamplingVars

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="ProcTHOR House Generator API",
    description="兼容 HoloScene 协议的程序化室内场景生成服务。",
    version="1.0.0",
)

# ==============================
# 环境配置
# ==============================
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "sk-")
OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE", "http://10.120.47.138:8000/v1")
OPENAI_MODEL = os.environ.get("LLM_MODEL_NAME", "qwen3.5")

# 项目数据目录
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "scenes")
os.makedirs(DATA_DIR, exist_ok=True)

# ==============================
# 任务状态管理（兼容 HoloScene 异步协议）
# ==============================
tasks_status: dict = {}
tasks_lock = threading.Lock()

# 所有支持的房间布局
VALID_ROOM_SPECS = [
    "8-room-3-bed",
    "7-room-3-bed",
    "12-room-3-bed",
    "12-room",
    "4-room",
    "2-bed-1-bath",
    "5-room",
    "2-bed-2-bath",
    "bedroom-bathroom",
    "kitchen-living-bedroom-room",
    "kitchen-living-bedroom-room2",
    "kitchen-living-room",
    "kitchen",
    "living-room",
    "bedroom",
    "bathroom",
]


# ==============================
# HoloScene 兼容的请求/响应模型
# ==============================
class SceneRequest(BaseModel):
    """与 HoloScene 完全兼容的请求格式 — 自然语言 + 可选控制参数。"""
    query: str = "a simple room"
    add_ceiling: bool = False
    generate_image: bool = False
    generate_video: bool = False
    use_constraint: bool = True
    use_milp: bool = False
    random_selection: bool = False
    single_room: bool = False
    used_assets: List[str] = []


class TaskResponse(BaseModel):
    """与 HoloScene 完全兼容的任务响应格式。"""
    task_id: str
    status: str  # "pending", "processing", "completed", "failed"
    message: str


# ==============================
# 原生 ProcTHOR 参数模型（保留向后兼容）
# ==============================
class GenerateRequest(BaseModel):
    """原生 ProcTHOR 参数 — 直接生成（POST /generate_direct）。"""

    room_spec: Optional[str] = Field(
        default=None,
        description=(
            "房间布局类型。不填则随机选择。可选值：\n"
            + "\n".join(f"- `{s}`" for s in VALID_ROOM_SPECS)
        ),
        examples=["bedroom", "kitchen-living-room", "2-bed-1-bath"],
    )
    seed: Optional[int] = Field(
        default=None,
        description="随机种子，固定后可复现相同结果。不填则每次随机。",
    )
    width: Optional[int] = Field(default=None, ge=4, le=30)
    height: Optional[int] = Field(default=None, ge=4, le=30)
    size_scale: Optional[float] = Field(default=None, ge=1.4, le=2.5)
    max_floor_objects: Optional[Literal[1, 4, 5, 6, 7]] = Field(default=None)
    uniform_wall_material: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    uniform_floor_material: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    solid_wall_color_prob: Optional[float] = Field(default=None, ge=0.0, le=1.0)

    @field_validator("room_spec")
    @classmethod
    def validate_room_spec(cls, v):
        if v is not None and v not in VALID_ROOM_SPECS:
            raise ValueError(f"不支持的 room_spec: '{v}'。可选值：{VALID_ROOM_SPECS}")
        return v


# ==============================
# 自然语言 → ProcTHOR 参数解析（轻量 LLM 调用）
# ==============================
_QUERY_TO_PARAMS_PROMPT = """You are a scene generation parameter parser. Given a natural language description of a house/apartment (may be in Chinese or English), extract the best-matching ProcTHOR room_spec and generation parameters.

Available room_spec values (choose the ONE that best matches the description):
{specs}

Rules:
- "room_spec": Choose the ONE best match from the list above.
  * If the description mentions multiple rooms (e.g., "kitchen, bedroom, living room", "整屋", "全屋", "house with X rooms"), pick a multi-room spec like "4-room", "5-room", "kitchen-living-room", or similar.
  * If the description focuses on a single room type, pick the most specific match.
  * NEVER return null or empty string for room_spec. If unsure, default to "living-room".
- "seed": always null.
- "size_scale": 1.4=cozy/small/intimate, 1.9=normal, 2.5=spacious/large/grand. Infer from descriptive words.
- "max_floor_objects": 1=minimal/sparse, 4=lightly-furnished, 5=normal, 6=well-furnished, 7=cluttered/fully-furnished.

Return ONLY valid JSON (no markdown, no explanation, no extra text):
{{"room_spec": "<value>", "size_scale": <float>, "max_floor_objects": <int>}}"""


def _parse_query_to_params(query: str) -> dict:
    """使用 LLM 将自然语言 query 解析为 ProcTHOR 参数。"""
    try:
        client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_API_BASE, timeout=15)
        prompt = _QUERY_TO_PARAMS_PROMPT.format(specs="\n".join(f"- {s}" for s in VALID_ROOM_SPECS))
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": query},
            ],
            temperature=0.1,
            max_tokens=2000,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # DeepSeek reasoning 模型可能 content 为空，尝试从 reasoning_content 回退
        if not raw:
            raw = (getattr(resp.choices[0].message, "reasoning_content", None) or "").strip()
            # 从 reasoning 中提取 JSON
            m = re.search(r"\{[^{}]*\"room_spec\"[^{}]*\}", raw, re.DOTALL)
            if m:
                raw = m.group(0)
        # 清理可能的 markdown 包裹
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        # 尝试提取 JSON 对象
        m = re.search(r"\{[^{}]*\"room_spec\"[^{}]*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
        if not raw:
            raise ValueError("LLM returned empty response")
        params = json.loads(raw)
        # 兜底：如果 room_spec 不在列表中，用默认值
        if params.get("room_spec") not in VALID_ROOM_SPECS:
            params["room_spec"] = None  # 让 ProcTHOR 自己随机选
        logger.info(f"Parsed query '{query[:60]}...' → {params}")
        return params
    except Exception as e:
        logger.warning(f"Query parsing failed, using defaults: {e}")
        return {}


# ==============================
# 核心生成逻辑
# ==============================
def _generate_house(req: GenerateRequest) -> tuple:
    """执行单次房屋生成，返回 (house_dict, seed)。"""
    if req.uniform_wall_material is not None:
        materials_module.P_ALL_WALLS_SAME = req.uniform_wall_material
    if req.uniform_floor_material is not None:
        materials_module.P_ALL_FLOOR_SAME = req.uniform_floor_material
    if req.solid_wall_color_prob is not None:
        materials_module.P_SAMPLE_SOLID_WALL_COLOR = req.solid_wall_color_prob

    interior_boundary = None
    if req.width is not None and req.height is not None:
        interior_boundary = np.ones((req.height, req.width), dtype=int)

    sampling_vars = None
    if req.size_scale is not None or req.max_floor_objects is not None:
        sampling_vars = SamplingVars(
            interior_boundary_scale=req.size_scale if req.size_scale is not None else 1.9,
            max_floor_objects=req.max_floor_objects if req.max_floor_objects is not None else 7,
        )

    gfs = _create_default_generation_functions()
    hg = HouseGenerator(
        split="train",
        seed=req.seed,
        room_spec=req.room_spec,
        interior_boundary=interior_boundary,
        generation_functions=gfs,
    )
    logger.info(f"Generating: seed={hg.seed} room_spec={req.room_spec or '(random)'}")

    try:
        house, _ = hg.sample(
            partial_house=None,
            return_partial_houses=False,
            sampling_vars=sampling_vars,
            next_sampling_stage=NextSamplingStage["STRUCTURE"],
        )
    finally:
        if hg.controller is not None:
            try:
                hg.controller.stop()
            except Exception:
                pass
            hg.controller = None

    return house, hg.seed


def _house_to_json_file(house, seed: int, query: str = "") -> str:
    """将 House 对象保存为 JSON 文件，返回场景目录下的相对路径。"""
    from datetime import datetime

    # 后处理：为每个 object 补上 roomId（Unity 客户端需要此字段来分配物体到房间）
    _inject_room_ids(house)

    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    dir_name = f"scene_{timestamp}_{seed}"
    result_dir = os.path.join(DATA_DIR, dir_name)
    os.makedirs(result_dir, exist_ok=True)

    json_filename = f"{dir_name}.json"
    json_path = os.path.join(result_dir, json_filename)
    house.to_json(json_path, compressed=False)

    meta = {"query": query, "seed": seed, "generated_at": timestamp}
    with open(os.path.join(result_dir, "metadata.json"), "w") as f:
        json.dump(meta, f)

    return f"{dir_name}/{json_filename}"


def _inject_room_ids(house):
    """为 ProcTHOR 输出中的每个 object 注入 roomId，并填充 rooms[].children。
    ProcTHOR 原始结构：data.objects[]（顶层平铺），rooms[].children 为空。
    Unity 客户端需要：rooms[].children 或 data.floor_objects（按 roomId 分组）。"""
    room_map = {}
    room_children = {}  # roomId → [objects]
    for r in house.data.get("rooms", []):
        rid = r.get("id", "")
        if "|" in rid:
            room_map[rid.split("|")[1]] = rid
        room_children[rid] = []

    for obj in house.data.get("objects", []):
        obj_id = obj.get("id", "")
        if "|" in obj_id:
            room_key = obj_id.split("|")[0]
            rid = room_map.get(room_key, "")
            obj["roomId"] = rid
            if rid:
                room_children.setdefault(rid, []).append(obj)
        for child in obj.get("children", []):
            child_id = child.get("id", "")
            if "|" in child_id:
                room_key = child_id.split("|")[0]
                rid = room_map.get(room_key, "")
                child["roomId"] = rid

    # 注入到 rooms[].children
    for r in house.data.get("rooms", []):
        rid = r.get("id", "")
        r["children"] = room_children.get(rid, [])


# ==============================
# 异步任务执行器（HoloScene 兼容）
# ==============================
def _run_generation_task(task_id: str, request: SceneRequest):
    """后台线程执行生成任务，结果写入 tasks_status。"""
    try:
        with tasks_lock:
            tasks_status[task_id]["status"] = "processing"

        # Step 1: 用 LLM 解析自然语言 query → ProcTHOR 参数
        params = _parse_query_to_params(request.query)
        gen_req = GenerateRequest(**params)

        # Step 2: 执行生成
        house, seed = _generate_house(gen_req)

        # Step 3: 保存到磁盘
        file_path = _house_to_json_file(house, seed, request.query)

        with tasks_lock:
            tasks_status[task_id]["result"] = {
                "status": "completed",
                "file_path": file_path,
                "output_dir": os.path.join(DATA_DIR, os.path.dirname(file_path)),
            }
            tasks_status[task_id]["status"] = "completed"
    except Exception as e:
        logger.error(f"[task:{task_id}] failed: {traceback.format_exc()}")
        with tasks_lock:
            tasks_status[task_id]["result"] = {"status": "failed", "error": str(e)}
            tasks_status[task_id]["status"] = "failed"


# ==============================
# HoloScene 兼容端点 — POST /generate + GET /status/{task_id}
# ==============================
@app.post("/generate", response_model=TaskResponse)
def generate_scene(request: SceneRequest):
    """HoloScene 兼容协议：提交自然语言场景生成任务，返回 task_id 用于轮询。"""
    task_id = str(uuid.uuid4())
    with tasks_lock:
        tasks_status[task_id] = {"status": "pending", "result": None}

    thread = threading.Thread(
        target=_run_generation_task,
        args=(task_id, request),
        daemon=True,
    )
    thread.start()

    return TaskResponse(
        task_id=task_id,
        status="pending",
        message="Scene generation task submitted",
    )


@app.get("/status/{task_id}")
def get_task_status(task_id: str):
    """HoloScene 兼容协议：查询任务状态。"""
    with tasks_lock:
        if task_id not in tasks_status:
            raise HTTPException(status_code=404, detail="Task not found")
        task = tasks_status[task_id]
        resp = {"task_id": task_id, "status": task["status"]}
        if task["status"] == "completed" and task.get("result"):
            resp["file_path"] = task["result"].get("file_path")
        elif task["status"] == "failed" and task.get("result"):
            resp["error"] = task["result"].get("error")
    return resp


@app.get("/download/{file_path:path}")
def download_scene(file_path: str):
    """下载生成的场景 JSON 文件（HoloScene 兼容）。"""
    full_path = os.path.join(DATA_DIR, file_path)
    full_path = os.path.normpath(full_path)
    base_path = os.path.normpath(DATA_DIR)
    if not full_path.startswith(base_path):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    import compress_json
    return compress_json.load(full_path)


# ==============================
# 原生 ProcTHOR 端点 — POST /generate_direct
# ==============================
@app.post(
    "/generate_direct",
    summary="原生参数化生成（同步直接下载 JSON）",
    response_description="返回生成的房屋 JSON 文件下载。",
)
def generate_direct(req: GenerateRequest):
    """原生 ProcTHOR 协议：参数化生成并直接下载 JSON。"""
    try:
        house, seed = _generate_house(req)
        # 将 House 对象转为 dict 用于 JSON 响应
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            house.to_json(tmp_path, compressed=False)
            with open(tmp_path, "r", encoding="utf-8") as f:
                house_dict = json.load(f)
        finally:
            os.unlink(tmp_path)

        filename = f"house_seed{seed}.json"
        content = json.dumps(house_dict, ensure_ascii=False, indent=2).encode("utf-8")
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception:
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="房间生成失败，请检查参数后重试。")


@app.get("/room-specs", summary="查询支持的房间布局列表")
def list_room_specs():
    return {"room_specs": VALID_ROOM_SPECS}


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=True)
