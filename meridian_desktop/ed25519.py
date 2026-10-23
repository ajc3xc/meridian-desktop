"""Ed25519 signatures in pure Python, following RFC 8032 section 5.1. No dependency beyond the standard library.

    public_key(seed: bytes) -> bytes         the 32-byte public key for a 32-byte private seed
    sign(seed: bytes, message: bytes) -> bytes   the 64-byte signature
    verify(public: bytes, message: bytes, signature: bytes) -> bool

verify never raises for bad input. A wrong length, a point that is not on the curve, a non-canonical point
encoding, or an S that is not below the group order all return False. The updater checks one signature per
update, so the speed of pure Python is enough here.
"""

from __future__ import annotations

import hashlib

_P = 2**255 - 19  # the field prime
_L = 2**252 + 27742317777372353535851937790883648493  # the order of the base point
_D = -121665 * pow(121666, _P - 2, _P) % _P  # the curve constant d
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)  # a square root of -1 modulo P
_BY = 4 * pow(5, _P - 2, _P) % _P  # the y coordinate of the base point


def _recover_x(y: int, sign: int) -> int | None:
    """The x coordinate for y with the given low bit, or None when no point has that y and sign."""
    if y >= _P:
        return None  # a non-canonical encoding
    xx = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - xx) % _P != 0:
        return None  # y is not on the curve
    if x == 0 and sign == 1:
        return None  # the sign bit cannot be set for x = 0
    if x & 1 != sign:
        x = _P - x
    return x


_B = (_recover_x(_BY, 0), _BY)  # the base point; its x is the even root
_IDENTITY = (0, 1)


def _add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    """Add two points in affine coordinates. The Edwards formulas are complete, so no case is special."""
    x1, y1 = p
    x2, y2 = q
    t = _D * x1 * x2 * y1 * y2 % _P
    # One inversion serves both denominators: 1/(1+t) = (1-t)/(1-t^2) and 1/(1-t) = (1+t)/(1-t^2).
    inverse = pow((1 - t * t) % _P, _P - 2, _P)
    x3 = (x1 * y2 + x2 * y1) * (1 - t) * inverse % _P
    y3 = (y1 * y2 + x1 * x2) * (1 + t) * inverse % _P
    return x3, y3


def _mul(scalar: int, point: tuple[int, int]) -> tuple[int, int]:
    """The point multiplied by a non-negative integer, by double and add."""
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decode_point(data: bytes) -> tuple[int, int] | None:
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, value >> 255)
    if x is None:
        return None
    return x, y


def _expand(seed: bytes) -> tuple[int, bytes]:
    """The clamped secret scalar and the hash prefix, from RFC 8032 section 5.1.5."""
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8  # clear the three low bits and bit 255
    scalar |= 1 << 254  # set bit 254
    return scalar, digest[32:]


def _check_seed(seed: bytes) -> None:
    if not isinstance(seed, (bytes, bytearray)) or len(seed) != 32:
        raise ValueError("an Ed25519 private seed is 32 bytes")


def public_key(seed: bytes) -> bytes:
    """The public key for a 32-byte private seed."""
    _check_seed(seed)
    scalar, _prefix = _expand(bytes(seed))
    return _encode_point(_mul(scalar, _B))


def sign(seed: bytes, message: bytes) -> bytes:
    """The 64-byte signature of message under the 32-byte private seed."""
    _check_seed(seed)
    scalar, prefix = _expand(bytes(seed))
    pub = _encode_point(_mul(scalar, _B))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    r_point = _encode_point(_mul(r, _B))
    k = int.from_bytes(hashlib.sha512(r_point + pub + message).digest(), "little") % _L
    s = (r + k * scalar) % _L
    return r_point + s.to_bytes(32, "little")


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True only when signature is a valid Ed25519 signature of message under public. Never raises."""
    if not isinstance(public, (bytes, bytearray)) or not isinstance(signature, (bytes, bytearray)):
        return False
    if len(public) != 32 or len(signature) != 64:
        return False
    a_point = _decode_point(bytes(public))
    r_point = _decode_point(bytes(signature[:32]))
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = int.from_bytes(hashlib.sha512(bytes(signature[:32]) + bytes(public) + message).digest(), "little") % _L
    return _mul(s, _B) == _add(r_point, _mul(k, a_point))


__all__ = ["public_key", "sign", "verify"]
