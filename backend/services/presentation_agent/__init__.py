"""Presentation Agent — turns research briefs and meeting context into a
structured slide outline ready for human polish or downstream design tools
(Canva, Figma, python-pptx).

Architecture mirrors services/research_agent: a typed YAML prompt registry,
a runner that calls Claude, and a Pydantic schema with a discriminated union
keyed by outline_type_id.
"""
