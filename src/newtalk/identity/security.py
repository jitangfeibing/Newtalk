import base64
import hashlib
import secrets


def generate_device_id() -> str:
    octets = bytearray(secrets.token_bytes(6))
    octets[0] = (octets[0] | 0x02) & 0xFE
    return ":".join(f"{octet:02x}" for octet in octets)


def generate_device_credential() -> str:
    return secrets.token_urlsafe(32)


def generate_recovery_code() -> str:
    encoded = base64.b32encode(secrets.token_bytes(15)).decode("ascii").rstrip("=")
    return "NT-" + "-".join(encoded[index : index + 4] for index in range(0, 24, 4))


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_recovery_code(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def digest_recovery_code(value: str) -> str:
    return digest_secret(normalize_recovery_code(value))
