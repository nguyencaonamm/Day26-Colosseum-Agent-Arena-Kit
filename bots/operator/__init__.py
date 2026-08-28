"""The `operator` practice bot. Import `gateway` / `prosecute` from this package.

Deliberately does NOT import them eagerly: a bot package that pulls in the whole
kit at import time makes the sys.path footgun in bots/__init__.py far harder to
diagnose, and nothing needs them until a duel actually starts.
"""
