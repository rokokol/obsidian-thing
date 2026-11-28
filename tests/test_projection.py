import pytest

from obsidian_thing.projection import project


def test_projection_puts_the_farthest_point_on_the_positive_side():
    direction = [1.0, -0.9, -0.9]
    points, explained = project({name: [value * scale for value in direction]
                                 for name, scale in (('a', 2.0), ('b', 0.4), ('c', -0.7))})
    assert abs(points['a'][0]) > abs(points['c'][0])
    # The largest component element fixes the axis sign, so a different BLAS cannot mirror the map
    assert points['a'][0] > 0
    assert 0 < explained[0] <= 1


def test_projection_rejects_a_vector_it_cannot_reduce():
    with pytest.raises(ValueError, match='invalid number'):
        project({'a': [1.0, 0.0], 'b': [float('nan'), 1.0]})


def test_projection_never_returns_a_negative_zero():
    points, explained = project({'a': [1.0, 0.0], 'b': [1.0, 0.0]})
    assert points['a'] == (0.0, 0.0)
    assert str(points['a'][0]) == '0.0'
    assert explained == [0.0, 0.0]


def test_projection_handles_one_file_and_two_files():
    assert project({'a': [1.0, 2.0]}) == ({'a': (0.0, 0.0)}, [0.0, 0.0])
    points, explained = project({'a': [1.0, 0.0], 'b': [0.0, 1.0]})
    assert [abs(points[name][0]) for name in ('a', 'b')] == [1.0, 1.0]
    assert [points[name][1] for name in ('a', 'b')] == [0.0, 0.0]
    assert explained[0] == 1.0 and explained[1] == 0.0
