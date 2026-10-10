# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 DevbyteCP
# Derived in part from zivillian/ism7mqtt (GPL-3.0-or-later)
"""Value codecs for ISM7 properties."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Protocol


class ValidationError(ValueError):
    """Raised when a value fails encode-time validation."""


class Codec(Protocol):
    """Encode and decode a single ISM7 property value."""

    @property
    def writable(self) -> bool: ...
    @property
    def ha_platform(self) -> str: ...
    def decode(self, raw: bytes) -> Any: ...
    def encode(self, value: Any) -> bytes: ...


@dataclass(slots=True)
class Int16Codec:
    min_value: int | None = None
    max_value: int | None = None
    unit: str | None = None

    @property
    def writable(self) -> bool:
        return self.min_value is not None and self.max_value is not None

    @property
    def ha_platform(self) -> str:
        return "number" if self.writable else "sensor"

    def decode(self, raw: bytes) -> int:
        return struct.unpack(">h", raw)[0]  # type: ignore[no-any-return]

    def encode(self, value: Any) -> bytes:
        v = int(value)
        if self.min_value is not None and v < self.min_value:
            raise ValidationError(f"{v} below min {self.min_value}")
        if self.max_value is not None and v > self.max_value:
            raise ValidationError(f"{v} above max {self.max_value}")
        return struct.pack(">h", v)

    def _pack_raw(self, raw_int: int) -> bytes:
        """Pack a raw int to bytes without range validation. Used by ScaledCodec."""
        try:
            return struct.pack(">h", raw_int)
        except struct.error as e:
            raise ValidationError(f"int16 overflow: {raw_int}") from e


@dataclass(slots=True)
class Uint8Codec:
    min_value: int | None = None
    max_value: int | None = None
    unit: str | None = None

    @property
    def writable(self) -> bool:
        return self.min_value is not None and self.max_value is not None

    @property
    def ha_platform(self) -> str:
        return "number" if self.writable else "sensor"

    def decode(self, raw: bytes) -> int:
        return raw[0]

    def encode(self, value: Any) -> bytes:
        v = int(value)
        if v < 0 or v > 255:
            raise ValidationError(f"{v} out of uint8 range")
        if self.min_value is not None and v < self.min_value:
            raise ValidationError(f"{v} below min {self.min_value}")
        if self.max_value is not None and v > self.max_value:
            raise ValidationError(f"{v} above max {self.max_value}")
        return bytes([v])

    def _pack_raw(self, raw_int: int) -> bytes:
        """Pack a raw int to bytes without range validation. Used by ScaledCodec."""
        if not 0 <= raw_int <= 255:
            raise ValidationError(f"uint8 overflow: {raw_int}")
        return bytes([raw_int])


@dataclass(slots=True)
class Uint16Codec:
    min_value: int | None = None
    max_value: int | None = None
    unit: str | None = None

    @property
    def writable(self) -> bool:
        return self.min_value is not None and self.max_value is not None

    @property
    def ha_platform(self) -> str:
        return "number" if self.writable else "sensor"

    def decode(self, raw: bytes) -> int:
        return struct.unpack(">H", raw)[0]  # type: ignore[no-any-return]

    def encode(self, value: Any) -> bytes:
        v = int(value)
        if v < 0 or v > 0xFFFF:
            raise ValidationError(f"{v} out of uint16 range")
        if self.min_value is not None and v < self.min_value:
            raise ValidationError(f"{v} below min {self.min_value}")
        if self.max_value is not None and v > self.max_value:
            raise ValidationError(f"{v} above max {self.max_value}")
        return struct.pack(">H", v)

    def _pack_raw(self, raw_int: int) -> bytes:
        """Pack a raw int to bytes without range validation. Used by ScaledCodec."""
        try:
            return struct.pack(">H", raw_int)
        except struct.error as e:
            raise ValidationError(f"uint16 overflow: {raw_int}") from e


@dataclass(slots=True)
class ScaledCodec:
    """Wraps a base int codec, dividing/multiplying by ``scale``.

    ``base.min_value`` / ``base.max_value`` are interpreted in **display
    (scaled) units**, e.g. -50..50 for temperatures in degrees C with
    scale=10. ``ScaledCodec.encode`` validates in display units, then
    multiplies and packs the raw int via the base struct without
    re-running the base's range check.
    """

    base: Int16Codec | Uint8Codec | Uint16Codec
    scale: float

    @property
    def writable(self) -> bool:
        return self.base.writable

    @property
    def ha_platform(self) -> str:
        return self.base.ha_platform

    @property
    def unit(self) -> str | None:
        return self.base.unit

    def decode(self, raw: bytes) -> float:
        return self.base.decode(raw) / self.scale

    def encode(self, value: Any) -> bytes:
        v = float(value)
        if self.base.min_value is None or self.base.max_value is None:
            raise ValidationError(
                "ScaledCodec requires base.min_value and base.max_value to be set for encoding"
            )
        if v < self.base.min_value:
            raise ValidationError(f"{v} below min {self.base.min_value}")
        if v > self.base.max_value:
            raise ValidationError(f"{v} above max {self.base.max_value}")
        raw_int = round(v * self.scale)
        # Pack via the base's struct format without re-validating range.
        return self.base._pack_raw(raw_int)


# Plain classes (not @dataclass) for codecs whose only stored field is the
# keyword-only `writable` flag, exposed as a `writable` property. Using a
# regular class with __slots__ keeps the constructor signature
# `__init__(self, *, writable=False)` clean and avoids the pitfall of
# @dataclass(slots=True) silently generating an __init__ that an explicit
# one then overrides.
class EnumCodec:
    """Codec for enum-valued properties (uint8 -> member name)."""

    __slots__ = ("members", "_writable")

    members: dict[int, str]
    _writable: bool

    def __init__(
        self,
        members: dict[int, str],
        *,
        writable: bool = False,
    ) -> None:
        self.members = members
        self._writable = writable

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def ha_platform(self) -> str:
        return "select" if self.writable else "sensor"

    @property
    def options(self) -> list[str]:
        return list(self.members.values())

    def decode(self, raw: bytes) -> str:
        v = raw[0]
        return self.members.get(v, f"unknown_{v}")

    def encode(self, value: Any) -> bytes:
        for k, name in self.members.items():
            if name == value:
                return bytes([k])
        raise ValidationError(f"unknown enum member: {value!r}")


class BoolCodec:
    """Codec for boolean properties (single byte, 0/non-zero)."""

    __slots__ = ("_writable",)

    _writable: bool

    def __init__(self, *, writable: bool = False) -> None:
        self._writable = writable

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def ha_platform(self) -> str:
        return "switch" if self.writable else "binary_sensor"

    def decode(self, raw: bytes) -> bool:
        return raw[0] != 0

    def encode(self, value: Any) -> bytes:
        return b"\x01" if bool(value) else b"\x00"


class TimeOfDayCodec:
    """Wolf time-of-day = uint16 minutes since midnight, formatted HH:MM."""

    __slots__ = ("_writable",)

    _writable: bool

    def __init__(self, *, writable: bool = False) -> None:
        self._writable = writable

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def ha_platform(self) -> str:
        return "text" if self.writable else "sensor"

    def decode(self, raw: bytes) -> str:
        minutes = struct.unpack(">H", raw)[0]
        h, m = divmod(minutes, 60)
        return f"{h:02d}:{m:02d}"

    def encode(self, value: Any) -> bytes:
        if not isinstance(value, str) or ":" not in value:
            raise ValidationError(f"invalid time-of-day: {value!r}")
        h, m = (int(x) for x in value.split(":", 1))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValidationError(f"invalid time-of-day: {value!r}")
        return struct.pack(">H", h * 60 + m)
