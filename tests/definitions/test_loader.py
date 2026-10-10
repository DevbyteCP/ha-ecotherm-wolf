from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.ecotherm_wolf.definitions.codecs import (
    EnumCodec,
    Int16Codec,
    ScaledCodec,
)
from custom_components.ecotherm_wolf.definitions.loader import (
    DefinitionLoadError,
    load_catalog_from_dir,
)

FIXTURES = Path(__file__).parent / "fixtures" / "devices"


class TestLoader:
    def test_loads_device_from_xml(self):
        cat = load_catalog_from_dir(FIXTURES)
        dev = cat.get_device("CHA-10")
        assert dev.display_name == "CHA-10/400V (Test)"
        assert len(dev.parameters) == 3

    def test_scaled_codec_loaded(self):
        cat = load_catalog_from_dir(FIXTURES)
        param = cat.get_parameter("CHA-10", 0x0080)
        assert isinstance(param.codec, ScaledCodec)
        assert param.codec.scale == 10

    def test_enum_codec_loaded_writable(self):
        cat = load_catalog_from_dir(FIXTURES)
        param = cat.get_parameter("CHA-10", 0x00A1)
        assert isinstance(param.codec, EnumCodec)
        assert param.codec.writable is True
        assert param.codec.members[1] == "Auto"

    def test_int16_with_range_loaded(self):
        cat = load_catalog_from_dir(FIXTURES)
        param = cat.get_parameter("CHA-10", 0x010C)
        assert isinstance(param.codec, Int16Codec)
        assert param.codec.min_value == 5
        assert param.codec.max_value == 30
        assert param.codec.writable is True

    def test_broken_xml_is_skipped_with_warning(self, caplog):
        cat = load_catalog_from_dir(FIXTURES)
        # broken.xml is in the directory but ignored
        assert "BROKEN" not in cat.devices
        assert any("broken.xml" in m for m in caplog.messages)

    def test_missing_dir_raises(self, tmp_path):
        with pytest.raises(DefinitionLoadError):
            load_catalog_from_dir(tmp_path / "nonexistent")
