import pytest
from prism.canvas_drop_policy import canvas_drop_destination

@pytest.mark.parametrize('distance,selected,candidate,expected', [
    (9, True, 'b', None), (10, False, 'b', None),
    (10, True, None, None), (10, True, 'a', None), (10, True, 'b', 'b')])
def test_destination(distance, selected, candidate, expected):
    assert canvas_drop_destination(travelled=distance, threshold=10,
        has_selection=selected, candidate=candidate, current='a') == expected
