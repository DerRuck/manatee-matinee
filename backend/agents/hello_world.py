from .base import BaseAgent


class HelloWorldAgent(BaseAgent):
    def __init__(self, version: int = 1):
        super().__init__("hello_world", version)
