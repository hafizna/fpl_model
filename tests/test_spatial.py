import pytest

from fpl_model.tactics.spatial import fingerprint, normalise_points, relative_height


def test_normalise_points_and_flip():
    points = [(0, 0), (100, 50)]
    normalised = normalise_points(points, x_max=100, y_max=50)
    assert normalised.tolist() == [[0.0, 0.0], [1.0, 1.0]]

    flipped = normalise_points(points, x_max=100, y_max=50, flip_x=True)
    assert flipped.tolist() == [[1.0, 0.0], [0.0, 1.0]]


def test_high_attacking_sample_has_high_final_third_share():
    points = [(0.75, 0.85), (0.82, 0.90), (0.90, 0.55), (0.88, 0.50)]
    result = fingerprint(points)

    assert result.sample_size == 4
    assert result.final_third_share == pytest.approx(1.0)
    assert result.box_share == pytest.approx(0.5)
    assert result.role_attack_index > 0.7


def test_relative_height_is_match_contextual():
    assert relative_height(0.72, 0.55) == pytest.approx(0.17)


def test_fingerprint_rejects_non_normalised_coordinates():
    with pytest.raises(ValueError):
        fingerprint([(70, 50)])
