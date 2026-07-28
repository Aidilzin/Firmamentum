import pytest
from workspace.app import calculate_interest

def test_negative_value_exception():
    with pytest.raises(ValueError):
        calculate_interest(-500)