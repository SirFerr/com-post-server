import math


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    # Fail closed for legacy stored coordinates as well as untrusted input.
    if not all(math.isfinite(value) for value in (lat1, lon1, lat2, lon2)):
        return math.inf
    if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and -180 <= lon1 <= 180 and -180 <= lon2 <= 180):
        return math.inf
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    a = min(1.0, max(0.0, a))
    return 6_371_000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
