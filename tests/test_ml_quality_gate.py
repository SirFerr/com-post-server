from tools.ml_pipeline import wilson_interval


def test_wilson_interval_penalizes_tiny_perfect_samples():
    one_lower, _ = wilson_interval(1, 1)
    twenty_lower, _ = wilson_interval(20, 20)
    assert one_lower < 0.5
    assert twenty_lower > 0.8


def test_wilson_interval_bounds_false_positive_uncertainty():
    _, tiny_upper = wilson_interval(0, 1)
    _, useful_upper = wilson_interval(0, 20)
    assert tiny_upper > 0.5
    assert useful_upper < 0.2
