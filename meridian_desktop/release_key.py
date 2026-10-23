"""The public key that verifies Meridian Desktop release signatures.

RELEASE_PUBLIC_KEY_B64 is the base64 of the 32-byte Ed25519 public key. The updater refuses any release whose
SHA256SUMS.sig does not verify against it, and the release publisher refuses to sign with a seed that does not
match it.

The private seed lives only outside the repository, at %LOCALAPPDATA%\\meridian-release-keys\\release-ed25519.key.
Create it once with packaging/make_release_key.py, which prints this public key. Paste that value here and commit
this file. While the value is empty, no release can be signed or verified, so the check fails closed.
"""

RELEASE_PUBLIC_KEY_B64 = ""
