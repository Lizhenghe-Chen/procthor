"""
Comprehensive example script for ProcTHOR house generation.

This script exposes the main HouseGenerator and sample() parameters and
adds flags to override common procedural choices (materials, lights,
which stages to run, etc.). Use `--help` for details.
"""
import argparse
import json
import logging
import os
import traceback
import numpy as np

from procthor.generation import PROCTHOR10K_ROOM_SPEC_SAMPLER, HouseGenerator, _create_default_generation_functions
from procthor.generation.house import NextSamplingStage
from procthor.generation import materials as materials_module
from procthor.utils.types import SamplingVars


def _noop(*args, **kwargs):
    """Generic no-op used to disable generation stages cleanly."""
    return None


def main():
    parser = argparse.ArgumentParser(description="ProcTHOR full-parameter example")

    # Basic
    parser.add_argument("--count", type=int, default=1,
                        help="要生成的房屋数量（整数）。每个房屋会保存为独立文件，默认 1。")
    parser.add_argument("--out-dir", type=str, default="out",
                        help="输出目录。生成的场景 JSON、元信息和（可选）中间阶段文件会写在此目录下。")
    parser.add_argument("--compress", action="store_true",
                        help="将输出以压缩格式 .json.gz 保存（默认保存为可读 .json）。")
    parser.add_argument("--no-validate", action="store_true",
                        help="跳过 AI2-THOR 控制器的验证步骤（更快，但可能生成无法直接在模拟器中加载的场景）。")

    # HouseGenerator
    parser.add_argument("--split", choices=["train", "val", "test"], default="train",
                        help="数据集划分，影响使用哪些资产/材质/skybox 等（通常设为 train）。")
    parser.add_argument("--seed", type=int, default=None,
                        help="随机种子；指定后同样的参数与种子可复现生成结果。")
    parser.add_argument("--room-spec", type=str, default=None,
                        help=(
                            "房间布局 ID（可选）。可用列表与采样权重如下：\n"
                            "  8-room-3-bed (1)\n"
                            "  7-room-3-bed (1)\n"
                            "  12-room-3-bed (1)\n"
                            "  12-room (1)\n"
                            "  4-room (5)\n"
                            "  2-bed-1-bath (1)\n"
                            "  5-room (1)\n"
                            "  2-bed-2-bath (1)\n"
                            "  bedroom-bathroom (2)\n"
                            "  kitchen-living-bedroom-room (1)\n"
                            "  kitchen-living-bedroom-room2 (1)\n"
                            "  kitchen-living-room (2)\n"
                            "  kitchen (1)\n"
                            "  living-room (1)\n"
                            "  bedroom (1)\n"
                            "  bathroom (1)\n"
                            "如果不指定，采样器会根据这些权重随机选择。"
                        ))
    parser.add_argument("--interior-boundary", type=str, default=None, metavar="W,H",
                        help="自定义内部边界大小，格式 '宽,高'（网格单元），例如 '10,8'；不指定则自动采样。")

    # sample() options
    parser.add_argument("--next-sampling-stage", type=str, default="STRUCTURE", choices=[s.name for s in NextSamplingStage],
                        help="从哪个生成阶段开始：用于从中断处恢复或只执行部分阶段（例如 STRUCTURE、DOORS、...）。")
    parser.add_argument("--return-partial-houses", action="store_true",
                        help="是否返回并（可选）保存每个生成阶段的中间 partial house，便于调试和查看中间状态。")

    # SamplingVars
    parser.add_argument("--interior-boundary-scale", type=float, default=None,
                        help="内部边界缩放系数，控制房间尺寸和门窗可用性，通常取值范围约为 1.6 到 2.2。")
    parser.add_argument("--max-floor-objects", type=int, default=None, choices=[1, 4, 5, 6, 7],
                        help="每个房间地面上大型物体（床、沙发等）的最大数量；若不指定则内部随机采样。")

    # Materials overrides (affect procthor.generation.materials globals)
    parser.add_argument("--p-all-walls-same", type=float, default=None,
                        help="以此概率使所有墙面使用相同材质（0-1），用于控制墙面统一性的强弱。")
    parser.add_argument("--p-all-floors-same", type=float, default=None,
                        help="以此概率使所有地板使用相同材质（0-1），用于控制地板统一性的强弱。")
    parser.add_argument("--p-sample-solid-wall-color", type=float, default=None,
                        help="以此概率选择纯色作为墙面颜色而不是贴图材质（0-1），便于生成简洁墙面风格。")

    # Light / skybox overrides
    # (已移除) skybox/灯光 参数已被简化

    # Enable/disable stages
    parser.add_argument("--no-doors", action="store_true",
                        help="禁用门的生成阶段（跳过放置门或保持现有门结构），用于仅测试布局/物体放置）。")
    # (已移除) no-lights / no-skybox 参数
    parser.add_argument("--no-exterior-walls", action="store_true",
                        help="禁用外墙生成阶段（可能影响外部轮廓、窗口/门的切割和遮挡效果）。")
    parser.add_argument("--no-rooms", action="store_true",
                        help="禁用按房间布置的阶段（保留结构但不放置房间内大型物体）。")
    parser.add_argument("--no-floor-objects", action="store_true",
                        help="禁用大型地面对象放置阶段（例如床、沙发、桌子等）。")
    parser.add_argument("--no-wall-objects", action="store_true",
                        help="禁用墙面对象放置阶段（例如挂画、橱柜等）。")
    parser.add_argument("--no-small-objects", action="store_true",
                        help="禁用小型装饰物放置阶段（例如杯子、植物等）。")
    parser.add_argument("--no-randomize-colors", action="store_true",
                        help="禁用物体颜色随机化（保留资产默认颜色，便于风格一致性测试）。")
    parser.add_argument("--no-randomize-states", action="store_true",
                        help="禁用物体状态随机化（例如门窗开合、物体可交互状态），用于确定性输出）。")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    os.makedirs(args.out_dir, exist_ok=True)

    # Apply material probability overrides if provided
    if getattr(args, "p_all_walls_same", None) is not None:
        materials_module.P_ALL_WALLS_SAME = args.p_all_walls_same
    if getattr(args, "p_all_floors_same", None) is not None:
        materials_module.P_ALL_FLOOR_SAME = args.p_all_floors_same
    if getattr(args, "p_sample_solid_wall_color", None) is not None:
        materials_module.P_SAMPLE_SOLID_WALL_COLOR = args.p_sample_solid_wall_color

    # Build SamplingVars
    sampling_vars = None
    if args.interior_boundary_scale is not None or args.max_floor_objects is not None:
        sampling_vars = SamplingVars(
            interior_boundary_scale=args.interior_boundary_scale or 1.9,
            max_floor_objects=args.max_floor_objects or 7,
        )

    # Build default generation functions and apply stage toggles / wrappers
    gfs = _create_default_generation_functions()

    # stage disabling
    if args.no_doors:
        gfs.add_doors = _noop
    if args.no_exterior_walls:
        gfs.add_exterior_walls = _noop
    if args.no_rooms:
        gfs.add_rooms = _noop
    if args.no_floor_objects:
        gfs.add_floor_objects = _noop
    if args.no_wall_objects:
        gfs.add_wall_objects = _noop
    if args.no_small_objects:
        gfs.add_small_objects = _noop
    if args.no_randomize_colors:
        gfs.randomize_object_colors = _noop
    if args.no_randomize_states:
        gfs.randomize_object_states = _noop


    # parse interior boundary if provided
    interior_boundary = None
    if args.interior_boundary:
        w, h = [int(x) for x in args.interior_boundary.split(",")]
        interior_boundary = np.ones((h, w), dtype=int)

    next_stage = NextSamplingStage[args.next_sampling_stage]

    # generation loop
    for i in range(args.count):
        hg = HouseGenerator(
            split=args.split,
            seed=args.seed,
            room_spec=args.room_spec,
            room_spec_sampler=PROCTHOR10K_ROOM_SPEC_SAMPLER,
            interior_boundary=interior_boundary,
            generation_functions=gfs,
        )

        logging.info(f"[{i+1}/{args.count}] seed={hg.seed} split={args.split} room_spec={args.room_spec or '(random)'}")

        ext = ".json.gz" if args.compress else ".json"
        out_path = os.path.join(args.out_dir, f"house_{i}_seed{hg.seed}{ext}")

        try:
            house, partial_houses = hg.sample(
                partial_house=None,
                return_partial_houses=args.return_partial_houses,
                sampling_vars=sampling_vars,
                next_sampling_stage=next_stage,
            )

            if not args.no_validate and hg.controller is not None:
                try:
                    house.validate(hg.controller)
                except Exception as e:
                    logging.warning(f"Validation failed but house will still be saved: {e}")

            house.to_json(out_path, compressed=args.compress)
            logging.info(f"Saved: {out_path}")

            if args.return_partial_houses and partial_houses:
                for stage, ph in partial_houses.items():
                    ph_path = out_path.replace(ext, f".partial_{stage.name}{ext}")
                    with open(ph_path, "w") as f:
                        json.dump(ph.__dict__ if hasattr(ph, "__dict__") else str(ph), f)

        except Exception:
            err_path = out_path + ".error.txt"
            logging.error(f"Generation failed; wrote error to: {err_path}")
            with open(err_path, "w") as f:
                f.write(traceback.format_exc())


if __name__ == "__main__":
    main()
