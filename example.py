from procthor.generation import PROCTHOR10K_ROOM_SPEC_SAMPLER, HouseGenerator

house_generator = HouseGenerator(
    split="train",  room_spec="4-room"
)
house, _ = house_generator.sample()
# house.validate(house_generator.controller)

house.to_json("temp.json")