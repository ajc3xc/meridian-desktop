"""The public key that verifies Meridian Desktop release signatures. It is public, so it is safe to commit.

Written from the output of packaging/make_release_key.py. The private seed is kept outside the repository on the
release machine and is never committed, printed or logged.
"""

RELEASE_PUBLIC_KEY_B64 = "X6VaLCZJ7PInnYW+CB7xm0G2OUNKDiY336Lp3xpGySU="
