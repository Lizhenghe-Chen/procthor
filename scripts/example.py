import argparse
import json
import logging
import os
from typing import Optional
import traceback

from procthor.generation import (
    PROCTHOR10K_ROOM_SPEC_SAMPLER,
    HouseGenerator,
    GenerationFunctions,
    _create_default_generation_functions,
)
from procthor.utils.types import SamplingVars


def make_custom_generation_functions(skybox_id: Optional[str] = None):
    """Return a GenerationFunctions object based on defaults, with small overrides.

    - If `skybox_id` is provided, the skybox stage will set that id.
    """
    gfs = _create_default_generation_functions()

    # wrap existing skybox fn to optionally force a skybox id
    default_skybox = gfs.add_skybox

    def add_skybox_override(partial_house, controller, pt_db, split):
        # call default implementation first (preserves other procedural params)
        default_skybox(
            partial_house=partial_house, controller=controller, pt_db=pt_db, split=split
        )
        if skybox_id is not None:
            pp = partial_house.procedural_parameters
            if pp is None:
                partial_house.procedural_parameters = {"skyboxId": skybox_id}
            else:
                partial_house.procedural_parameters["skyboxId"] = skybox_id

    gfs.add_skybox = add_skybox_override
    return gfs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1, help="How many houses to generate")
    parser.add_argument("--seed", type=int, default=None, help="Fixed seed (omit for random)")
    parser.add_argument("--room-spec", type=str, default=None, help="Room spec key or None")
    parser.add_argument("--out-dir", type=str, default="out", help="Output directory")
    parser.add_argument("--skybox", type=str, default=None, help="Force skybox id")
    parser.add_argument("--max-floor-objects", type=int, default=None, help="Override max floor objects")
    parser.add_argument("--interior-boundary-scale", type=float, default=None, help="Override interior boundary scale")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    os.makedirs(args.out_dir, exist_ok=True)

    # Build a sample SamplingVars if user provided overrides
    sampling_vars = None
    if args.max_floor_objects is not None or args.interior_boundary_scale is not None:
        iv = args.interior_boundary_scale if args.interior_boundary_scale is not None else 1.9
        mf = args.max_floor_objects if args.max_floor_objects is not None else 5
        sampling_vars = SamplingVars(interior_boundary_scale=iv, max_floor_objects=mf)

    # Prepare generation functions (example: force skybox)
    gfs = make_custom_generation_functions(skybox_id=args.skybox)

    for i in range(args.count):
        run_seed = args.seed if args.seed is not None else None
        hg = HouseGenerator(
            split="train",
            seed=run_seed,
            room_spec=args.room_spec,
            room_spec_sampler=PROCTHOR10K_ROOM_SPEC_SAMPLER,
            generation_functions=gfs,
        )

        # If seed was None, HouseGenerator will pick one; capture it for reproducibility
        logging.info(f"Using seed={hg.seed}")

        out_path = os.path.join(args.out_dir, f"house_{i}_seed{hg.seed}.json")
        try:
            house, _ = hg.sample(sampling_vars=sampling_vars)
            # Validate with the controller (this will call the AI2-THOR controller)
            try:
                house.validate(hg.controller)
            except Exception as e:
                logging.warning(f"Validation failed: {e}")

            house.to_json(out_path)
        except Exception as e:
            # Save error info so user can inspect failures without stopping the whole run
            err_path = out_path + ".error.txt"
            logging.error(f"Sampling/validation failed. Writing error to {err_path}")
            with open(err_path, "w") as ef:
                ef.write("Exception:\n")
                ef.write(traceback.format_exc())
        # Also write a tiny metadata file with the seed and args used
        meta = {
            "seed": hg.seed,
            "room_spec": str(args.room_spec),
            "sampling_vars": {
                "interior_boundary_scale": sampling_vars.interior_boundary_scale if sampling_vars is not None else None,
                "max_floor_objects": sampling_vars.max_floor_objects if sampling_vars is not None else None,
            },
            "skybox": args.skybox,
        }
        with open(out_path + ".meta.json", "w") as f:
            json.dump(meta, f, indent=2)

        logging.info(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
