"""Deterministic text processing: normalization, terminology, numbers,
negation protection and BiDi/display formatting.

Nothing in this package calls a network service or an LLM. Every function
here must be a pure, deterministic transformation of its input so that the
same transcript always produces the same output and can be unit tested
without a microphone, network, or GUI.
"""
