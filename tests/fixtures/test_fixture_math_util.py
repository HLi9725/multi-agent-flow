from tests.fixtures.fixture_math_util import pure_add

def test_pure_add():
    assert pure_add(2, 3) == 5
    assert pure_add(-1, 1) == 0
