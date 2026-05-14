"""
ProcTHOR FastAPI Server

暴露对用户有意义的参数，生成房间场景后直接返回 JSON 文件（单个房间）
或 ZIP 压缩包（多个房间）供下载。

启动方式：
    uvicorn server:app --reload --host 0.0.0.0 --port 8000

接口文档：
    http://localhost:8000/docs
"""
import json
import logging
import os
import tempfile
import traceback
from typing import Literal, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
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
    description="根据用户指定的参数程序化生成室内场景，并以 JSON / ZIP 格式下载。",
    version="1.0.0",
)

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


class GenerateRequest(BaseModel):
    """用户可配置的生成参数（对设计者友好）。"""

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
    width: Optional[int] = Field(
        default=None,
        ge=4,
        le=30,
        description="房屋宽度（网格单元，4–30）。与 height 同时填写才生效。",
    )
    height: Optional[int] = Field(
        default=None,
        ge=4,
        le=30,
        description="房屋深度（网格单元，4–30）。与 width 同时填写才生效。",
    )
    size_scale: Optional[float] = Field(
        default=None,
        ge=1.4,
        le=2.5,
        description="房间尺寸缩放系数（1.4–2.5），值越大房间越宽敞。",
    )
    max_floor_objects: Optional[Literal[1, 4, 5, 6, 7]] = Field(
        default=None,
        description="每个房间最多摆放多少件大型家具（床/沙发/桌子等），可选 1、4、5、6、7。",
    )
    uniform_wall_material: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="所有墙面使用统一材质的概率（0=完全随机，1=全部统一）。",
    )
    uniform_floor_material: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="所有地板使用统一材质的概率（0=完全随机，1=全部统一）。",
    )
    solid_wall_color_prob: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="墙面使用纯色而非贴图材质的概率（0=全贴图，1=全纯色）。",
    )

    @field_validator("room_spec")
    @classmethod
    def validate_room_spec(cls, v):
        if v is not None and v not in VALID_ROOM_SPECS:
            raise ValueError(
                f"不支持的 room_spec: '{v}'。可选值：{VALID_ROOM_SPECS}"
            )
        return v


def _generate_house(req: GenerateRequest) -> dict:
    """执行单次房屋生成，返回 house dict。"""
    # 材质概率覆盖（全局变量，需在每次生成前设置）
    if req.uniform_wall_material is not None:
        materials_module.P_ALL_WALLS_SAME = req.uniform_wall_material
    if req.uniform_floor_material is not None:
        materials_module.P_ALL_FLOOR_SAME = req.uniform_floor_material
    if req.solid_wall_color_prob is not None:
        materials_module.P_SAMPLE_SOLID_WALL_COLOR = req.solid_wall_color_prob

    # 内部边界
    interior_boundary = None
    if req.width is not None and req.height is not None:
        interior_boundary = np.ones((req.height, req.width), dtype=int)

    # SamplingVars
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
        room_spec_sampler=PROCTHOR10K_ROOM_SPEC_SAMPLER,
        interior_boundary=interior_boundary,
        generation_functions=gfs,
    )

    logger.info(
        f"Generating: seed={hg.seed} room_spec={req.room_spec or '(random)'}"
    )

    try:
        house, _ = hg.sample(
            partial_house=None,
            return_partial_houses=False,
            sampling_vars=sampling_vars,
            next_sampling_stage=NextSamplingStage["STRUCTURE"],
        )

        # 序列化为 dict（house.to_json 写文件；这里用临时文件再读回）
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            house.to_json(tmp_path, compressed=False)
            with open(tmp_path, "r", encoding="utf-8") as f:
                house_dict = json.load(f)
        finally:
            os.unlink(tmp_path)
    finally:
        # 确保 AI2-THOR Controller（Unity 子进程）在每次生成后都被关闭，避免进程泄漏
        if hg.controller is not None:
            try:
                hg.controller.stop()
            except Exception:
                pass
            hg.controller = None

    return house_dict, hg.seed


@app.post(
    "/generate",
    summary="生成室内场景",
    response_description="单个房间返回 JSON 文件；多个房间返回 ZIP 压缩包",
)
def generate(req: GenerateRequest):
    """根据参数生成一个 ProcTHOR 室内场景，直接下载 `.json` 文件。"""
    try:
        house_dict, seed = _generate_house(req)
    except Exception:
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail="房间生成失败，请检查参数后重试。")

    filename = f"house_seed{seed}.json"
    content = json.dumps(house_dict, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/room-specs", summary="查询支持的房间布局列表")
def list_room_specs():
    """返回所有可用的 `room_spec` 值。"""
    return {"room_specs": VALID_ROOM_SPECS}


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=True)
