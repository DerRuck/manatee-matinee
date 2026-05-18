"""Letter rendering package.

Public surface:
  - render_letter_to_pdf(result, sender_title=...) -> bytes
"""
from .render import render_letter_to_pdf

__all__ = ["render_letter_to_pdf"]
