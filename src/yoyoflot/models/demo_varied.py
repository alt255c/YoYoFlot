"""Independently fictional, varied histories; no imported or relabelled data."""
import random
from datetime import date, timedelta


def generate(spec):
    rng = random.Random(spec["seed"])
    start, end = date.fromisoformat(spec["window_start"]), date.fromisoformat(spec["window_end"])
    people, segments = [], []
    ranges = [(6, 12), (20, 45), (8, 24), (8, 26), (30, 60), (12, 35), (10, 35), (0, 0), (12, 35), (10, 30)]
    for i in range(spec["profiles"]):
        mode = i % 10
        pid = f"demo2_profile_{spec['seed']:08x}_{i + 1:06d}"
        people.append(dict(profile_id=pid, display_name=f"Вымышленный профиль {spec['seed']} / {i + 1:04d}",
                           identity_status="certain", source_coverage=0.5 if mode == 6 else 1.0,
                           window_start=start, window_end=end))
        n = rng.randint(*ranges[mode])
        airports = ["SVO", "LED", "JFK", "CDG", "HND", "LHR", "DXB", "SIN"]
        rng.shuffle(airports)
        width = rng.randint(3, 7)
        point, previous, elapsed = airports[0], None, rng.randrange(4)
        for j in range(n):
            if mode in {2, 3, 4}:
                destination = airports[(j + 1) % 2]
            elif mode == 1:
                destination = airports[(j + 1) % width]
            elif mode == 5 and previous and rng.random() < 0.4:
                destination = previous
            else:
                destination = rng.choice([a for a in airports if a != point])
            missing = mode == 8 and j % 2 == 0
            segments.append(dict(segment_id=f"demo2_segment_{spec['seed']:08x}_{i + 1:06d}_{j + 1:03d}", profile_id=pid,
                                 departure_date=None if mode == 9 else start + timedelta(days=elapsed),
                                 departure_precision="unknown" if mode == 9 else "day",
                                 origin_iata=None if missing else point, destination_iata=None if missing else destination))
            previous, point = point, destination
            elapsed += rng.randint(5, 7) if mode == 0 else 3 if mode == 3 else rng.randint(1, 3) if mode == 2 else rng.randint(1, 2) if mode == 5 else 1
    return people, segments
