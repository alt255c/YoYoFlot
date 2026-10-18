"""Independent fixed-template holdout family, not the training generator.

Only fictional rows are returned. Benchmark annotations live in evaluation,
outside the forest inputs. Censoring is an explicit replayable intervention.
"""
import random
from datetime import date, timedelta


def generate(spec):
    start, end = date.fromisoformat(spec["window_start"]), date.fromisoformat(spec["window_end"])
    seed = spec["seed"]
    shared = ["SVO", "LED", "CDG", "JFK", "HND", "SIN", "DXB", "LHR"]
    random.Random(seed).shuffle(shared)
    people, segments = [], []
    for i in range(spec["profiles"]):
        mode = i % 12
        rng = random.Random((seed << 16) + i)
        points = shared[:] if mode == 11 else rng.sample(shared, len(shared))
        offset = seed % 3 if mode == 11 else rng.randrange(3)
        pid = f"holdout_profile_{seed:08x}_{i + 1:06d}"
        people.append(dict(profile_id=pid, display_name=f"Вымышленная история {seed} / {i + 1:04d}",
                           identity_status="certain", source_coverage=0.5 if mode == 9 else 1.0,
                           window_start=start, window_end=end))
        ranges = [(6, 8), (20, 26), (6, 10), (46, 60), (10, 16), (30, 48), (16, 20),
                  (18, 18), (18, 18), (18, 18), (0, 0), (18, 18)]
        n = rng.randint(*ranges[mode])
        for j in range(n):
            if mode == 0:
                elapsed, a, b = j * 11, j % 8, (j + 1) % 8
            elif mode == 1:
                elapsed, a, b = (j // 2) * 6 + j % 2, j % 2, (j + 1) % 2
            elif mode == 2:
                elapsed, a, b = (j // 2) * 18 + j % 2, j % 2, (j + 1) % 2
            elif mode == 4:
                elapsed, a, b = j, j % 2, (j + 1) % 2
            elif mode == 5:
                elapsed, a, b = j, j % 3, (j + 1) % 3
            elif mode == 6:
                elapsed, a, b = j * 4, j % 2, (j + 1) % 2
            else:
                elapsed, a, b = (j * 3 if mode == 11 else j), j % 8, (j + 1) % 8
            censored = spec["generator"].endswith("censored-v1") and i % 5 == 0 and j % 2 == 0
            missing_route = mode == 8 and j % 2 == 0 or censored
            day = start + timedelta(days=offset + elapsed)
            if day > end:
                raise ValueError("Holdout generator exceeded its fixed window")
            segments.append(dict(segment_id=f"holdout_segment_{seed:08x}_{i + 1:06d}_{j + 1:03d}", profile_id=pid,
                                 departure_date=None if mode == 7 else day,
                                 departure_precision="unknown" if mode == 7 else "day",
                                 origin_iata=None if missing_route else points[a], destination_iata=None if missing_route else points[b]))
    return people, segments
