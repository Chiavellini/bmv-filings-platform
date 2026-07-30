"""Bloomberg side of the coverage engine — the market-data + estimates inputs.

soft/ has no terminal access, so this package emits a fill-in template and ingests the
values the user pastes back from a Bloomberg terminal (``[bbg]`` source tag).
"""
