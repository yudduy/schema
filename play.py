"""ARC-AGI-3 quickstart — play LS20 with a few scripted actions.

Docs: https://docs.arcprize.org
"""

import arc_agi
from arcengine import GameAction

arc = arc_agi.Arcade()  # uses ARC_API_KEY from env/.env, else an anonymous key
env = arc.make("ls20", render_mode="terminal")

for _ in range(10):
    env.step(GameAction.ACTION1)

print(arc.get_scorecard())
