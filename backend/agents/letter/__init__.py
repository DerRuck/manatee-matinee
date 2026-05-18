"""Letter agent package.

Public surface used by services/letter_runner.py:
  - LetterAgent          — BaseAgent subclass that loads letter/v1.yaml
  - LetterInput          — typed payload (lead + sender + triggering event)
  - LetterResult         — typed model output + run metadata
"""
from .agent import LetterAgent, LetterInput, LetterResult

__all__ = ["LetterAgent", "LetterInput", "LetterResult"]
