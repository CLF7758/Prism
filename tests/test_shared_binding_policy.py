import pytest
from prism.material_parameters import CHANNELS, RANGES, validate_texture_channel, texture_color_space
from prism.widgets.material_slots import MaterialSlot


def test_both_controls_and_service_policy_agree(qtbot):
    for key in RANGES:
        slot=MaterialSlot(key,key)
        qtbot.addWidget(slot)
        for i, channel in enumerate(CHANNELS):
            allowed=key!='normal' or channel=='RGB'
            assert slot.channel.model().item(i).isEnabled() is allowed
            if allowed:
                assert validate_texture_channel(key,channel)==channel
            else:
                with pytest.raises(ValueError,match='RGB'):
                    validate_texture_channel(key,channel)
        assert texture_color_space(key)==('srgb' if key in ('basecolor','emissive') else 'linear')
    with pytest.raises(ValueError):validate_texture_channel('unknown','RGB')
    with pytest.raises(ValueError):validate_texture_channel('roughness','unknown')
