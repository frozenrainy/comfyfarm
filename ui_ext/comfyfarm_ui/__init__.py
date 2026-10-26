# SPDX-License-Identifier: GPL-3.0-or-later
"""ComfyFarm frontend extension — install on the UI worker (a stock ComfyUI behind the ComfyFarm gateway).

Adds, through the official frontend extension API only:
  - a "Group: …" button next to Run (action bar); node definitions and submissions follow the chosen worker group,
    remembered per browser (localStorage); with it, a title for the runs of the open workflow (else its tab's name)
  - "My queue" next to it: the user's jobs by title, their state, and what the finished ones made
  - retry-safe submit: one prompt_id per click, retried with the same id on 503 / network errors (no duplicates)
  - a warning when the node list the router serves is out of date (no worker of the group answered since; D-46)
  - inside the dashboard's Workflow tab: a same-origin message to the ComfyFarm shell after each submit
"""
WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
