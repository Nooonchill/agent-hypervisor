from __future__ import annotations

from rich.text import Text
from textual.widgets import Button


class HypervisorButton(Button):
    """Render button content with the widget's full Rich style, including its background."""

    def render(self) -> Text:
        return Text(str(self.label), style=self.rich_style)
